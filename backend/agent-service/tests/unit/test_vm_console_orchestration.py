"""qkv_vm_console 在线适配器编排集成测试（§11.1 验收线 A 的进程内 E2E）。

用 fake HTTP/Redis/store 驱动 run_vm_console_signal 全流程：
意图编译 → Inventory 校验 → 基线截图 → 质量判定 → （可选唤醒分支）→
视觉策略门禁 → 变量产出。真实 Bridge/宿主机交互由 Go 侧测试覆盖。
"""

import json

import pytest
from app.tools.vm_console import adapter as vm_adapter
from app.tools.vm_console.adapter import run_vm_console_signal


class _FakeResponse:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {"ok": True}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHTTPClient:
    """记录全部 POST，模拟 conversation-service 内部端点。"""

    def __init__(self, *args, **kwargs):
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict | None = None, **kwargs):  # noqa: A002
        self.posts.append((url, json or {}))
        return _FakeResponse()


class _FakeRedis:
    def __init__(self, results: list[tuple[str, str]]):
        self._results = list(results)

    async def blpop(self, key: str, timeout: int = 0):
        if self._results:
            return self._results.pop(0)
        return None


class _FakeSessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return False


def _signal(host="{{HOST}}", vm_id="{{VM_ID}}"):
    return {
        "id": "s_vm_console_it",
        "acquire": {
            "tool": "qkv_vm_console",
            "args": {"host": host, "vm_id": vm_id, "timeout": 60},
        },
        "orchestrate": {
            "requires": ["HOST", "VM_ID"],
            "produces": [
                {"name": "VM_CONSOLE_STATE", "path": "display_state"},
                {"name": "VM_CONSOLE_SUMMARY", "path": "summary"},
                {"name": "VM_CONSOLE_CONFIDENCE", "path": "confidence"},
                {"name": "VM_CONSOLE_ARTIFACT_ID", "path": "artifact_id"},
            ],
        },
    }


def _ppm_bytes():
    return b"P6\n8 8\n255\n" + bytes((120, 120, 120)) * 64


@pytest.mark.asyncio
async def test_online_orchestration_baseline_path_produces_variables(monkeypatch):
    """基线路径：非近黑 → 无唤醒 → 视觉策略门禁降级 unavailable → 变量齐备。"""

    monkeypatch.setenv("VM_CONSOLE_CAPTURE_ENABLED", "true")
    monkeypatch.delenv("VM_CONSOLE_VISION_ALLOWED", raising=False)
    monkeypatch.setenv("VM_CONSOLE_SIM_INVENTORY", "123=SVR_SIM_01")

    fake_http = _FakeHTTPClient()
    monkeypatch.setattr(vm_adapter, "InternalHTTPClient", lambda *a, **k: fake_http)

    baseline_payload = {
        "exit_code": 0,
        "near_black": False,
        "artifact_id": "artifact-it-001",
        "sha256": "f" * 64,
        "quality": {"near_black": False, "metrics": {"algorithm_revision": "near-black-v1"}},
    }
    monkeypatch.setattr(
        vm_adapter, "_redis_client", lambda: _FakeRedis([("vm_console_result:exec-1", json.dumps(baseline_payload))])
    )

    async def _fake_fetch(http_client, artifact_id):
        return _ppm_bytes()

    monkeypatch.setattr(vm_adapter, "_fetch_artifact_bytes", _fake_fetch)

    status_log: list[str] = []
    audit_log: list[str] = []

    async def _fake_update(session, capture_id, status, **kwargs):
        status_log.append(status)

    async def _fake_audit(session, *, capture_id, event_type, **kwargs):
        audit_log.append(event_type)

    async def _fake_create(session, **kwargs):
        status_log.append("created")

    monkeypatch.setattr(vm_adapter.store, "update_capture_status", _fake_update)
    monkeypatch.setattr(vm_adapter.store, "insert_audit_event", _fake_audit)
    monkeypatch.setattr(vm_adapter.store, "create_capture_record", _fake_create)

    result = await run_vm_console_signal(
        _signal(),
        {"HOST": "SVR_SIM_01", "VM_ID": "123"},
        conversation_id="conv-it",
        case_id="Q202608200001",
        session_id="run-it",
        db_session_factory=_FakeSessionFactory(),
    )

    assert result.success is True
    assert result.capture_id
    # 状态机前进顺序（无唤醒分支）
    assert status_log == [
        "created",
        "inventory_verified",
        "baseline_capturing",
        "quality_checked",
        "vision_analyzing",
        "completed",
    ]
    # 审计事件覆盖关键节点
    for event in ("requested", "target_verified", "baseline_capturing", "quality_checked", "vision_completed"):
        assert event in audit_log
    assert "wake_confirm_requested" not in audit_log  # 非近黑不得请求唤醒
    # 变量产出（视觉策略关闭 → unavailable/unknown，仍回写变量供下游判定）
    first = result.values[0]
    assert first["vm_console_state"] == "unknown"
    assert first["vm_console_artifact_id"] == "artifact-it-001"
    assert "VISION_UNAVAILABLE_BY_POLICY" in first["vm_console_summary"]
    # 观察推送与内部 API 只使用受限端点
    urls = [url for url, _ in fake_http.posts]
    assert any(url.endswith("/vm-console-op") for url in urls)
    assert any(url.endswith("/vm-console-observation") for url in urls)


@pytest.mark.asyncio
async def test_online_orchestration_near_black_wake_flow(monkeypatch):
    """近黑路径：确认卡 → 用户确认 → 唤醒 + 重截 → 有效制品切换为重截图。"""

    monkeypatch.setenv("VM_CONSOLE_CAPTURE_ENABLED", "true")
    monkeypatch.delenv("VM_CONSOLE_VISION_ALLOWED", raising=False)
    monkeypatch.setenv("VM_CONSOLE_SIM_INVENTORY", "123=SVR_SIM_01")
    monkeypatch.setattr(vm_adapter, "WAKE_SETTLE_SECONDS", 0)

    fake_http = _FakeHTTPClient()
    monkeypatch.setattr(vm_adapter, "InternalHTTPClient", lambda *a, **k: fake_http)

    baseline_payload = {"exit_code": 0, "near_black": True, "artifact_id": "a-base", "sha256": "b" * 64, "quality": {"near_black": True}}
    wake_payload = {"exit_code": 0, "error_type": None}
    recapture_payload = {"exit_code": 0, "near_black": False, "artifact_id": "a-recap", "sha256": "c" * 64, "quality": {"near_black": False}}
    decision = json.dumps({"confirmed": True})
    results = [
        ("vm_console_result:exec-1", json.dumps(baseline_payload)),
        ("vm_console_wake_decision:cap-1", decision),
        ("vm_console_result:wake-1", json.dumps(wake_payload)),
        ("vm_console_result:recap-1", json.dumps(recapture_payload)),
    ]
    # 共享同一个 fake Redis：适配器多次调用 _redis_client() 必须按序消费同一队列。
    fake_redis = _FakeRedis(results)
    monkeypatch.setattr(vm_adapter, "_redis_client", lambda: fake_redis)

    async def _fake_fetch(http_client, artifact_id):
        return _ppm_bytes()

    monkeypatch.setattr(vm_adapter, "_fetch_artifact_bytes", _fake_fetch)

    status_log: list[str] = []
    audit_log: list[str] = []
    capture_ids: list[str] = []

    async def _fake_update(session, capture_id, status, **kwargs):
        status_log.append(status)
        capture_ids.append(capture_id)

    async def _fake_audit(session, *, capture_id, event_type, **kwargs):
        audit_log.append(event_type)

    async def _fake_create(session, **kwargs):
        pass

    monkeypatch.setattr(vm_adapter.store, "update_capture_status", _fake_update)
    monkeypatch.setattr(vm_adapter.store, "insert_audit_event", _fake_audit)
    monkeypatch.setattr(vm_adapter.store, "create_capture_record", _fake_create)

    result = await run_vm_console_signal(
        _signal(),
        {"HOST": "SVR_SIM_01", "VM_ID": "123"},
        conversation_id="conv-it",
        case_id="Q202608200002",
        session_id="run-it2",
        db_session_factory=_FakeSessionFactory(),
    )

    assert result.success is True
    # 唤醒链路状态：confirmation_pending → waking → recapturing
    assert "wake_confirmation_pending" in status_log
    assert "waking" in status_log
    assert "recapturing" in status_log
    assert "wake_confirm_requested" in audit_log
    assert "wake_confirmed" in audit_log
    assert "recaptured" in audit_log
    # 固定 operation：只推送 capture_baseline 与 wake_down_key
    ops = [payload.get("operation") for _, payload in fake_http.posts if "vm-console-op" in _]
    assert set(ops) <= {"capture_baseline", "wake_down_key"}
    assert ops.count("wake_down_key") == 1  # 每运行最多一次唤醒


@pytest.mark.asyncio
async def test_online_orchestration_inventory_mismatch_fails_closed(monkeypatch):
    """归属不匹配：TARGET_OWNERSHIP_MISMATCH，绝不下发 Bridge 操作。"""

    monkeypatch.setenv("VM_CONSOLE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("VM_CONSOLE_SIM_INVENTORY", "123=SVR_SIM_01")

    fake_http = _FakeHTTPClient()
    monkeypatch.setattr(vm_adapter, "InternalHTTPClient", lambda *a, **k: fake_http)
    monkeypatch.setattr(vm_adapter, "_redis_client", lambda: _FakeRedis([]))

    async def _fake_create(session, **kwargs):
        pass

    async def _fake_update(session, *args, **kwargs):
        pass

    async def _fake_audit(session, **kwargs):
        pass

    monkeypatch.setattr(vm_adapter.store, "create_capture_record", _fake_create)
    monkeypatch.setattr(vm_adapter.store, "update_capture_status", _fake_update)
    monkeypatch.setattr(vm_adapter.store, "insert_audit_event", _fake_audit)

    result = await run_vm_console_signal(
        _signal(),
        {"HOST": "SVR_OTHER_HOST", "VM_ID": "123"},
        conversation_id="conv-it",
        case_id="Q202608200003",
        session_id="run-it3",
        db_session_factory=_FakeSessionFactory(),
    )

    assert result.success is False
    assert result.error_code == "TARGET_OWNERSHIP_MISMATCH"
    # 未向 Bridge/会话端点推送任何截图操作
    assert all(not url.endswith("/vm-console-op") for url, _ in fake_http.posts)
