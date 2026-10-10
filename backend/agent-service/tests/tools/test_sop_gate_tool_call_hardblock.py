"""
SOP 编排「守卫门扩展 + 终局反幻觉门」测试（修复 B + 修复 C，V-017）。

覆盖 Q2026100812343 复盘：模型绕过受控采集、手工敲裸命令并带幻觉数值下结论。
  - 声明为 tool_call/skill_call 但未解析的变量，必须由软推荐升级为硬阻断，
    使裸 bash_exec/acli_exec/qkv_* 被 sop_variable_gate_blocked 拦截；
  - 推进到结论节点时若存在未解析声明变量，sop_advance 必须返回
    sop_conclusion_blocked_unresolved_variables，禁止生成定量结论。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from app.adapters.agents.htp.sop_tools import SopToolExecutor
from app.tools.sop.nav import (
    _find_missing_guarded_variables,
    _is_conclusion_node,
    find_missing_guarded_variables_for_node_window,
    sop_advance,
)


def _schema() -> list[dict]:
    return [
        {
            "name": "disk_dev",
            "type": "string",
            "description": "故障盘设备名",
            "acquisition_strategy": "tool_call",
            "acquisition_tool": "acli_exec",
        },
        {
            "name": "check_meth",
            "type": "string",
            "description": "健康检查方法",
            "acquisition_strategy": "skill:hci-disk-health-checker",
        },
    ]


def _tree() -> dict:
    return {
        "node_id": "n-1",
        "title": "硬盘坏道",
        "children": [
            {
                "node_id": "n-1-1",
                "title": "确认坏道盘",
                "prerequisites": ["{disk_dev} 已确认", "{check_meth} 已获取"],
                "children": [],
                "solution": {"thorough_fix": ["更换磁盘 {disk_dev}"]},
            }
        ],
    }


# ── 修复 B：unit-level 守卫门 ──────────────────────────────────────────────
def test_tool_call_and_skill_call_upgraded_to_hard_blocked():
    required = [
        {"name": "disk_dev", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec"},
        {"name": "check_meth", "acquisition_strategy": "skill:hci-disk-health-checker"},
        {"name": "hint", "acquisition_strategy": "llm_inference"},
    ]
    hard, soft = _find_missing_guarded_variables(required, context_variables={})
    hard_names = {v["name"] for v in hard}
    soft_names = {v["name"] for v in soft}
    assert hard_names == {"disk_dev", "check_meth"}, "tool_call/skill_call 必须硬阻断"
    assert soft_names == {"hint"}, "llm_inference 仍为软提示"


def test_resolved_tool_call_variable_not_blocked():
    required = [{"name": "disk_dev", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec"}]
    hard, _ = _find_missing_guarded_variables(
        required, context_variables={"disk_dev": {"value": "/dev/sdb", "source": "tool_call"}}
    )
    assert hard == []


def test_conclusion_window_blocks_unresolved_tool_call_variable():
    missing = find_missing_guarded_variables_for_node_window(
        current_node=_tree()["children"][0],
        variable_schema=_schema(),
        context_variables={},
    )
    assert {v["name"] for v in missing} == {"disk_dev", "check_meth"}


# ── 修复 B：SopToolExecutor 运行前门禁 ─────────────────────────────────────
def _executor(current_node_id: str, context_variables: dict | None = None) -> tuple[SopToolExecutor, AsyncMock]:
    kb_client = AsyncMock()
    kb_client.get_sop_tree.return_value = {"tree": _tree()}
    kb_client.get_sop_document.return_value = {"variable_schema": _schema()}
    sop_client = AsyncMock()
    sop_client.get_execution.return_value = {
        "current_node_id": current_node_id,
        "context_variables": context_variables or {},
        "pending_variable_name": None,
    }
    default_executor = AsyncMock()
    default_executor.execute.return_value = {"ok": True, "stdout": "real"}
    executor = SopToolExecutor(
        sop_document_id=4,
        conversation_id=str(uuid.uuid4()),
        kb_client=kb_client,
        conversation_sop_client=sop_client,
        default_executor=default_executor,
    )
    return executor, default_executor


@pytest.mark.asyncio
async def test_bare_tool_blocked_when_tool_call_variable_missing():
    executor, default_executor = _executor(current_node_id="n-1-1")

    result = await executor.execute(
        "acli_exec",
        {"command": "acli disk list", "reason": "手工找坏道盘"},
    )

    assert result["ok"] is False
    assert result["error"] == "sop_variable_gate_blocked"
    assert {v["name"] for v in result["missing_variables"]} == {"disk_dev", "check_meth"}
    assert result["next_tool_call"]["tool_name"] == "sop_request_variable"
    default_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bare_tool_allowed_when_tool_call_variables_resolved():
    executor, default_executor = _executor(
        current_node_id="n-1-1",
        context_variables={
            "disk_dev": {"value": "/dev/sdb", "source": "tool_call"},
            "check_meth": {"value": "smartctl", "source": "skill_call"},
        },
    )

    result = await executor.execute("acli_exec", {"command": "acli disk list", "reason": "确认"})

    assert result == {"ok": True, "stdout": "real"}
    default_executor.execute.assert_awaited_once()


# ── 修复 C：终局反幻觉门 ────────────────────────────────────────────────────
def test_is_conclusion_node_classification():
    assert _is_conclusion_node({"solution": {"x": 1}}, "solution") is True
    assert _is_conclusion_node({"children": [], "solution": {"x": 1}}, None) is True
    assert _is_conclusion_node({"children": [{"node_id": "c"}], "solution": {"x": 1}}, "branch") is False


@pytest.mark.asyncio
async def test_sop_advance_conclusion_blocked_unresolved_variables():
    kb_client = AsyncMock()
    kb_client.get_sop_tree.return_value = {"tree": _tree()}
    kb_client.get_sop_document.return_value = {"variable_schema": _schema()}
    sop_client = AsyncMock()
    sop_client.get_execution.return_value = {
        "current_node_id": "n-1",
        "context_variables": {},
        "pending_variable_name": None,
    }

    result = await sop_advance(
        "n-1-1",
        "证据充分可下结论",
        conversation_id=str(uuid.uuid4()),
        sop_document_id=4,
        kb_client=kb_client,
        conversation_sop_client=sop_client,
    )

    assert result["ok"] is False
    assert result["error"] == "sop_conclusion_blocked_unresolved_variables"
    assert result["escalation"] == "human_confirm"
    assert result["next_tool_call"]["tool_name"] == "sop_request_variable"
    assert {v["name"] for v in result["missing_variables"]} == {"disk_dev", "check_meth"}
