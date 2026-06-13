from unittest.mock import AsyncMock, MagicMock

import pytest
from app.memory.variable_pool.engine import sop_request_variable


@pytest.mark.asyncio
async def test_sop_request_variable_skill_call():
    # Mock KBClient
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "check_meth",
                    "acquisition_strategy": "skill_call",
                    "acquisition_tool": "disk_vendor_lifetime",
                }
            ],
        }
    )

    # Mock ConversationSopClient
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "smart_info": "Device Model: SAMSUNG MZ7LM240\n177 Wear_Leveling_Count 0x0013 008 008 000 Old_age Always - 8"
            },
            "pending_variable_name": None,
        }
    )
    skill_runner = MagicMock()
    skill_runner.execute = AsyncMock(
        return_value={
            "ok": True,
            "value": "返修",
            "source": "dynamic_skill",
            "skill_name": "hci-disk-vendor-lifetime",
        }
    )

    # Execute
    res = await sop_request_variable(
        variable_name="check_meth",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
        skill_runner=skill_runner,
    )

    # Validate
    assert isinstance(res, dict)
    assert res.get("ok") is True
    assert res.get("value") == "返修"
    assert res.get("source") == "skill_call"
    skill_runner.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_sop_request_variable_skill_call_requires_dynamic_runner():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {"name": "node_ip", "acquisition_strategy": "skill_call", "acquisition_tool": "hci-alert-parsing"}
            ],
        }
    )
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(return_value={"context_variables": {}, "pending_variable_name": None})

    res = await sop_request_variable(
        variable_name="node_ip",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
    )

    assert isinstance(res, dict)
    assert res["error"] == "sop_dynamic_skill_runner_missing"


@pytest.mark.asyncio
async def test_sop_request_variable_env_strategy():
    # 模拟 env:xxx 获取策略
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [{"name": "node_ip", "acquisition_strategy": "env:node_ip"}],
        }
    )

    # 1. 测试变量已经在 context_variables 中存在的情况（缓存命中）
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {"node_ip": {"value": "192.168.1.100", "source": "env_context"}},
            "pending_variable_name": None,
        }
    )

    res = await sop_request_variable(
        variable_name="node_ip",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
    )
    assert isinstance(res, dict)
    assert res.get("ok") is True
    assert res.get("value") == "192.168.1.100"
    assert res.get("source") == "cached"

    # 2. 测试变量在 context_variables 中不存在的情况（自动来源应显式失败）
    conversation_sop_client_missing = MagicMock()
    conversation_sop_client_missing.get_execution = AsyncMock(
        return_value={
            "context_variables": {},
            "pending_variable_name": None,
        }
    )
    conversation_sop_client_missing.interrupt = AsyncMock(return_value={"ok": True})

    res_missing = await sop_request_variable(
        variable_name="node_ip",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client_missing,
    )

    assert isinstance(res_missing, dict)
    assert res_missing["error"] == "sop_env_variable_missing"


@pytest.mark.asyncio
async def test_sop_request_variable_dependency_missing():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "check_meth",
                    "acquisition_strategy": "skill_call",
                    "acquisition_tool": "hci-disk-vendor-lifetime",
                    "depends_on": ["smart_info"],
                }
            ],
        }
    )
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(return_value={"context_variables": {}, "pending_variable_name": None})

    res = await sop_request_variable(
        variable_name="check_meth",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
    )

    assert isinstance(res, dict)
    assert res["error"] == "sop_variable_dependency_missing"
    assert res["missing_dependencies"] == ["smart_info"]


@pytest.mark.asyncio
async def test_sop_request_variable_skill_call_uses_unwrapped_fact_sources():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "node_ip",
                    "acquisition_strategy": "skill_call",
                    "acquisition_tool": "hci-alert-parsing",
                    "depends_on": ["alert_logs"],
                    "output_path": "values.node_ip",
                }
            ],
        }
    )
    alert_logs = [{"target": "SVR_aCloud_670", "description": "磁盘寿命异常"}]
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "alert_logs": {
                    "value": alert_logs,
                    "source": "environment_context",
                }
            },
            "pending_variable_name": None,
        }
    )
    skill_runner = MagicMock()
    skill_runner.execute = AsyncMock(
        return_value={
            "ok": True,
            "value": "SVR_aCloud_670",
            "source": "dynamic_skill",
            "skill_name": "hci-alert-parsing",
        }
    )

    res = await sop_request_variable(
        variable_name="node_ip",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
        skill_runner=skill_runner,
    )

    assert isinstance(res, dict)
    assert res["ok"] is True
    skill_runner.execute.assert_awaited_once()
    assert skill_runner.execute.await_args.args[1]["alert_logs"] == alert_logs
