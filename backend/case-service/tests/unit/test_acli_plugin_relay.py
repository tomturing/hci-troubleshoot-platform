"""acli 插件下发状态机单测：复用 bridge 已登录会话，只有设备侧真成功才推进 spec_hash。

同步测试 + asyncio.run；内存 store + fake bridge，不依赖 pytest-asyncio / 真实 WS。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.domain.acli_plugin_spec import TEMPLATE_SPEC, build_descriptor, normalize_spec
from app.services.acli_plugin_relay import AcliPluginDeployRelay, SqlAcliPluginStore

NOW = datetime(2026, 11, 14, 6, 0, 0, tzinfo=UTC)
DEPLOY_ID = "htp-acli-test-0001"
CASE_ID = "case-0042"      # 存在 deployment.env_code 列里，语义是 bridge 会话 case_id
NODE_IP = "172.28.24.1"
SPEC_HASH = build_descriptor(normalize_spec(TEMPLATE_SPEC), deploy_id=DEPLOY_ID, now=NOW)["spec_hash"]

BRIDGE_CALL_KEYS = {"bridge_url", "case_id", "node_ip", "command", "exec_id", "trace_id", "timeout"}


class NoRowsCursor:
    """不带 RETURNING 的 UPDATE 没有结果集；读取 mappings() 属于契约破坏。"""

    returns_rows = False

    def mappings(self):
        raise AssertionError("无 RETURNING 的 UPDATE 不应读取结果集")


class NoRowsSession:
    def __init__(self):
        self.committed = False

    async def execute(self, _sql, _params):
        return NoRowsCursor()

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class NoRowsDB:
    def __init__(self):
        self.session = NoRowsSession()

    def async_session_factory(self):
        return self.session


def test_sql_store_does_not_read_rows_from_update_without_returning():
    db = NoRowsDB()
    asyncio.run(SqlAcliPluginStore(db).mark_succeeded_plugin("hci_lmt_check", SPEC_HASH, NOW))
    assert db.session.committed


def _context(**overrides) -> dict[str, Any]:
    base: dict[str, Any] = {
        "deploy_id": DEPLOY_ID,
        "plugin_name": "hci_lmt_check",
        "spec_hash": SPEC_HASH,
        "env_code": CASE_ID,
        "node_ip": NODE_IP,
        "spec_json": normalize_spec(TEMPLATE_SPEC),
    }
    base.update(overrides)
    return base


class InMemoryStore:
    def __init__(self, *, context: dict | None = None, status: str = "pending", running_ok: bool = True):
        self.context = context if context is not None else _context()
        self.deployment: dict[str, Any] = {
            "deploy_id": DEPLOY_ID,
            "plugin_name": "hci_lmt_check",
            "spec_hash": SPEC_HASH,
            "env_code": CASE_ID,
            "node_ip": NODE_IP,
            "status": status,
            "attempt": 0,
            "exec_id": "",
            "trace_id": "",
            "lease_owner": "",
        }
        self.running_ok = running_ok
        self.claim_calls = 0
        self.finishes: list[dict[str, Any]] = []
        self.plugin_updates: list[dict[str, Any]] = []
        self.heartbeats = 0

    async def claim(self, **kwargs) -> dict[str, Any] | None:
        self.claim_calls += 1
        if self.claim_calls > 1 or self.deployment["status"] != "pending":
            return None
        self.deployment.update(
            status="claimed",
            lease_owner=kwargs["owner"],
            attempt=self.deployment["attempt"] + 1,
            exec_id=kwargs["exec_id"],
            trace_id=kwargs["trace_id"],
        )
        return dict(self.deployment)

    async def mark_running(self, deploy_id: str, owner: str, until: datetime) -> bool:
        if not self.running_ok:
            return False
        self.deployment["status"] = "running"
        return True

    async def heartbeat(self, deploy_id: str, owner: str, until: datetime) -> bool:
        self.heartbeats += 1
        return self.deployment["lease_owner"] == owner

    async def get_context(self, deploy_id: str) -> dict[str, Any] | None:
        return self.context

    async def finish(self, deploy_id: str, **kwargs) -> bool:
        if self.deployment["lease_owner"] != kwargs["owner"]:
            return False
        if self.deployment["status"] not in ("claimed", "running"):
            return False
        self.deployment["status"] = kwargs["status"]
        self.finishes.append({"deploy_id": deploy_id, **kwargs})
        return True

    async def mark_succeeded_plugin(self, plugin_name: str, spec_hash: str, at: datetime) -> None:
        self.plugin_updates.append({"plugin_name": plugin_name, "spec_hash": spec_hash, "at": at})


class FakeBridge:
    def __init__(
        self,
        *,
        status: str = "success",
        smoke: str = "巡检完成",
        exit_code: int = 0,
        spec_hash: str = SPEC_HASH,
    ):
        self.calls: list[dict[str, Any]] = []
        payload = {
            "status": status,
            "plugin_name": "hci_lmt_check",
            "version": "0.0.1",
            "spec_hash": spec_hash,
            "deploy_id": DEPLOY_ID,
            "error": "" if status == "success" else "plugin_smoke_failed",
        }
        self.stdout = (
            "ACP_RESULT_BEGIN\n" + json.dumps(payload) + "\nACP_RESULT_END\n"
            + f"ACP_SMOKE_BEGIN\n{smoke}\nACP_SMOKE_END\n"
        )
        self.exit_code = exit_code

    async def deploy(self, **kwargs) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "stdout": self.stdout,
            "stderr": "",
            "exit_code": self.exit_code,
            "timed_out": False,
            "error_type": None,
            "exec_id": kwargs["exec_id"],
            "trace_id": kwargs["trace_id"],
        }


class SlowBridge(FakeBridge):
    """让心跳循环有机会在 deploy 期间真正跑起来。"""

    async def deploy(self, **kwargs) -> dict[str, Any]:
        await asyncio.sleep(0.04)
        return await super().deploy(**kwargs)


def _relay(store: InMemoryStore, bridge: FakeBridge, **overrides):
    options = {
        "store": store,
        "bridge_client": bridge,
        "default_bridge_url": "ws://terminal-bridge:9999",
        "heartbeat_interval": 0.01,
        "owner": "test-owner",
        "now_fn": lambda: NOW,
    }
    options.update(overrides)
    return AcliPluginDeployRelay(**options)


def test_success_advances_current_spec_hash_and_keeps_trace():
    store, bridge = InMemoryStore(), FakeBridge()
    assert asyncio.run(_relay(store, bridge).reconcile_once()) is True

    assert store.finishes[-1]["status"] == "succeeded"
    verification = store.finishes[-1]["verification"]
    assert verification["ok"] is True
    assert verification["device_spec_hash"] == verification["spec_hash"]
    assert verification["smoke_output"] == "巡检完成"
    assert len(store.plugin_updates) == 1
    assert store.plugin_updates[0]["spec_hash"] == verification["spec_hash"]

    call = bridge.calls[0]
    assert set(call) == BRIDGE_CALL_KEYS  # 不含 credential/ssh_port/credential_ref 等任何凭据字段
    assert call["bridge_url"] == "ws://terminal-bridge:9999"
    assert call["case_id"] == CASE_ID
    assert call["node_ip"] == NODE_IP
    assert call["exec_id"] == store.deployment["exec_id"]
    assert call["trace_id"] == store.deployment["trace_id"]
    assert DEPLOY_ID in call["command"]
    serialized = json.dumps(call)
    assert "TESTKEY" not in serialized
    assert "private_key" not in serialized
    assert "password" not in serialized


def test_device_rollback_never_marks_success():
    store, bridge = InMemoryStore(), FakeBridge(status="rolled_back")
    asyncio.run(_relay(store, bridge).reconcile_once())

    assert store.finishes[-1]["status"] == "failed"
    assert store.finishes[-1]["verification"]["device_status"] == "rolled_back"
    assert store.finishes[-1]["error_code"]
    assert store.finishes[-1]["lease_until"] > NOW  # 冷却后允许重试
    assert store.plugin_updates == []


def test_nonzero_exit_code_is_failed_even_with_success_marker():
    store, bridge = InMemoryStore(), FakeBridge(exit_code=1)
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert store.finishes[-1]["status"] == "failed"
    assert store.finishes[-1]["verification"]["ok"] is False
    assert store.plugin_updates == []


def test_invalid_spec_never_calls_bridge():
    bad_spec = normalize_spec({**TEMPLATE_SPEC, "smoke_args": ""})
    store, bridge = InMemoryStore(context=_context(spec_json=bad_spec)), FakeBridge()
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert bridge.calls == []
    assert store.finishes[-1]["error_code"] == "spec_invalid"


@pytest.mark.parametrize("missing", ["case_id", "node_ip"])
def test_missing_target_session_is_structured(missing):
    if missing == "case_id":
        store = InMemoryStore(context=_context(env_code=""))
        store.deployment["env_code"] = ""
    else:
        store = InMemoryStore(context=_context(node_ip=""))
        store.deployment["node_ip"] = ""
    bridge = FakeBridge()
    asyncio.run(_relay(store, bridge).reconcile_once())

    assert bridge.calls == []
    assert store.finishes[-1]["error_code"] == "session_missing"
    assert store.finishes[-1]["lease_until"] > NOW


def test_credentials_json_is_never_forwarded_to_bridge():
    store, bridge = InMemoryStore(), FakeBridge()
    relay = _relay(store, bridge, credentials_json="TESTKEY-placeholder")
    asyncio.run(relay.reconcile_once())
    assert store.finishes[-1]["status"] == "succeeded"
    assert "TESTKEY" not in json.dumps(bridge.calls[0])


def test_lost_lease_stops_before_bridge():
    store, bridge = InMemoryStore(running_ok=False), FakeBridge()
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert bridge.calls == []
    assert store.finishes[-1]["error_code"] == "lease_lost"


def test_bridge_url_always_uses_default_even_if_context_has_override():
    store = InMemoryStore(context=_context(bridge_url="ws://windows-host:9999"))
    bridge = FakeBridge()
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert bridge.calls[0]["bridge_url"] == "ws://terminal-bridge:9999"


def test_no_claimable_task_returns_false():
    store, bridge = InMemoryStore(status="succeeded"), FakeBridge()
    assert asyncio.run(_relay(store, bridge).reconcile_once()) is False
    assert bridge.calls == []


def test_heartbeat_runs_during_deploy():
    store, bridge = InMemoryStore(), SlowBridge()
    asyncio.run(_relay(store, bridge, heartbeat_interval=0.01).reconcile_once())
    assert store.heartbeats >= 1
    assert store.finishes[-1]["status"] == "succeeded"


def test_reconcile_loop_is_cancellable():
    store, bridge = InMemoryStore(status="succeeded"), FakeBridge()

    async def _run_loop():
        task = asyncio.create_task(_relay(store, bridge).reconcile_loop(interval=0.01))
        await asyncio.sleep(0.03)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run_loop())
    assert store.claim_calls >= 1


def test_spec_hash_mismatch_is_rejected():
    store, bridge = InMemoryStore(), FakeBridge(spec_hash="other-hash")
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert store.finishes[-1]["status"] == "failed"
    assert store.finishes[-1]["error_code"] == "spec_hash_mismatch"
    assert store.plugin_updates == []


def test_retry_backoff_grows_with_attempt():
    store, bridge = InMemoryStore(), FakeBridge(status="rolled_back")
    store.deployment["attempt"] = 3
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert store.finishes[-1]["lease_until"] <= NOW + timedelta(seconds=60)
