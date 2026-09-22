"""qfk_var 变量采集原语的三层门禁回归。

覆盖：
1. Q3 抽取门禁：LLM 生成的 qfk_var 一律剥离（reason_code=tool_restricted），
   由服务端强制执行，不依赖 Prompt 约束。
2. 语义门禁（signal_schema）：qfk_var 豁免 match/produces XOR——produces 必需、
   match 可共存；其余 qfk_* 维持 XOR；qfk_var 的 free shell 管道合法，
   其余 qfk_* 的管道命令仍被拒绝。
3. 参数契约（acquirer_args）：command 必填、NUL 拒绝、stdin/keep_on_failure 类型规整。
"""

from __future__ import annotations

import pytest
from app.routes.extract_signals import _validate_and_collect_signals
from jsonschema import ValidationError
from shared.schemas.acquirer_args import validate_acquire_args
from shared.schemas.signal_schema import validate_publishable_signals_json

# ─── 通用构造 ────────────────────────────────────────────────────────────────

_TEXT_EXTRACT = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "first",
    "source": "stdout",
    "value_mode": "string",
}


def _var_signal(signal_id: str = "var-1", with_match: bool = False) -> dict:
    """构造专家维护语义下的合法 qfk_var 信号（produces 必填、match 可选共存）。"""
    signal = {
        "id": signal_id,
        "acquire": {
            "tool": "qfk_var",
            "args": {
                "command": "nvme list | grep -c nvme",
                "host": "{{HOST}}",
                "timeout": 60,
            },
        },
        "match": None,
        "orchestrate": {
            "phase": "diagnostic",
            "produces": [
                {"name": "NVME_COUNT", "type": "string", "extract": _TEXT_EXTRACT},
            ],
            "requires": ["HOST"],
        },
    }
    if with_match:
        signal["match"] = {
            "type": "threshold",
            "expected": True,
            "value": 0,
            "operator": ">",
            "aggregation": "max",
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "first",
                "source": "stdout",
                "value_mode": "integer",
            },
        }
    return signal


# ─── 1. Q3 抽取门禁：LLM 生成的 qfk_var 强制剥离 ─────────────────────────────


def test_llm_generated_qfk_var_is_rejected_as_tool_restricted():
    candidates = [
        _var_signal(signal_id="ai-var"),
        {
            "id": "good",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机", "is_failed": True}},
            "match": None,
            "orchestrate": {
                "phase": "diagnostic",
                "produces": [{"name": "HOST", "path": "host"}],
                "requires": [],
            },
        },
    ]

    accepted, rejected = _validate_and_collect_signals(
        candidates,
        "kbd:test",
        enforce_kbd_read_only=True,
    )

    assert [item["id"] for item in accepted] == ["good"]
    assert [(item["signal"]["id"], item["reason_code"]) for item in rejected] == [
        ("ai-var", "tool_restricted"),
    ]
    assert "仅允许专家" in rejected[0]["reason"]


def test_qfk_var_reject_reason_code_is_registered():
    # 拒绝码必须登记在 REJECT_REASON_CODES 白名单内（防遗忘回归）。
    from app.routes.extract_signals import REJECT_REASON_CODES

    assert "tool_restricted" in REJECT_REASON_CODES


# ─── 2. 语义门禁：produces 必需 + match 可共存；其余 qfk_* 维持 XOR ───────────


def test_publish_review_accepts_var_signal_with_produces_only():
    document = {"schema_version": 2, "signals": [_var_signal()]}
    validate_publishable_signals_json(document)


def test_publish_review_accepts_var_signal_with_match_and_produces_coexisting():
    # qfk_var 核心语义：落池与判定共存（豁免 XOR 门禁）。
    document = {"schema_version": 2, "signals": [_var_signal(with_match=True)]}
    validate_publishable_signals_json(document)


def test_publish_review_rejects_var_signal_without_produces():
    signal = _var_signal()
    signal["orchestrate"]["produces"] = []

    with pytest.raises(ValidationError, match="qfk_var"):
        validate_publishable_signals_json({"schema_version": 2, "signals": [signal]})


def test_other_qfk_tool_keeps_xor_gate():
    # 回归保护：qfk_system 的 match 与 produces 共存仍必须被拒绝。
    signal = {
        "id": "sys-1",
        "acquire": {"tool": "qfk_system", "args": {"command": "ps", "resource_keyword": "vm"}},
        "match": {
            "type": "keyword",
            "pattern": "ok",
            "expected": True,
            "extract": _TEXT_EXTRACT,
        },
        "orchestrate": {
            "phase": "diagnostic",
            "produces": [{"name": "PS_OUT", "type": "string", "extract": _TEXT_EXTRACT}],
            "requires": [],
        },
    }

    with pytest.raises(ValidationError):
        validate_publishable_signals_json({"schema_version": 2, "signals": [signal]})


def test_var_pipeline_command_passes_contract_but_other_qfk_pipeline_is_rejected():
    # qfk_var：free shell 管道是合法语义（核心特性）。
    ok, error = validate_acquire_args("qfk_var", {"command": "nvme list | grep -c nvme"})
    assert ok, error

    # 其余 qfk_*：管道命令仍是注入黑名单违规。
    ok, error = validate_acquire_args("qfk_system", {"command": "ps | grep vm"})
    assert not ok
    assert "非法字符" in error


# ─── 3. 参数契约：normalize_qfk_var_args 结构性校验 ──────────────────────────


def test_var_args_requires_non_empty_command():
    ok, error = validate_acquire_args("qfk_var", {"command": "   "})
    assert not ok
    assert "非空 command" in error

    ok, error = validate_acquire_args("qfk_var", {})
    assert not ok
    assert "command" in error


def test_var_args_rejects_nul_character():
    ok, error = validate_acquire_args("qfk_var", {"command": "ps\x00 aux"})
    assert not ok
    assert "NUL" in error


def test_var_args_strips_empty_optional_fields_and_rejects_bad_types():
    ok, error = validate_acquire_args(
        "qfk_var",
        {"command": "  lsblk  ", "stdin": "", "keep_on_failure": False},
    )
    assert ok, error

    ok, error = validate_acquire_args("qfk_var", {"command": "lsblk", "stdin": 123})
    assert not ok
    assert "stdin" in error

    ok, error = validate_acquire_args("qfk_var", {"command": "lsblk", "keep_on_failure": "yes"})
    assert not ok
    assert "keep_on_failure" in error


def test_var_args_rejects_unregistered_fields():
    ok, error = validate_acquire_args("qfk_var", {"command": "lsblk", "formatter": "upper"})
    assert not ok
    assert "formatter" in error
