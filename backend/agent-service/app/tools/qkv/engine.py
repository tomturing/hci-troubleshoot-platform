"""
QKV 前端信号变量提取执行引擎
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any

from shared.observability.langfuse import observe_tool
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from app.tools.acli.executor import exec_result_observation
from app.tools.qkv.parser import parse_frontend_value
from app.tools.qkv.signal import FrontendQueryType, FrontendSignal

logger = get_logger("qkv-engine")


def _dialog_output_without_self_observation(text: str, commands: list[str]) -> str:
    """剔除本次 ``acli log get`` 被 audit_log 记录后反查到的探针自身，并去重。"""

    command_tokens = [command.strip() for command in commands if command.strip()]
    kept: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        normalized = line.strip()
        if not normalized or any(command in normalized for command in command_tokens):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        kept.append(line)
    return "\n".join(kept)


@dataclass
class QKVResult:
    """
    QKV 执行与元数据变量提取最终输出结果
    """

    success: bool  # 查询是否成功执行
    query: str  # 查询类型 (alert/task/dialog)
    keyword: str  # 查询关键字
    command: str  # 底层实际执行指令
    values: list[dict[str, Any]] = field(default_factory=list)  # 提取提取出来的 Value 结果集
    error: str | None = None  # 报错信息描述
    exec_id: str | None = None  # 流水号记录

    def to_observation(self) -> str:
        """
        转换为给 ReAct 决策 Agent 观察的标准化展示格式
        """
        if not self.success:
            return f"QKV 查询异常: {self.error or '未知错误'}"

        status_desc = f"成功查找到 {len(self.values)} 条记录" if self.values else "未找到符合的匹配记录"
        lines = [
            f"QKV 查询状态: {status_desc}",
            f"Q(查询类型): {self.query} | K(关键字): {self.keyword}",
            f"底层命令: {self.command}",
        ]
        if self.values:
            lines.append("\n【提取出的结构化变量数据集 (Values)】")
            # 为防止 ReAct 窗口过载，限制展示部分结果，但返回给内存的数据是完整的
            preview_limit = 5
            for idx, val in enumerate(self.values[:preview_limit]):
                lines.append(f"记录 [{idx + 1}]: {json.dumps(val, ensure_ascii=False)}")
            if len(self.values) > preview_limit:
                lines.append(f"... 还有 {len(self.values) - preview_limit} 条记录已暂存至变量上下文")
        return "\n".join(lines)


def qkv_load(signal_json: dict[str, Any] | str) -> FrontendSignal:
    """
    加载并校验前端信号对象
    """
    if isinstance(signal_json, str):
        return FrontendSignal.from_json(signal_json)
    return FrontendSignal.from_dict(signal_json)


async def qkv_exec(
    signal: FrontendSignal,
    *,
    conversation_id: str,
    node_ip: str | None = None,
    exec_id: str | None = None,
) -> QKVResult:
    """
    运行前端信号提取引擎，执行 acli 并过滤析出特定字段

    Args:
        signal: 验证通过的 FrontendSignal 实例
        conversation_id: 会话标识
        node_ip: 执行目标节点 IP
        exec_id: 流水号追踪

    Returns:
        QKVResult
    """
    # 1. 底层命令构建逻辑
    try:
        quoted_kw = shlex.quote(signal.keyword)
        limit_val = max(1, min(signal.limit, 200))  # 强制区间限制 [1, 200]

        if signal.query == FrontendQueryType.ALERT:
            commands = [f"acli --formatter json alert get -k {quoted_kw} -l {limit_val}"]
        elif signal.query == FrontendQueryType.TASK:
            status_part = " -s failed" if signal.is_failed else ""
            commands = [f"acli --formatter json task get -k {quoted_kw}{status_part} -l {limit_val}"]
        elif signal.query == FrontendQueryType.DIALOG:
            # HCI 没有 dialog CRUD API。弹框的可执行事实源是当前主控的当日日志；
            # /sf/log/today 与 vt 分开查询，兼容 aCLI 对目录递归深度的版本差异。
            commands = [
                f"acli log get -k {quoted_kw} -p {shlex.quote(path)} -c {signal.context_lines}"
                for path in signal.paths
            ]
        else:
            raise ValueError(f"未知的前端信号类型: {signal.query}")
    except Exception as build_err:
        logger.error(event="qkv_command_build_failed", error=str(build_err))
        return QKVResult(
            success=False,
            query=signal.query.value,
            keyword=signal.keyword,
            command="",
            error=f"QKV 命令构建异常: {build_err}",
        )

    logger.info(
        event="qkv_engine_executing",
        query=signal.query.value,
        keyword=signal.keyword,
        command=" && ".join(commands),
    )

    # 2. 复用 BridgeRelayExecutor 执行命令
    from app.tools.acli.executor import _executor

    if _executor is None:
        return QKVResult(
            success=False,
            query=signal.query.value,
            keyword=signal.keyword,
            command=" && ".join(commands),
            error="BridgeRelayExecutor 尚未初始化",
        )

    exec_results = []
    try:
        for cmd in commands:
            tool_args = {"command": cmd, "reason": f"QKV前端变量抽取: {signal.query.value}"}
            with observe_tool(
                tool_name=f"qkv_{signal.query.value}",
                tool_args=tool_args,
                exec_id=exec_id or "",
                session_id=conversation_id,
                risk_level=1,
                trace_id=get_current_trace_id(),
            ) as observation:
                exec_res = await _executor.execute(
                    tool_name="acli_exec",
                    args=tool_args,
                    conversation_id=conversation_id,
                    node_ip=node_ip,
                    risk_level=1,  # 均属只读
                    policy="auto",  # 静默跑
                    exec_id=exec_id,
                )
                if observation:
                    observation.update(output=exec_result_observation(exec_res))
            exec_results.append(exec_res)
    except Exception as exec_err:
        logger.error(
            event="qkv_execution_failed",
            command=" && ".join(commands),
            error=str(exec_err),
        )
        return QKVResult(
            success=False,
            query=signal.query.value,
            keyword=signal.keyword,
            command=" && ".join(commands),
            error=str(exec_err),
        )

    failed = next((item for item in exec_results if item.exit_code not in (0, None)), None)
    if failed is not None:
        error_text = (failed.stderr or failed.stdout or "命令执行失败").strip()
        logger.warning(
            event="qkv_terminal_execution_failed",
            command=" && ".join(commands),
            exit_code=failed.exit_code,
            error=error_text,
        )
        return QKVResult(
            success=False,
            query=signal.query.value,
            keyword=signal.keyword,
            command=" && ".join(commands),
            error=error_text,
            exec_id=getattr(failed, "exec_id", None),
        )

    # 3. 数据结构清洗与提取
    try:
        stdout = "\n".join(item.stdout or "" for item in exec_results)
        if signal.query == FrontendQueryType.DIALOG:
            stdout = _dialog_output_without_self_observation(stdout, commands)
        values = parse_frontend_value(signal.query, stdout, signal.produces)
        if signal.query == FrontendQueryType.DIALOG and node_ip:
            for value in values:
                value.setdefault("host", node_ip)
        values = values[:limit_val]
    except Exception as parse_err:
        logger.error(
            event="qkv_output_parse_exception",
            error=str(parse_err),
            stdout=stdout,
        )
        return QKVResult(
            success=False,
            query=signal.query.value,
            keyword=signal.keyword,
            command=" && ".join(commands),
            error=f"分析提取 JSON 返回值异常: {parse_err}",
            exec_id=getattr(exec_results[0], "exec_id", None) if exec_results else None,
        )

    return QKVResult(
        success=True,
        query=signal.query.value,
        keyword=signal.keyword,
        command=" && ".join(commands),
        values=values,
        exec_id=getattr(exec_results[0], "exec_id", None) if exec_results else None,
    )
