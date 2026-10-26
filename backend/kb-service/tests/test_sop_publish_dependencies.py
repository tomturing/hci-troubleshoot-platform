"""
SOP 发布及变量编辑中工具/技能可用性校验单元测试
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.routes.admin import enforce_variable_schema_contract
from fastapi import HTTPException


@pytest.mark.anyio
async def test_validate_no_dependencies():
    """测试当没有变量声明为 tool_call/skill_call 时，直接通过校验且不执行查询"""
    mock_session = AsyncMock()

    variable_schema = [
        {"name": "node_ip", "acquisition_strategy": "env_injection", "acquisition_tool": "node_ip"},
        {"name": "vm_name", "acquisition_strategy": "user_input"},
    ]

    # 应该直接通过，不抛出异常，不执行 database 查询
    await enforce_variable_schema_contract(mock_session, variable_schema)
    mock_session.execute.assert_not_called()


@pytest.mark.anyio
async def test_validate_all_dependencies_active():
    """测试所有依赖的工具和技能都存在且启用时通过校验"""
    mock_session = AsyncMock()

    # 模拟工具返回结果
    mock_tool_res = MagicMock()
    mock_tool_res.scalars.return_value.all.return_value = ["acli_exec"]

    # 模拟技能返回结果
    mock_skill_res = MagicMock()
    mock_skill_res.scalars.return_value.all.return_value = ["hci-alert-parsing"]

    mock_session.execute.side_effect = [mock_tool_res, mock_skill_res]

    variable_schema = [
        {"name": "vm_list", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec"},
        {
            "name": "alert_info",
            "acquisition_strategy": "skill_call",
            "acquisition_tool": "hci-alert-parsing",
            "output_path": "values.info",
        },
    ]

    # 应该通过校验，不抛出异常
    await enforce_variable_schema_contract(mock_session, variable_schema)
    assert mock_session.execute.call_count == 2


@pytest.mark.anyio
async def test_validate_missing_tool():
    """测试依赖的工具不存在时抛出 422 错误"""
    mock_session = AsyncMock()

    # 模拟查询工具返回为空（说明工具未启用或不存在）
    mock_tool_res = MagicMock()
    mock_tool_res.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_tool_res

    variable_schema = [
        {"name": "vm_list", "acquisition_strategy": "tool_call", "acquisition_tool": "non_existent_tool"},
    ]

    with pytest.raises(HTTPException) as exc_info:
        await enforce_variable_schema_contract(mock_session, variable_schema)

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["error"] == "missing_dependencies"
    assert "工具：non_existent_tool" in detail["message"]
    assert "non_existent_tool" in detail["missing_tools"]
    assert len(detail["validation_issues"]) == 1
    assert detail["validation_issues"][0]["level"] == "error"
    assert "non_existent_tool" in detail["validation_issues"][0]["message"]


@pytest.mark.anyio
async def test_validate_missing_skill():
    """测试依赖的技能不存在时抛出 422 错误"""
    mock_session = AsyncMock()

    # 模拟查询技能返回为空
    mock_skill_res = MagicMock()
    mock_skill_res.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_skill_res

    variable_schema = [
        {
            "name": "alert_info",
            "acquisition_strategy": "skill_call",
            "acquisition_tool": "non_existent_skill",
            "output_path": "values.info",
        },
    ]

    with pytest.raises(HTTPException) as exc_info:
        await enforce_variable_schema_contract(mock_session, variable_schema)

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["error"] == "missing_dependencies"
    assert "技能：non_existent_skill" in detail["message"]
    assert "non_existent_skill" in detail["missing_skills"]
    assert len(detail["validation_issues"]) == 1
    assert detail["validation_issues"][0]["level"] == "error"
    assert "non_existent_skill" in detail["validation_issues"][0]["message"]


@pytest.mark.anyio
async def test_validate_multiple_missing():
    """测试多个工具和技能同时缺失时在错误中完整列出"""
    mock_session = AsyncMock()

    # 模拟工具查询仅返回 acli_exec，因此 missing_tool = "non_existent_tool"
    mock_tool_res = MagicMock()
    mock_tool_res.scalars.return_value.all.return_value = ["acli_exec"]

    # 模拟技能查询返回空，因此 missing_skill = "non_existent_skill"
    mock_skill_res = MagicMock()
    mock_skill_res.scalars.return_value.all.return_value = []

    mock_session.execute.side_effect = [mock_tool_res, mock_skill_res]

    variable_schema = [
        {"name": "vm_list", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec"},
        {"name": "disk_status", "acquisition_strategy": "tool_call", "acquisition_tool": "non_existent_tool"},
        {
            "name": "alert_info",
            "acquisition_strategy": "skill_call",
            "acquisition_tool": "non_existent_skill",
            "output_path": "values.info",
        },
    ]

    with pytest.raises(HTTPException) as exc_info:
        await enforce_variable_schema_contract(mock_session, variable_schema)

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["error"] == "missing_dependencies"
    assert "non_existent_tool" in detail["missing_tools"]
    assert "non_existent_skill" in detail["missing_skills"]
    assert len(detail["validation_issues"]) == 2


@pytest.mark.anyio
async def test_contract_gate_rejects_undeclared_dependency_before_resource_check():
    """结构契约必须先于资源查询生效。

    历史缺陷：函数只查工具/技能注册，depends_on 悬空（"任务原始数据"）完全
    不校验，导致 Q2026102600003 的坏配置一路发布到线上。这里锁死两点：
      1) 悬空依赖必须 422；
      2) 校验在触达 DB 之前就失败（不浪费连接，也说明是纯结构判定）。
    """
    mock_session = AsyncMock()
    variable_schema = [
        {
            "name": "task_logs",
            "acquisition_strategy": "tool_call",
            "acquisition_tool": "acli_exec",
            "output_path": "stdout",
            "depends_on": ["任务原始数据"],
        }
    ]

    with pytest.raises(HTTPException) as exc_info:
        await enforce_variable_schema_contract(mock_session, variable_schema, mode="publish")

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["error"] == "variable_schema_contract_violation"
    assert any(i["code"] == "SOP_VAR_DEP_NOT_IDENTIFIER" for i in detail["validation_issues"])
    mock_session.execute.assert_not_called()
