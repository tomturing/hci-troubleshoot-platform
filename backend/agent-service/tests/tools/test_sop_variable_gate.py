"""
SOP 变量来源运行时门禁测试。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from app.adapters.agents.htp.sop_tools import SopToolExecutor


def _tree() -> dict:
    return {
        "node_id": "n-1",
        "title": "磁盘寿命到期",
        "children": [
            {
                "node_id": "n-1-1",
                "title": "系统盘分支",
                "prerequisites": ["{is_sys_disk} == true"],
                "children": [],
                "diagnosis": {
                    "acli_methods": ['container_exec -n vs-cp-manager -c "smartctl -a /dev/sda"'],
                    "page_methods": [],
                },
            }
        ],
    }


def _kb_client() -> AsyncMock:
    kb_client = AsyncMock()
    kb_client.get_sop_tree.return_value = {"tree": _tree()}
    kb_client.get_sop_document.return_value = {
        "variable_schema": [
            {
                "name": "is_sys_disk",
                "type": "boolean",
                "description": "是否是系统盘",
                "acquisition_strategy": "user_input",
            }
        ]
    }
    return kb_client


def _executor(
    *,
    conversation_id: str,
    context_variables: dict | None = None,
) -> tuple[SopToolExecutor, AsyncMock, AsyncMock]:
    conversation_sop_client = AsyncMock()
    conversation_sop_client.get_execution.return_value = {
        "current_node_id": "n-1",
        "context_variables": context_variables or {},
        "pending_variable_name": None,
    }
    default_executor = AsyncMock()
    default_executor.execute.return_value = {"ok": True, "stdout": "real output"}
    executor = SopToolExecutor(
        sop_document_id=2,
        conversation_id=conversation_id,
        kb_client=_kb_client(),
        conversation_sop_client=conversation_sop_client,
        default_executor=default_executor,
    )
    return executor, conversation_sop_client, default_executor


@pytest.mark.asyncio
async def test_blocks_real_tool_when_user_input_variable_missing():
    conversation_id = str(uuid.uuid4())
    executor, _, default_executor = _executor(conversation_id=conversation_id)

    result = await executor.execute(
        "bash_exec",
        {"container": "vs-cp-manager", "command": "smartctl -a /dev/sda", "reason": "检查系统盘寿命"},
    )

    assert result["ok"] is False
    assert result["error"] == "sop_variable_gate_blocked"
    assert result["missing_variables"][0]["name"] == "is_sys_disk"
    assert result["next_tool_call"]["tool_name"] == "sop_request_variable"
    assert result["next_tool_call"]["args"]["variable_name"] == "is_sys_disk"
    default_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_allows_real_tool_when_required_variable_exists():
    conversation_id = str(uuid.uuid4())
    executor, _, default_executor = _executor(
        conversation_id=conversation_id,
        context_variables={"is_sys_disk": {"value": "true", "source": "user_input"}},
    )

    result = await executor.execute(
        "bash_exec",
        {"container": "vs-cp-manager", "command": "smartctl -a /dev/sda", "reason": "检查系统盘寿命"},
    )

    assert result == {"ok": True, "stdout": "real output"}
    default_executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_allows_sop_navigation_tools_before_variable_ready():
    conversation_id = str(uuid.uuid4())
    executor, _, default_executor = _executor(conversation_id=conversation_id)

    node = await executor.execute("get_sop_node", {"node_id": "n-1"})

    assert node["node_id"] == "n-1"
    assert node["children"][0]["required_variables"][0]["name"] == "is_sys_disk"
    default_executor.execute.assert_not_awaited()
