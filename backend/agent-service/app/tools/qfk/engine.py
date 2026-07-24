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
    namespace: str                     # 执行信号类型
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
            f"信号类型: {self.namespace} | 预期匹配模式: {self.match_mode}",
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
    case_id: str | None = None,
    exec_id: str | None = None,
) -> QFKResult:
    """
    根据给定的标准化后端信号，寻找对应 Handler 执行 acli 底层命令，并自动执行关键字逻辑评估

    Args:
        signal: 已经解析验证的 BackendSignal 对象
        conversation_id: 会话标识（上下文变量传递）
        node_ip: 执行目标节点 IP（可指定，默认在集群主控节点）
        case_id: 工单 ID（可选；缺失时由 conversation-service 从会话解析，对齐 B1/B2 修复）
        exec_id: 流水号跟踪

    Returns:
        QFKResult
    """
    # 1. 寻找 handler 处理器并构建指令
    try:
        handler = HandlerRegistry.get(signal.namespace)
        commands = handler.build_commands(signal)
    except Exception as e:
        logger.warning(
            event="qfk_handler_setup_failed",
            namespace=signal.namespace,
            error=str(e),
        )
        return QFKResult(
            matched=False,
            namespace=signal.namespace,
            commands=[],
            keywords=signal.keyword,
            match_mode=signal.match_mode,
            matched_keywords=[],
            evidence="",
            error=f"QFK 信号解析与命令构建失败: {e}",
        )

    logger.info(
        event="qfk_engine_executing",
        namespace=signal.namespace,
        commands=commands,
        keywords=signal.keyword,
        match_mode=signal.match_mode,
        node_ip=node_ip,
        conversation_id=conversation_id,
    )

    # 2. 复用底层 BridgeRelayExecutor 执行命令
    from app.tools.acli.executor import _executor
    if _executor is None:
        # 注意：此处的 None 仅表示 agent-service 进程内部的全局执行器未注入
        # （lifespan 未调用 set_executor），并不代表 terminal_bridge / SSH 链路异常。
        # 切勿据此误判为"终端桥未启动"而去重启 terminal_bridge。
        return QFKResult(
            matched=False,
            namespace=signal.namespace,
            commands=commands,
            keywords=signal.keyword,
            match_mode=signal.match_mode,
            matched_keywords=[],
            evidence="",
            error=(
                "诊断服务端 BridgeRelayExecutor 全局实例未注入（agent-service 启动流程未完成 set_executor 注册），"
                "并非终端桥未启动。请检查 agent-service 启动日志（bridge_relay_executor_registered 事件），"
                "确认 REDIS_URL / CONVERSATION_SERVICE_URL / INTERNAL_API_TOKEN 均已就绪后重启 agent-service。"
            ),
        )

    # 终端级失败哨兵：命令根本没在 HCI 主机上执行（会话缺失 / 桥未运行 / 超时）。
    # 这类结果绝不能进入关键字判定——否则 match_mode="not" / expected=False 信号会把
    # "无输出"误判为"关键字缺失 → 符合排查判定"，产生假阳性（见工单 Q2026071923606）。
    terminal_failure_sentinels = (
        "SSH 会话不存在",
        "需先 ssh_connect",
        "execution timeout",
        "执行超时",
        "终端桥未运行",
        "SSH 未连接",
    )

    results = []
    exec_ids = []
    for cmd in commands:
        try:
            exec_res = await _executor.execute(
                tool_name="acli_exec",
                args={"command": cmd, "reason": f"QFK诊断信号提取执行: {signal.instruction or ''}"},
                conversation_id=conversation_id,
                node_ip=node_ip,
                case_id=case_id,
                risk_level=1,  # QFK 判定均属只读行为，风险为 1
                policy="auto", # 无需前端弹窗，静默自动跑
                exec_id=exec_id,
            )
            # 让命令"在桥上跑过但未真正落到主机"的失败显式化：不进入 evaluate，直接判失败。
            combined = f"{exec_res.stdout or ''}\n{exec_res.stderr or ''}"
            is_terminal_failure = (exec_res.exit_code not in (0, None)) and any(
                s in combined for s in terminal_failure_sentinels
            )
            if is_terminal_failure:
                logger.warning(
                    event="qfk_terminal_failure",
                    namespace=signal.namespace,
                    exec_id=exec_res.exec_id,
                    case_id=case_id or "(empty)",
                    conversation_id=conversation_id,
                    node_ip=node_ip,
                    exit_code=exec_res.exit_code,
                    # 排查定位关键：区分「调用方未透传 case_id」与「会话未建立/已断开」
                    triage=(
                        "case_id 缺失：调用方未透传工单ID，将由 conversation-service 兜底解析"
                        if not case_id
                        else "case_id 已携带仍 session_missing：SSH 会话未建立或已断开"
                    ),
                    preview=combined[:200],
                )
                return QFKResult(
                    matched=False,
                    namespace=signal.namespace,
                    commands=commands,
                    keywords=signal.keyword,
                    match_mode=signal.match_mode,
                    matched_keywords=[],
                    evidence=f"命令未在 HCI 主机执行（终端桥返回失败）: {combined.strip()[:500]}",
                    error=(
                        "命令执行失败：终端会话缺失或桥未运行，未获得真实主机输出，"
                        "无法判定信号。请先通过 Custom-UI 建立 SSH 连接（ssh_connect）后再触发诊断。"
                    ),
                    exec_ids=exec_ids,
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
                namespace=signal.namespace,
                commands=commands,
                keywords=signal.keyword,
                match_mode=signal.match_mode,
                matched_keywords=[],
                evidence=f"在通过 Bridge 执行命令 [{cmd}] 时抛出底层异常: {exec_err}",
                error=str(exec_err),
                exec_ids=exec_ids,
            )

    # 3. 解析结果并做关键字评估（evaluate 同时返回命中关键字，避免重复计算）
    matched, matched_kws, evidence = handler.evaluate(results, signal.keyword, signal.match_mode)

    # 4. 最终布尔判定
    # match_mode == "not" 已在 evaluate 内部表达取反语义（均不出现才为真），无需再翻转；
    # 其余模式（or/and）兼容旧 matcher 的 expected 翻转（expected=False 表示"不出现才符合预期"）。
    mode_norm = {"any": "or", "all": "and"}.get(
        (signal.match_mode or "or").lower(), (signal.match_mode or "or").lower()
    )
    # 显式 is True（详见 2.3①）：signal.expected 为 bool（默认 True，由 pydantic 在
    # 边界拒绝 None/非布尔），此处仅信任布尔真值；避免 falsy 判断在「上游误传 None」时
    # 静默走入 not matched 分支。mode == "not" 已在 evaluate 内部表达取反语义，无需再翻转。
    final_matched = (
        matched
        if mode_norm == "not"
        else matched if (signal.expected is True) else not matched
    )

    logger.info(
        event="qfk_engine_finished",
        namespace=signal.namespace,
        raw_matched=matched,
        final_matched=final_matched,
        matched_keywords=matched_kws,
    )

    return QFKResult(
        matched=final_matched,
        namespace=signal.namespace,
        commands=commands,
        keywords=signal.keyword,
        match_mode=signal.match_mode,
        matched_keywords=matched_kws,
        evidence=evidence,
        exec_ids=exec_ids,
    )
