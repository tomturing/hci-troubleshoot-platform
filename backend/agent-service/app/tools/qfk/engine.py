"""
QFK 后端信号谓词匹配引擎
负责信号加载与解析，复用 Bridge Relay Executor 进行指令中转下发
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.observability.langfuse import observe_tool
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from app.tools.acli.executor import exec_result_observation
from app.tools.qfk.extractor import QFKExtractionError, get_complete_output
from app.tools.qfk.handlers import HandlerRegistry
from app.tools.qfk.matcher import evaluate_matcher
from app.tools.qfk.signal import BackendSignal

logger = get_logger("qfk-engine")


def _exclude_probe_self_observation(text: str, commands: list[str]) -> tuple[str, int]:
    """删除包含本次完整探针命令的日志行，避免 qfk_log 检索自身形成假阳性。"""

    command_tokens = [command.strip() for command in commands if command.strip()]
    kept: list[str] = []
    removed = 0
    for line in (text or "").splitlines():
        if any(command in line for command in command_tokens):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed


@dataclass
class QFKResult:
    """
    QFK 关键信号执行与布尔评判最终输出结果
    """

    matched: bool  # 核心判定：符合排查信号预期返回 True，不符合返回 False
    namespace: str  # 执行信号类型
    commands: list[str]  # 实际执行的底层 acli 指令列表
    keywords: list[str]  # K: 对比关键字列表
    match_mode: str  # 关键字匹配规则 (any / all)
    matched_keywords: list[str]  # 实际检测到匹配命中的关键字
    evidence: str  # 诊断评估执行的完整证据链（由 handler 生成）
    error: str | None = None  # 异常报错信息描述
    exec_ids: list[str] = field(default_factory=list)  # 追踪流流水号记录
    raw_output: str = ""  # 未混入 matcher 目标文本的现场原始输出
    complete_outputs: dict[str, str] = field(default_factory=dict)  # 产出变量使用的完整物理流

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
    required_output_sources: set[str] | None = None,
    output_filters: list[dict[str, Any]] | None = None,
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
            tool_args = {
                "command": cmd,
                "reason": f"QFK诊断信号提取执行: {signal.instruction or ''}",
                # qfk_system.container 已编译为 aCLI --container；它绝不能再被
                # Terminal Bridge 解释为 container_exec 目标，否则容器内会找不到 acli。
                "container": None,
                # 非 JSON 产出变量的行筛选必须在 terminal_bridge 流式执行边界
                # 完成，避免几十 MB 原始输出先穿过 WebSocket/浏览器/HTTP。
                "output_filters": output_filters or [],
            }
            with observe_tool(
                tool_name=f"qfk_{signal.namespace}",
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
                    case_id=case_id,
                    risk_level=1,  # QFK 判定均属只读行为，风险为 1
                    policy="auto",  # 无需前端弹窗，静默自动跑
                    exec_id=exec_id,
                    timeout=signal.timeout,
                )
                if observation:
                    observation.update(output=exec_result_observation(exec_res))
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

    # 3. 产出变量必须使用完整物理流。展示摘要仍可截断；缓存缺失/超限时 Fail Closed。
    complete_outputs: dict[str, str] = {}
    requested_sources = required_output_sources or set()
    if requested_sources:
        failed = next((result for result in results if result.exit_code not in (0, None)), None)
        if failed is not None:
            combined = f"{failed.stdout or ''}\n{failed.stderr or ''}".strip()
            return QFKResult(
                matched=False,
                namespace=signal.namespace,
                commands=commands,
                keywords=signal.keyword,
                match_mode=signal.match_mode,
                matched_keywords=[],
                evidence=combined[:800],
                error=f"QFK_COMMAND_FAILED: 命令退出码为 {failed.exit_code}，不执行判定或变量写入",
                exec_ids=exec_ids,
                raw_output=combined,
            )
        try:
            for source in requested_sources:
                if source not in {"stdout", "stderr"}:
                    raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"不支持的输出来源: {source}")
                complete_outputs[source] = "\n".join(
                    [await get_complete_output(result, _executor._redis, source=source) for result in results]
                )
        except QFKExtractionError as exc:
            return QFKResult(
                matched=False,
                namespace=signal.namespace,
                commands=commands,
                keywords=signal.keyword,
                match_mode=signal.match_mode,
                matched_keywords=[],
                evidence="",
                error=str(exc),
                exec_ids=exec_ids,
                raw_output="\n".join(result.stdout or "" for result in results),
            )

    # 4. 解析结果并做结构化 Matcher 求值。所有 QFK 判定均依赖新版 matcher.extract。
    combined_output = "\n".join(
        f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}" for result in results
    )
    evaluated_output, excluded_probe_lines = _exclude_probe_self_observation(combined_output, commands)
    if signal.matcher:
        matcher_input = evaluated_output
        matcher_extract = signal.matcher.get("extract")
        if isinstance(matcher_extract, dict):
            source = str(matcher_extract.get("source") or "stdout")
            if source not in complete_outputs:
                return QFKResult(
                    matched=False,
                    namespace=signal.namespace,
                    commands=commands,
                    keywords=signal.keyword,
                    match_mode=signal.match_mode,
                    matched_keywords=[],
                    evidence="",
                    error=f"QFK_EXTRACT_INVALID_SPEC: 未取得完整 {source} 输出",
                    exec_ids=exec_ids,
                    raw_output=combined_output,
                    complete_outputs=complete_outputs,
                )
            matcher_input, excluded_probe_lines = _exclude_probe_self_observation(complete_outputs[source], commands)
        matcher_result = evaluate_matcher(signal.matcher, matcher_input)
        if matcher_result.matched is None:
            return QFKResult(
                matched=False,
                namespace=signal.namespace,
                commands=commands,
                keywords=signal.keyword,
                match_mode=signal.match_mode,
                matched_keywords=[],
                evidence=matcher_result.evidence,
                error="QFK_MATCHER_INCONCLUSIVE: 现场输出不足以完成确定性判定",
                exec_ids=exec_ids,
                raw_output=combined_output,
                complete_outputs=complete_outputs,
            )
        final_matched = bool(matcher_result.matched)
        matched_kws = list(matcher_result.detail.get("matched_keywords") or [])
        evidence = matcher_result.evidence
        if signal.namespace == "log":
            evidence = (
                f"【qfk_log 解释契约】family={signal.source_family}, parser={signal.parser}, "
                f"path={signal.path or '(acli default)'}, self_observation_excluded={excluded_probe_lines}\n"
                f"{evidence}"
            )
        matched = bool(matcher_result.detail.get("hit", final_matched))
    else:
        return QFKResult(
            matched=False,
            namespace=signal.namespace,
            commands=commands,
            keywords=signal.keyword,
            match_mode=signal.match_mode,
            matched_keywords=[],
            evidence="",
            error="QFK_MATCHER_MISSING: QFK 判定必须配置新版 match.extract",
            exec_ids=exec_ids,
            raw_output=combined_output,
            complete_outputs=complete_outputs,
        )

    # 5. 最终布尔判定
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
        raw_output=combined_output,
        complete_outputs=complete_outputs,
    )
