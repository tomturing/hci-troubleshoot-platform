from types import SimpleNamespace
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
    conversation_sop_client.get_execution = AsyncMock(
        return_value={"context_variables": {}, "pending_variable_name": None}
    )

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
    conversation_sop_client.get_execution = AsyncMock(
        return_value={"context_variables": {}, "pending_variable_name": None}
    )

    res = await sop_request_variable(
        variable_name="check_meth",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
    )

    # 依赖 smart_info 不在 variable_schema 中 → 无法自动解析，返回缺失错误
    assert isinstance(res, dict), f"expected dict, got {type(res).__name__}: {res}"
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


@pytest.mark.asyncio
async def test_sop_request_variable_tool_call_renders_args_template_and_extracts_stdout():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "smart_info",
                    "acquisition_strategy": "tool_call",
                    "acquisition_tool": "bash_exec",
                    "acquisition_args_template": {
                        "container": "vs-cp-manager",
                        "command": "smartctl -a /dev/{disk_dev}",
                        "node_ip": "{node_ip}",
                        "reason": "采集 {disk_dev} SMART 原始信息",
                    },
                    "depends_on": ["disk_dev", "node_ip"],
                }
            ],
        }
    )
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "disk_dev": {"value": "sda", "source": "llm_inference"},
                "node_ip": {"value": "SVR_aCloud_670", "source": "skill_call"},
            },
            "pending_variable_name": None,
        }
    )
    tool_executor = MagicMock()
    tool_executor.execute = AsyncMock(
        return_value=SimpleNamespace(stdout="Device Model: SAMSUNG\n177 Wear_Leveling_Count ... 8", stderr="")
    )

    res = await sop_request_variable(
        variable_name="smart_info",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
        tool_executor=tool_executor,
    )

    assert isinstance(res, dict)
    assert res["ok"] is True
    assert res["value"].startswith("Device Model")
    tool_executor.execute.assert_awaited_once_with(
        "bash_exec",
        {
            "container": "vs-cp-manager",
            "command": "smartctl -a /dev/sda",
            "node_ip": "SVR_aCloud_670",
            "reason": "采集 sda SMART 原始信息",
        },
    )


@pytest.mark.asyncio
async def test_sop_request_variable_tool_call_preserves_false_value():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "is_sys_disk",
                    "type": "boolean",
                    "acquisition_strategy": "tool_call",
                    "acquisition_tool": "disk_role_check",
                    "depends_on": ["disk_dev"],
                    "acquisition_args_template": {"disk_dev": "{disk_dev}"},
                }
            ],
        }
    )
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "disk_dev": {"value": "sda", "source": "llm_inference"},
            },
            "pending_variable_name": None,
        }
    )
    tool_executor = MagicMock()
    tool_executor.execute = AsyncMock(return_value={"value": False})

    res = await sop_request_variable(
        variable_name="is_sys_disk",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
        tool_executor=tool_executor,
    )

    assert isinstance(res, dict)
    assert res["ok"] is True
    assert res["value"] is False
    assert res["source"] == "tool_call"


@pytest.mark.asyncio
async def test_sop_request_variable_derived_expression():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "is_sys_disk",
                    "type": "boolean",
                    "acquisition_strategy": "derived",
                    "expression": "contains(alert_type, 'vs') ? false : unknown",
                    "depends_on": ["alert_type"],
                }
            ],
        }
    )
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "alert_type": {"value": "vs_disk_warn", "source": "skill_call"},
            },
            "pending_variable_name": None,
        }
    )

    res = await sop_request_variable(
        variable_name="is_sys_disk",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
    )

    assert isinstance(res, dict)
    assert res["ok"] is True
    assert res["value"] is False
    assert res["source"] == "derived"


@pytest.mark.asyncio
async def test_sop_request_variable_derived_unknown_fails_loud():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "is_sys_disk",
                    "type": "boolean",
                    "acquisition_strategy": "derived",
                    "expression": "contains(alert_type, 'vs') ? false : unknown",
                    "depends_on": ["alert_type"],
                }
            ],
        }
    )
    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "alert_type": {"value": "host_disk_warn", "source": "skill_call"},
            },
            "pending_variable_name": None,
        }
    )

    res = await sop_request_variable(
        variable_name="is_sys_disk",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
    )

    assert isinstance(res, dict)
    assert res["error"] == "sop_derived_variable_acquire_failed"


@pytest.mark.asyncio
async def test_sop_request_variable_json_extract_success_with_cache():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "disk_sn",
                    "type": "string",
                    "acquisition_strategy": "json_extract",
                    "depends_on": ["asan_disks"],
                    "expression": "$.data.disks[?(@.host_name == '{node_hostname}' & @.disk_name == '{disk_name}')].disk_sn",
                }
            ],
        }
    )

    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "node_hostname": {"value": "host-1", "source": "llm_inference"},
                "disk_name": {"value": "1号盘", "source": "user_input"},
                "asan_disks": {
                    "value": "[truncated...]",
                    "exec_id": "exec-abc-123",
                    "source": "tool_call",
                },
            },
            "pending_variable_name": None,
        }
    )

    # Mock Redis client
    mock_redis = MagicMock()
    mock_redis.client.get = AsyncMock(
        return_value=b'{"data": {"disks": [{"host_name": "host-1", "disk_name": "\xe4\xb8\x80\xe5\x8f\xb7\xe7\x9b\xb8", "disk_sn": "SN-REDIS-001"}, {"host_name": "host-1", "disk_name": "1\xe5\x8f\xb7\xe7\x9b\xb8", "disk_sn": "SN-REDIS-001"}, {"host_name": "host-1", "disk_name": "1\xe5\x8f\xb7\xe7\x9b\x94", "disk_sn": "SN-REDIS-001"}, {"host_name": "host-1", "disk_name": "1\xe5\x8f\xb7\xe7\x9b\x98", "disk_sn": "SN-REDIS-001"}]}}'
    )
    tool_executor = MagicMock()
    tool_executor._redis = mock_redis

    res = await sop_request_variable(
        variable_name="disk_sn",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
        tool_executor=tool_executor,
    )

    assert isinstance(res, dict)
    assert res.get("ok") is True
    assert res.get("value") == "SN-REDIS-001"
    assert res.get("source") == "json_extract"
    mock_redis.client.get.assert_awaited_once_with("cmd_cache:exec-abc-123")


@pytest.mark.asyncio
async def test_sop_request_variable_json_extract_fallback_to_value():
    kb_client = MagicMock()
    kb_client.get_sop_document = AsyncMock(
        return_value={
            "id": 2,
            "variable_schema": [
                {
                    "name": "disk_sn",
                    "type": "string",
                    "acquisition_strategy": "json_extract",
                    "depends_on": ["asan_disks"],
                    "expression": "$.data.disks[?(@.host_name == '{node_hostname}' & @.disk_name == '{disk_name}')].disk_sn",
                }
            ],
        }
    )

    conversation_sop_client = MagicMock()
    conversation_sop_client.get_execution = AsyncMock(
        return_value={
            "context_variables": {
                "node_hostname": {"value": "host-1", "source": "llm_inference"},
                "disk_name": {"value": "1号盘", "source": "user_input"},
                "asan_disks": {
                    "value": '{"data": {"disks": [{"host_name": "host-1", "disk_name": "1号盘", "disk_sn": "SN-FALLBACK-002"}]}}',
                    "exec_id": "exec-expired-999",
                    "source": "tool_call",
                },
            },
            "pending_variable_name": None,
        }
    )

    # Redis returns None (cache expired)
    mock_redis = MagicMock()
    mock_redis.client.get = AsyncMock(return_value=None)
    tool_executor = MagicMock()
    tool_executor._redis = mock_redis

    res = await sop_request_variable(
        variable_name="disk_sn",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
        sop_document_id=2,
        kb_client=kb_client,
        conversation_sop_client=conversation_sop_client,
        tool_executor=tool_executor,
    )

    assert isinstance(res, dict)
    assert res.get("ok") is True
    assert res.get("value") == "SN-FALLBACK-002"
    assert res.get("source") == "json_extract"
    mock_redis.client.get.assert_awaited_once_with("cmd_cache:exec-expired-999")
