"""
SOP 变量自动取值「守卫门 + 失败即接管」测试（修复 A + 修复 B，V-017）。

覆盖 Q2026100812343 复盘根因：
  - tool_call 取值必须向执行器透传 conversation_id（缺失即 contract_error）；
  - 采集失败一律「失败即接管」转人工弹框（VariableRequestResult(needs_input=True)），
    绝不把控制权以 dict 错误形式交回模型即兴敲命令；
  - 泛化异常按类型分流为可判别的 error_type，不再伪装成笼统取值失败。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from app.memory.variable_pool.engine import (
    _classify_acquisition_error,
    _classify_tool_result_error,
    sop_request_variable,
)
from app.memory.variable_pool.pool import VariableRequestResult


def _kb_client(tool: str = "acli_exec") -> AsyncMock:
    kb_client = AsyncMock()
    kb_client.get_sop_document.return_value = {
        "variable_schema": [
            {
                "name": "disk_dev",
                "type": "string",
                "description": "故障盘设备名",
                "acquisition_strategy": "tool_call",
                "acquisition_tool": tool,
            }
        ]
    }
    return kb_client


def _sop_client(
    pending_variable_name: str | None = None,
    context_variables: dict | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.get_execution.return_value = {
        "current_node_id": "n-1",
        "context_variables": context_variables or {},
        "pending_variable_name": pending_variable_name,
    }
    return client


async def _request(tool_executor):
    conversation_id = str(uuid.uuid4())
    return await sop_request_variable(
        "disk_dev",
        reason="定位坏道盘",
        conversation_id=conversation_id,
        sop_document_id=4,
        kb_client=_kb_client(),
        conversation_sop_client=_sop_client(),
        tool_executor=tool_executor,
    )


@pytest.mark.asyncio
async def test_tool_call_passes_conversation_id_on_success():
    executor = AsyncMock()
    executor.execute.return_value = {"disk_dev": "/dev/sdb"}

    result = await _request(executor)

    # 透传 conversation_id 是关键修复点：execute 必须以关键字收到 conversation_id
    assert executor.execute.await_count == 1
    call = executor.execute.await_args
    assert call.kwargs.get("conversation_id"), "tool_call 必须透传 conversation_id"
    assert call.args[0] == "acli_exec"
    assert result == {"ok": True, "value": "/dev/sdb", "source": "tool_call"}


@pytest.mark.asyncio
async def test_tool_call_failure_escalates_to_human_not_model():
    # 采集异常（如对端不可达）→ 失败即接管转人工，返回 VariableRequestResult 而非 dict 错误
    executor = AsyncMock()
    executor.execute.side_effect = RuntimeError("node 10.0.0.5 unreachable")

    result = await _request(executor)

    assert isinstance(result, VariableRequestResult)
    assert result.needs_input is True
    assert result.variable_name == "disk_dev"
    assert "node_unreachable" in result.message  # 精确 error_type 落入转人工文案


@pytest.mark.asyncio
async def test_tool_call_empty_result_escalates_to_human():
    # 工具执行成功但未取到值 → 仍转人工，禁止把兜底权交回模型
    executor = AsyncMock()
    executor.execute.return_value = {}

    result = await _request(executor)

    assert isinstance(result, VariableRequestResult)
    assert result.needs_input is True
    assert "empty_value" in result.message


@pytest.mark.asyncio
async def test_tool_call_missing_executor_escalates_to_human():
    result = await _request(None)

    assert isinstance(result, VariableRequestResult)
    assert result.needs_input is True
    assert "executor_unavailable" in result.message


def test_classify_acquisition_error_mapping():
    cases = {
        "missing 1 required positional argument: 'conversation_id'": "contract_error",
        "jq: error: ONIGURUMA regex not supported": "tool_capability_missing",
        "acli: command not found": "tool_capability_missing",
        "operation timed out after 30s": "timeout",
        "connection refused to node": "node_unreachable",
        "无对应执行器": "executor_unavailable",
    }
    for text, expected in cases.items():
        assert _classify_acquisition_error(RuntimeError(text)) == expected, text
    # TypeError/ValueError 归一为契约错误
    assert _classify_acquisition_error(TypeError("bad args")) == "contract_error"


def test_classify_tool_result_error_mapping():
    assert _classify_tool_result_error({"error": "缺少 conversation_id"}) == "contract_error"
    assert _classify_tool_result_error({"error": "jq ONIGURUMA unsupported"}) == "tool_capability_missing"
    assert _classify_tool_result_error({"error": "unreachable host"}) == "node_unreachable"

    class _Res:
        exit_code = 2
        stderr = "boom"
        timed_out = False

    assert _classify_tool_result_error(_Res()) == "tool_error"
    assert _classify_tool_result_error({}) == "empty_value"


# ─────────────────────────────────────────────────────────────────────────
# device_absent（V-017 §4）：盘符为空/内核无此设备，区别于命令缺失/工具能力缺失
# ─────────────────────────────────────────────────────────────────────────


def test_classify_acquisition_error_device_absent():
    for text in (
        "smartctl: Unable to open /dev/sdb: No such device",
        "cannot open /dev/sdb: No such device or address",
        "目标磁盘不存在",
    ):
        assert _classify_acquisition_error(RuntimeError(text)) == "device_absent", text
    # 反例：命令本身缺失仍归工具能力缺失，不得误判为设备缺失
    assert _classify_acquisition_error(RuntimeError("acli: command not found")) == "tool_capability_missing"


def test_classify_tool_result_error_device_absent():
    # 退出码非零但语义是"盘没了"→ 优先归 device_absent，不得笼统归 tool_error
    assert (
        _classify_tool_result_error({"error": "Unable to open /dev/sdb: No such device", "exit_code": 1})
        == "device_absent"
    )

    class _MissingDisk:
        exit_code = 1
        stderr = ""
        stdout = "Smartctl open device: /dev/sddc failed: No such device"
        timed_out = False

    assert _classify_tool_result_error(_MissingDisk()) == "device_absent"


@pytest.mark.asyncio
async def test_tool_call_device_absent_escalates_with_precise_error_type():
    # 采集设备已摘除 → 失败即接管转人工，文案携带精确 device_absent 判别
    executor = AsyncMock()
    executor.execute.return_value = {"error": "Unable to open /dev/sdb: No such device"}

    result = await _request(executor)

    assert isinstance(result, VariableRequestResult)
    assert result.needs_input is True
    assert "device_absent" in result.message


# ─────────────────────────────────────────────────────────────────────────
# 失败即接管「维持 pending」回归（V-017 §3.2：人工超时/未补值不伪造、不推进）
# ─────────────────────────────────────────────────────────────────────────


async def _request_with_client(executor, sop_client):
    conversation_id = str(uuid.uuid4())
    return await sop_request_variable(
        "disk_dev",
        reason="定位坏道盘",
        conversation_id=conversation_id,
        sop_document_id=4,
        kb_client=_kb_client(),
        conversation_sop_client=sop_client,
        tool_executor=executor,
    )


@pytest.mark.asyncio
async def test_pending_variable_without_value_stays_pending_no_fabrication():
    # 模拟人工超时：变量处于 pending 且上下文仍无值 → 再次请求仍转人工，绝不伪造 ok/数值
    executor = AsyncMock()
    executor.execute.return_value = {}  # 取值为空
    client = _sop_client(pending_variable_name="disk_dev", context_variables={})

    result = await _request_with_client(executor, client)

    assert isinstance(result, VariableRequestResult)
    assert result.needs_input is True
    assert not isinstance(result, dict) or result.get("ok") is not True


@pytest.mark.asyncio
async def test_human_provided_value_resumes_from_cache():
    # 模拟人工补值续跑：上下文已有值 → 缓存命中直接返回 ok，不再弹框
    client = _sop_client(
        pending_variable_name="disk_dev",
        context_variables={"disk_dev": {"value": "/dev/sdb"}},
    )

    result = await _request_with_client(AsyncMock(), client)

    assert isinstance(result, dict)
    assert result == {"ok": True, "value": "/dev/sdb", "source": "cached", "message": "变量 disk_dev 已有值：/dev/sdb"}
