"""
SOP 导航工具的命令意图与变量门禁测试。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from app.tools.sop.nav import find_missing_guarded_variables_for_node_window, get_sop_node, sop_advance


def _tree() -> dict:
    return {
        "node_id": "n-1",
        "title": "磁盘寿命到期",
        "children": [
            {
                "node_id": "n-1-1",
                "title": "系统盘",
                "prerequisites": ["{is_sys_disk} == true"],
                "children": [],
                "diagnosis": {
                    "acli_methods": ['container_exec -n vs-cp-manager -c "smartctl -a /dev/sda"'],
                    "page_methods": [],
                },
            },
            {
                "node_id": "n-1-2",
                "title": "普通命令检查",
                "children": [],
                "diagnosis": {
                    "acli_methods": ["ls -h"],
                    "page_methods": [],
                },
            },
        ],
    }


def _kb_client():
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


@pytest.mark.asyncio
async def test_get_sop_node_returns_tool_calls_and_child_required_variables():
    result = await get_sop_node("n-1", sop_document_id=2, kb_client=_kb_client())

    child = result["children"][0]
    assert child["required_variables"][0]["name"] == "is_sys_disk"
    assert child["required_variables"][0]["acquisition_strategy"] == "user_input"


@pytest.mark.asyncio
async def test_get_sop_node_normalizes_container_exec_command():
    result = await get_sop_node("n-1-1", sop_document_id=2, kb_client=_kb_client())

    assert result["commands"] == ['container_exec -n vs-cp-manager -c "smartctl -a /dev/sda"']
    assert result["tool_calls"][0]["tool_name"] == "bash_exec"
    assert result["tool_calls"][0]["args"]["container"] == "vs-cp-manager"
    assert result["tool_calls"][0]["args"]["command"] == "smartctl -a /dev/sda"


@pytest.mark.asyncio
async def test_get_sop_node_defaults_naked_command_to_host():
    result = await get_sop_node("n-1-2", sop_document_id=2, kb_client=_kb_client())

    assert result["tool_calls"][0]["tool_name"] == "bash_exec"
    assert result["tool_calls"][0]["args"]["container"] == "host"
    assert result["tool_calls"][0]["args"]["command"] == "ls -h"


@pytest.mark.asyncio
async def test_sop_advance_blocks_missing_user_input_variable():
    conversation_id = str(uuid.uuid4())
    conversation_sop_client = AsyncMock()
    conversation_sop_client.get_execution.return_value = {"context_variables": {}}

    result = await sop_advance(
        target_node_id="n-1-1",
        reasoning="进入系统盘分支",
        conversation_id=conversation_id,
        sop_document_id=2,
        kb_client=_kb_client(),
        conversation_sop_client=conversation_sop_client,
    )

    assert result["ok"] is False
    assert result["error"] == "missing_required_variables"
    assert result["missing_variables"][0]["name"] == "is_sys_disk"
    assert result["next_tool_call"]["tool_name"] == "sop_request_variable"
    conversation_sop_client.advance.assert_not_awaited()


@pytest.mark.asyncio
async def test_sop_advance_allows_when_required_variable_exists():
    conversation_id = str(uuid.uuid4())
    conversation_sop_client = AsyncMock()
    conversation_sop_client.get_execution.return_value = {
        "context_variables": {"is_sys_disk": {"value": "true", "source": "user_input"}}
    }
    conversation_sop_client.advance.return_value = {"ok": True}

    result = await sop_advance(
        target_node_id="n-1-1",
        reasoning="用户确认系统盘",
        conversation_id=conversation_id,
        sop_document_id=2,
        kb_client=_kb_client(),
        conversation_sop_client=conversation_sop_client,
    )

    assert result["ok"] is True
    conversation_sop_client.advance.assert_awaited_once()


def test_variable_gate_window_does_not_scan_deep_future_branch():
    current_node = {
        "node_id": "n-1",
        "title": "根节点",
        "children": [
            {
                "node_id": "n-1-1",
                "title": "直接分支",
                "prerequisites": ["{is_sys_disk} == true"],
                "children": [],
            }
        ],
    }
    variable_schema = [
        {"name": "is_sys_disk", "type": "boolean", "acquisition_strategy": "user_input"},
    ]

    # 非叶子节点不应把子分支的前置变量合并进来提前阻断
    missing = find_missing_guarded_variables_for_node_window(
        current_node=current_node,
        variable_schema=variable_schema,
        context_variables={},
    )
    assert missing == []

    # 叶子节点本身所需的变量正常校验
    leaf_node = current_node["children"][0]
    missing_leaf = find_missing_guarded_variables_for_node_window(
        current_node=leaf_node,
        variable_schema=variable_schema,
        context_variables={},
    )
    assert [item["name"] for item in missing_leaf] == ["is_sys_disk"]
