"""四类 QFK 从 Signal Schema 到最终业务输出的完整契约矩阵。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.adapters.agents.htp.kbd_differential import KBDDiagnostic
from app.adapters.agents.htp.kbd_model import KBDStep
from app.tools.acli import executor as executor_module
from app.tools.acli.executor import ExecResult
from shared.schemas.kbd_signal_safety import validate_kbd_read_only_signals_json
from shared.schemas.signal_schema import validate_signals_json


class FakeExecutor:
    """记录编译后的桥请求，并返回带标准进程三元组的现场替身。"""

    def __init__(self, stdout: str, *, stderr: str = "", exit_code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.calls: list[dict] = []
        self._redis = SimpleNamespace()

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ExecResult(
            stdout=self.stdout,
            stderr=self.stderr,
            exit_code=self.exit_code,
            command=str(kwargs.get("args", {}).get("command") or ""),
            node="172.28.25.4",
            duration_ms=12,
            truncated=False,
            risk_level=1,
            exec_id=f"matrix-{len(self.calls)}",
        )


def _document(signal: dict) -> dict:
    return {"schema_version": 2, "signals": [signal]}


def _step(signal: dict) -> KBDStep:
    return KBDStep(
        tool_name=signal["acquire"]["tool"],
        tool_args_template=signal["acquire"]["args"],
        matcher=signal.get("match"),
    )


def _diagnostic(ai_client=None) -> KBDDiagnostic:
    registry = MagicMock()
    registry.get_client.return_value = ai_client or MagicMock()
    return KBDDiagnostic(registry, MagicMock(), conversation_id="matrix-conversation", case_id="matrix-case")


@pytest.mark.asyncio
async def test_qfk_log_produce_complete_line_filter_ai_and_variable_pool(monkeypatch):
    """C1：KBD27736 的同记录 AND、排除 OR、AI 摘取和变量池必须闭环。"""

    signal = {
        "id": "qfk-log-duplicate-ip",
        "acquire": {
            "tool": "qfk_log",
            "args": {"file": "sfvt_vtpdaemon.log", "time_window": "2026-08-05 10:11:12", "timeout": 60},
        },
        "match": None,
        "orchestrate": {
            "phase": "diagnostic",
            "produces": [{
                "name": "DUP_IP",
                "type": "string",
                "extract": {
                    "type": "text",
                    "rows": {
                        "mode": "keywords",
                        "scope": "same_record",
                        "include": ["检测到IP", "冲突"],
                        "exclude": ["测试数据", "模拟冲突"],
                        "include_mode": "all",
                        "exclude_mode": "any",
                        "case_sensitive": True,
                    },
                    "cardinality": "all",
                    "source": "stdout",
                    "ai_extract": {"instruction": "提取其中的第一个 IP 地址"},
                },
            }],
            "requires": [],
        },
    }
    validate_signals_json(_document(signal))
    output = (
        "检测到IP ip=192.168.100.1\n"
        "发生冲突 ip=192.168.100.2\n"
        "检测到IP，发生冲突，ip=192.168.100.55\n"
        "检测到IP，模拟冲突，ip=192.168.100.99\n"
    )
    executor = FakeExecutor(output)

    class FakeAIClient:
        async def invoke(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({
                "ok": True,
                "value": "192.168.100.55",
                "evidence_lines": [3],
            }))

    monkeypatch.setattr(executor_module, "_executor", executor)
    diagnostic = _diagnostic(FakeAIClient())

    raw, error, matched, _ai_value = await diagnostic._execute_acquirer(
        _step(signal), {}, "matrix-session", "matrix-user", signal=signal, exec_id="matrix-log",
    )

    assert error is None
    assert matched is True
    assert raw is not None and "192.168.100.55" in raw
    assert diagnostic._variable_pool == {"dup_ip": "192.168.100.55"}
    bridge_args = executor.calls[0]["args"]
    assert bridge_args["command"].startswith("acli log get -E -k ")
    assert "检测到IP" in bridge_args["command"] and "冲突" in bridge_args["command"]
    assert bridge_args["output_filters"] == [{
        "source": "stdout",
        "include": ["检测到IP", "冲突"],
        "exclude": ["测试数据", "模拟冲突"],
        "include_mode": "all",
        "exclude_mode": "any",
        "case_sensitive": True,
    }]


@pytest.mark.asyncio
async def test_qfk_system_match_text_column_threshold(monkeypatch):
    """C2：系统输入、文本表格列取值和阈值 Match 必须得到业务 True。"""

    signal = {
        "id": "qfk-system-df",
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": "df", "command_args": ["-P", "/sf/log"], "timeout": 60},
        },
        "match": {
            "type": "threshold",
            "operator": ">=",
            "value": 80,
            "aggregation": "first_number",
            "expected": True,
            "extract": {
                "type": "text",
                "parser": "whitespace_table",
                "header": {"mode": "contains", "required": ["Filesystem", "Use%"], "case_sensitive": False},
                "rows": {"mode": "all"},
                "columns": [{
                    "key": "USE_PERCENT",
                    "selector": {"by": "header", "name": "Use%"},
                    "value_mode": "number",
                }],
                "value_key": "USE_PERCENT",
                "cardinality": "exactly_one",
                "source": "stdout",
            },
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
    }
    validate_signals_json(_document(signal))
    executor = FakeExecutor("Filesystem Use%\n/dev/sda3 83%\n")
    monkeypatch.setattr(executor_module, "_executor", executor)

    raw, error, matched, _ = await _diagnostic()._execute_acquirer(
        _step(signal), {}, "matrix-session", "matrix-user", signal=signal, exec_id="matrix-system",
    )

    assert error is None
    assert matched is True
    assert raw is not None and "83%" in raw
    assert executor.calls[0]["args"]["command"] == "acli --timeout 60 system df -P /sf/log"


@pytest.mark.asyncio
async def test_qfk_service_match_complete_line_state_and_status_only(monkeypatch):
    """C3：服务输入只编译 status，完整行进入精确 state 判定。"""

    signal = {
        "id": "qfk-service-running",
        "acquire": {
            "tool": "qfk_service",
            "args": {"service": "asv-manager", "container": "asv", "action": "status", "timeout": 60},
        },
        "match": {
            "type": "state",
            "pattern": "running",
            "expected": True,
            "extract": {
                "type": "text", "rows": {"mode": "all"},
                "cardinality": "exactly_one", "source": "stdout", "value_mode": "string",
            },
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
    }
    validate_signals_json(_document(signal))
    executor = FakeExecutor("running\n")
    monkeypatch.setattr(executor_module, "_executor", executor)

    _raw, error, matched, _ = await _diagnostic()._execute_acquirer(
        _step(signal), {}, "matrix-session", "matrix-user", signal=signal, exec_id="matrix-service",
    )

    assert error is None
    assert matched is True
    assert executor.calls[0]["args"]["command"] == "acli service asv asv-manager status"

    invalid = json.loads(json.dumps(signal))
    invalid["acquire"]["args"]["action"] = "restart"
    validate_signals_json(_document(invalid))  # 共享 Signal 契约不误伤 SOP 处置动作。
    with pytest.raises(Exception, match="写操作命令 restart"):
        validate_kbd_read_only_signals_json(_document(invalid))


@pytest.mark.asyncio
async def test_qfk_vm_produce_json_path_and_variable_pool(monkeypatch):
    """C4：VM 输入、JSON 路径取值、类型转换和变量池写入必须闭环。"""

    signal = {
        "id": "qfk-vm-id",
        "acquire": {
            "tool": "qfk_vm",
            "args": {"command": "list --formatter json", "timeout": 60},
        },
        "match": None,
        "orchestrate": {
            "phase": "diagnostic",
            "produces": [{
                "name": "VM_ID",
                "type": "integer",
                "extract": {
                    "type": "json", "path": "data[0].id", "cardinality": "exactly_one",
                    "source": "stdout", "value_mode": "integer",
                },
            }],
            "requires": [],
        },
    }
    validate_signals_json(_document(signal))
    executor = FakeExecutor('{"data":[{"id":27736,"name":"demo-vm"}]}')
    monkeypatch.setattr(executor_module, "_executor", executor)
    diagnostic = _diagnostic()

    _raw, error, matched, _ = await diagnostic._execute_acquirer(
        _step(signal), {}, "matrix-session", "matrix-user", signal=signal, exec_id="matrix-vm",
    )

    assert error is None
    assert matched is True
    assert executor.calls[0]["args"]["command"] == "acli vm list --formatter json"
    assert diagnostic._variable_pool == {"vm_id": 27736}
