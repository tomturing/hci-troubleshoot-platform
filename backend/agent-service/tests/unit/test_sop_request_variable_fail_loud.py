"""运行时变量闸门：未声明变量必须 fail-loud，不再降级为自由输入。

守护 Q2026102600003「填了还要填」的完整因果链：

    acli_exec 被门禁拦截(vmid 缺失)
      -> sop_request_variable(task_logs) 返回依赖缺失
      -> 旧逻辑用 next_tool_call 引导模型去请求 "任务原始数据"（非变量名）
      -> 旧逻辑把未声明变量降级为 needs_input=True，下发输入卡片
      -> 用户填值写入 context_variables["任务原始数据"]（野键）
      -> SOP 永远消费不到 -> 下一轮继续索要

本文件锁死三个行为：
  1. 未声明变量 -> 返回 error，绝不下发 needs_input 卡片；
  2. 中文/非标识符变量名 -> 同样 error（字符级相似度对中英混排恒为 0，
     旧逻辑会把所有中文野变量都放进死路）；
  3. 依赖指向未声明变量 -> 返回配置错误，不再用 next_tool_call 引导模型。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from app.memory.variable_pool.engine import _render_variable_template, sop_request_variable
from app.memory.variable_pool.pool import VariableRequestResult

CONVERSATION_ID = "5fe5cb2c-f767-480b-826d-5c8a9a6c8df1"

#: 线上 SOP#3 的真实形态：task_logs 的依赖列被写成了自然语言
SOP3_BROKEN_SCHEMA = [
    {
        "name": "task_logs",
        "type": "json",
        "acquisition_strategy": "user_input",
        "depends_on": ["任务原始数据"],
        "description": "acli_exec",
    },
    {
        "name": "vmid",
        "type": "string",
        "acquisition_strategy": "skill_call",
        "acquisition_tool": "hci-task-parsing",
        "output_path": "values.vm",
        "depends_on": ["task_logs"],
        "description": "任务虚拟机id",
    },
]


def _kb_client(variable_schema: list[dict]) -> AsyncMock:
    client = AsyncMock()
    client.get_sop_document.return_value = {"variable_schema": variable_schema}
    return client


def _sop_client(pending: str | None = None) -> AsyncMock:
    client = AsyncMock()
    client.get_execution.return_value = {
        "current_node_id": "n-1",
        "context_variables": {},
        "pending_variable_name": pending,
    }
    return client


async def _request(variable_name: str, variable_schema: list[dict]):
    return await sop_request_variable(
        variable_name,
        reason="变量 task_logs 的前置依赖",
        conversation_id=CONVERSATION_ID,
        sop_document_id=3,
        kb_client=_kb_client(variable_schema),
        conversation_sop_client=_sop_client(),
    )


@pytest.mark.asyncio
async def test_undeclared_chinese_variable_is_rejected_not_offered_as_input():
    """核心回归：请求 "任务原始数据" 必须直接 error，不能下发输入卡片。"""
    result = await _request("任务原始数据", SOP3_BROKEN_SCHEMA)

    assert not isinstance(result, VariableRequestResult), "未声明变量不得降级为 needs_input 卡片"
    assert result["error"] == "sop_variable_not_defined"
    assert result.get("config_error") is True
    # 必须把可用变量名回给模型，避免它继续猜
    assert "task_logs" in result["suggested_variables"]


@pytest.mark.asyncio
async def test_undeclared_identifier_variable_is_rejected():
    result = await _request("task_raw", SOP3_BROKEN_SCHEMA)
    assert not isinstance(result, VariableRequestResult)
    assert result["error"] == "sop_variable_not_defined"


@pytest.mark.asyncio
async def test_unresolvable_dependency_returns_config_error_without_next_tool_call():
    """task_logs 依赖了未声明的 "任务原始数据" -> 配置错误，且不得引导模型继续请求。"""
    result = await _request("task_logs", SOP3_BROKEN_SCHEMA)

    assert not isinstance(result, VariableRequestResult)
    assert result["error"] == "sop_schema_invalid_dependency"
    assert result.get("config_error") is True
    assert "任务原始数据" in result["unresolvable_dependencies"]
    # 关键：不能再返回 next_tool_call 把模型推向未声明变量
    assert "next_tool_call" not in result


@pytest.mark.asyncio
async def test_dependency_error_propagates_to_dependent_variable():
    """vmid -> task_logs -> 未声明依赖，错误必须一路上抛且标记 config_error。"""
    result = await _request("vmid", SOP3_BROKEN_SCHEMA)

    assert not isinstance(result, VariableRequestResult)
    assert result["error"] in ("sop_schema_invalid_dependency", "sop_variable_dependency_failed")


@pytest.mark.asyncio
async def test_missing_variable_schema_is_config_error():
    """SOP 完全没有变量声明时，请求变量属于契约违规。"""
    result = await _request("vmid", [])

    assert not isinstance(result, VariableRequestResult)
    assert result["error"] == "sop_variable_schema_missing"
    assert result.get("config_error") is True


@pytest.mark.asyncio
async def test_path_template_renders_from_pool():
    """路径模板类变量（如 qemu_log_path）应由变量池直接拼出，不再弹人工框。"""
    schema = [
        {
            "name": "qemu_log_path",
            "type": "string",
            "acquisition_strategy": "path_template",
            "expression": "/sf/log/{task_day}/sfvt_qemu_{vmid}.log",
            "depends_on": ["task_day", "vmid"],
            "description": "QEMU 日志路径",
        }
    ]
    client = AsyncMock()
    client.get_execution.return_value = {
        "current_node_id": "n-1",
        "context_variables": {
            "task_day": {"value": "2026-10-26"},
            "vmid": {"value": "1001"},
        },
        "pending_variable_name": None,
    }

    result = await sop_request_variable(
        "qemu_log_path",
        reason="定位 QEMU 日志",
        conversation_id=CONVERSATION_ID,
        sop_document_id=3,
        kb_client=_kb_client(schema),
        conversation_sop_client=client,
    )

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["value"] == "/sf/log/2026-10-26/sfvt_qemu_1001.log"
    assert result["source"] == "path_template"


def test_path_template_missing_token_reports_dependencies_not_user_input():
    """占位符未就绪时应报出缺失依赖，而不是让人工手抄路径。

    注意：完整链路里 depends_on 检查在前，缺值会先走递归解析，因此这里直接
    验证渲染函数本身（防御分支），避免构造不可能的 schema 组合。
    """
    rendered, missing = _render_variable_template(
        "/sf/log/{task_day}/sfvt_qemu_{vmid}.log",
        {"task_day": {"value": "2026-10-26"}},
    )
    assert rendered is None
    assert missing == ["vmid"]


@pytest.mark.asyncio
async def test_declared_user_input_variable_still_offers_input_card():
    """正常路径不能被误伤：已声明的 user_input 变量仍应下发输入卡片。"""
    schema = [
        {
            "name": "disk_dev",
            "type": "string",
            "acquisition_strategy": "user_input",
            "description": "故障盘设备名，如 /dev/sdb",
        }
    ]
    result = await _request("disk_dev", schema)

    assert isinstance(result, VariableRequestResult)
    assert result.needs_input is True
    assert result.variable_name == "disk_dev"


# ──────────────────────────────────────────────────────────────────────────────
# tool_call 参数模板类型守卫
# 守护 staging 真实事故（2026-10-26，SOP#3 / conversation a4dd8df1）：
#   acquisition_args_template 被写成 "stdout"（output_path 的值串到了模板列）
#   -> 渲染后原样作为 args 传入执行器
#   -> 执行器内 args.get("node_ip") 抛 AttributeError: 'str' object has no attribute 'get'
#   -> 被"失败即接管"吞成"人工弹卡"，日志里只剩那句 AttributeError，根因不可见
#   -> 模型每重试一次再弹一张卡，同一轮重复下发
# ──────────────────────────────────────────────────────────────────────────────

TOOL_CALL_SCHEMA_TMPL = {
    "name": "task_logs",
    "type": "json",
    "acquisition_strategy": "tool_call",
    "acquisition_tool": "acli_exec",
    "output_path": "stdout",
    "description": "启动虚拟机失败任务原始数据",
}


@pytest.mark.asyncio
async def test_tool_call_string_args_template_stops_before_executor():
    """模板为裸字符串时，必须在调用执行器之前就被拦下（不产生 AttributeError）。"""
    schema = [dict(TOOL_CALL_SCHEMA_TMPL, acquisition_args_template="stdout")]
    executor = AsyncMock()

    result = await sop_request_variable(
        "task_logs",
        reason="获取任务原始数据",
        conversation_id=CONVERSATION_ID,
        sop_document_id=3,
        kb_client=_kb_client(schema),
        conversation_sop_client=_sop_client(),
        tool_executor=executor,
    )

    # 关键：执行器一次都不该被调用——脏参数不允许流到下游
    executor.execute.assert_not_called()
    # 降级语义保持"失败即接管"：仍然给用户一张输入卡
    assert isinstance(result, VariableRequestResult)
    assert result.needs_input is True


@pytest.mark.asyncio
async def test_tool_call_object_args_template_reaches_executor():
    """合法对象模板必须真正下发（守住不误伤正常路径）。"""
    template = {
        "command": 'acli --formatter json task get -k "启动虚拟机" -s \'failed\'',
        "reason": "获取所有“虚拟机开机”相关失败任务原始数据",
    }
    schema = [dict(TOOL_CALL_SCHEMA_TMPL, acquisition_args_template=template)]
    executor = AsyncMock()
    executor.execute.return_value = None

    await sop_request_variable(
        "task_logs",
        reason="获取任务原始数据",
        conversation_id=CONVERSATION_ID,
        sop_document_id=3,
        kb_client=_kb_client(schema),
        conversation_sop_client=_sop_client(),
        tool_executor=executor,
    )

    executor.execute.assert_called_once()
    call = executor.execute.call_args
    assert call.args[0] == "acli_exec"
    assert call.args[1] == template
    assert call.kwargs["conversation_id"] == CONVERSATION_ID
