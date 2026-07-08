"""
QFK 后端信号谓词匹配引擎
负责信号加载与解析，复用 Bridge Relay Executor 进行指令中转下发
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.observability.logger import get_logger

from app.tools.qfk.handlers import HandlerRegistry
from app.tools.qfk.signal import BackendSignal

logger = get_logger("qfk-engine")


@dataclass
class QFKResult:
    """
    QFK 关键信号执行与布尔评判最终输出结果
    """

    matched: bool                        # 核心判定：符合排查信号预期返回 True，不符合返回 False
    signal_type: str                     # 执行信号类型
    commands: list[str]                  # 实际执行的底层 acli 指令列表
    keywords: list[str]                  # K: 对比关键字列表
    match_mode: str                      # 关键字匹配规则 (any / all)
    matched_keywords: list[str]          # 实际检测到匹配命中的关键字
    evidence: str                        # 诊断评估执行的完整证据链（由 handler 生成）
    error: str | None = None             # 异常报错信息描述
    exec_ids: list[str] = field(default_factory=list) # 追踪流流水号记录

    def to_observation(self) -> str:
        """
        转换为给 ReAct 决策 Agent 观察的标准化展示格式
        """
        status_tag = "✅ 符合排查判定" if self.matched else "❌ 不符合排查判定"
        lines = [
            f"QFK 排查状态: {status_tag}",
            f"信号类型: {self.signal_type} | 预期匹配模式: {self.match_mode}",
            f"目标关键字: {self.keywords}",
            f"执行命令: {self.commands}",
        ]
        if self.error:
            lines.append(f"执行异常: {self.error}")
        if self.evidence:
            lines.append("\n" + self.evidence)
        return "\n".join(lines)


def qfk_load(signal_json: dict[str, Any] | str) -> BackendSignal:
    """
    加载并校验结构化后端信号对象。

    Args:
        signal_json: 信号定义字典或 JSON 字符串

    Returns:
        实例化并校验通过的 BackendSignal 对象
    """
    if isinstance(signal_json, str):
        return BackendSignal.from_json(signal_json)
    return BackendSignal.from_dict(signal_json)


async def qfk_exec(
    signal: BackendSignal,
    *,
    conversation_id: str,
    node_ip: str | None = None,
    exec_id: str | None = None,
) -> QFKResult:
    """
    根据给定的标准化后端信号，寻找对应 Handler 执行 acli 底层命令，并自动执行关键字逻辑评估

    Args:
        signal: 已经解析验证的 BackendSignal 对象
        conversation_id: 会话标识（上下文变量传递）
        node_ip: 执行目标节点 IP（可指定，默认在集群主控节点）
        exec_id: 流水号跟踪

    Returns:
        QFKResult
    """
    # 1. 寻找 handler 处理器并构建指令
    try:
        handler = HandlerRegistry.get(signal.signal_type)
        commands = handler.build_commands(signal)
    except Exception as e:
        logger.warning(
            event="qfk_handler_setup_failed",
            signal_type=signal.signal_type.value,
            error=str(e),
        )
        return QFKResult(
            matched=False,
            signal_type=signal.signal_type.value,
            commands=[],
            keywords=signal.keywords,
            match_mode=signal.match_mode,
            matched_keywords=[],
            evidence="",
            error=f"QFK 信号解析与命令构建失败: {e}",
        )

    logger.info(
        event="qfk_engine_executing",
        signal_type=signal.signal_type.value,
        commands=commands,
        keywords=signal.keywords,
        match_mode=signal.match_mode,
        node_ip=node_ip,
        conversation_id=conversation_id,
    )

    # 2. 复用底层 BridgeRelayExecutor 执行命令
    from app.tools.acli.executor import _executor
    if _executor is None:
        return QFKResult(
            matched=False,
            signal_type=signal.signal_type.value,
            commands=commands,
            keywords=signal.keywords,
            match_mode=signal.match_mode,
            matched_keywords=[],
            evidence="",
            error="BridgeRelayExecutor 未启动或尚未完成初始化，请检查服务启动流程",
        )

    results = []
    exec_ids = []
    for cmd in commands:
        try:
            exec_res = await _executor.execute(
                tool_name="acli_exec",
                args={"command": cmd, "reason": f"QFK诊断信号提取执行: {signal.description or ''}"},
                conversation_id=conversation_id,
                node_ip=node_ip,
                risk_level=1,  # QFK 判定均属只读行为，风险为 1
                policy="auto", # 无需前端弹窗，静默自动跑
                exec_id=exec_id,
            )
            results.append(exec_res)
            if exec_res.exec_id:
                exec_ids.append(exec_res.exec_id)
        except Exception as exec_err:
            logger.error(
                event="qfk_bridge_execution_exception",
                command=cmd,
                error=str(exec_err),
            )
            return QFKResult(
                matched=False,
                signal_type=signal.signal_type.value,
                commands=commands,
                keywords=signal.keywords,
                match_mode=signal.match_mode,
                matched_keywords=[],
                evidence=f"在通过 Bridge 执行命令 [{cmd}] 时抛出底层异常: {exec_err}",
                error=str(exec_err),
                exec_ids=exec_ids,
            )

    # 3. 解析结果并做关键字评估
    matched, evidence = handler.evaluate(results, signal.keywords, signal.match_mode)

    # 提取实际命中的关键字，用于后续状态填充
    combined_lower = "\n".join([f"{r.stdout}\n{r.stderr}" for r in results]).lower()
    matched_kws = [kw for kw in signal.keywords if kw.lower() in combined_lower]

    # 4. 根据 expected (预期结果) 做出最终布尔翻转
    # 例如：如果排查项检查"无OOM报错"，expected=False (不期望匹配到关键字)
    # 如果 matched=True (匹配到了报错词)，则最终判定 matched = False (判定异常/不符合正常预期)
    final_matched = matched if signal.expected else not matched

    logger.info(
        event="qfk_engine_finished",
        signal_type=signal.signal_type.value,
        raw_matched=matched,
        final_matched=final_matched,
        matched_keywords=matched_kws,
    )

    return QFKResult(
        matched=final_matched,
        signal_type=signal.signal_type.value,
        commands=commands,
        keywords=signal.keywords,
        match_mode=signal.match_mode,
        matched_keywords=matched_kws,
        evidence=evidence,
        exec_ids=exec_ids,
    )
