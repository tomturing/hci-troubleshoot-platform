"""acli 插件下发状态机单测：只有设备侧真成功才推进 current_spec_hash。

用内存 store + fake bridge，断言租约、回执、重试冷却与「页面无法伪造成功」。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.acli_plugin_spec import TEMPLATE_SPEC, build_descriptor, normalize_spec
from app.services.acli_plugin_relay import AcliPluginDeployRelay

NOW = datetime(2026, 11, 14, 6, 0, 0, tzinfo=UTC)
DEPLOY_ID = "htp-acli-test-0001"
CREDENTIALS = json.dumps({"24": {"auth_type": "key", "username": "root", "private_key": "TESTKEY"}})
SPEC_HASH = build_descriptor(normalize_spec(TEMPLATE_SPEC), deploy_id=DEPLOY_ID, now=NOW)["spec_hash"]


def _context(**overrides) -> dict[str, Any]:
    base: dict[str, Any] = {
        "deploy_id": DEPLOY_ID,
        "plugin_name": "hci_lmt_check",
        "spec_hash": SPEC_HASH,
        "env_code": "24",
        "snapshot_node_ip": "172.28.24.1",
        "spec_json": normalize_spec(TEMPLATE_SPEC),
        "node_ip": "172.28.24.1",
        "ssh_port": 22,
        "auth_type": "key",
        "credential_ref": "24",
        "bridge_url": "",
        "enabled": True,
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
            "env_code": "24",
            "node_ip": "172.28.24.1",
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


def _relay(store: InMemoryStore, bridge: FakeBridge, **overrides):
    options = {
        "store": store,
        "bridge_client": bridge,
        "default_bridge_url": "ws://terminal-bridge:9999",
        "credentials_json": CREDENTIALS,
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
    assert call["bridge_url"] == "ws://terminal-bridge:9999"
    assert call["node_ip"] == "172.28.24.1"
    assert call["exec_id"] == store.deployment["exec_id"]
    assert call["trace_id"] == store.deployment["trace_id"]
    assert call["case_id"] == f"htp-acli-{DEPLOY_ID}"
    assert DEPLOY_ID in call["command"]
    assert "TESTKEY" not in call["command"]


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


def test_missing_environment_is_structured():
    store, bridge = InMemoryStore(context=_context(node_ip="")), FakeBridge()
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert bridge.calls == []
    assert store.finishes[-1]["error_code"] == "environment_missing"


def test_missing_credentials_is_structured():
    store, bridge = InMemoryStore(), FakeBridge()
    relay = _relay(store, bridge, credentials_json="")
    asyncio.run(relay.reconcile_once())
    assert bridge.calls == []
    assert store.finishes[-1]["error_code"] == "credential_missing"


def test_lost_lease_stops_before_bridge():
    store, bridge = InMemoryStore(running_ok=False), FakeBridge()
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert bridge.calls == []
    assert store.finishes[-1]["error_code"] == "lease_lost"


def test_environment_bridge_url_override_is_used():
    store = InMemoryStore(context=_context(bridge_url="ws://windows-host:9999"))
    bridge = FakeBridge()
    asyncio.run(_relay(store, bridge).reconcile_once())
    assert bridge.calls[0]["bridge_url"] == "ws://windows-host:9999"


def test_no_claimable_task_returns_false():
    store, bridge = InMemoryStore(status="succeeded"), FakeBridge()
    assert asyncio.run(_relay(store, bridge).reconcile_once()) is False
    assert bridge.calls == []


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
