"""
KBD 差异诊断引擎（KBD Differential Diagnostic）

实现贪心最大消除算法（Greedy Maximum Elimination）：
  1. 统计各步骤工具在候选 KBD 中的覆盖频率
  2. 选择覆盖频率最高的步骤（最具区分度）
  3. 执行步骤，判断实际输出与各 KBD 期望的匹配度
  4. 过滤不匹配的 KBD
  5. 重复直到候选集 ≤ early_stop_threshold 或所有步骤已耗尽
  6. 生成最终诊断报告

期望模式判断优先级：
  1. __REGEX__:<pattern>       正则匹配（程序判断，最快）
  2. __CONTAINS__:<text>       包含文本（程序判断，不区分大小写）
  3. <自然语言描述>              LLM 批量判断（最慢，仅在无法规则判断时使用）
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from shared.clients import AIAssistantRegistry
from shared.observability.logger import get_logger

from app.adapters.agents.htp.kbd_model import (
    KBD,
    PATTERN_CONTAINS_PREFIX,
    PATTERN_MATCHER_PREFIX,
    PATTERN_REGEX_PREFIX,
    KBDStep,
    _acquire_tool,
    _signal_category,
    _signal_to_step,
)
from app.core.utils import smart_truncate
from app.domain.agent_port import (
    AgentEvent,
    AgentInteractiveRequest,
    AgentStageUpdate,
    AgentTextChunk,
)

logger = get_logger("kbd-differential")

# 候选 KBD 数 ≤ 此值时停止贪心消除，直接进入报告生成
EARLY_STOP_THRESHOLD = 2

# 工具执行连续失败超过此次数后停止，防止在损坏环境中无限等待
MAX_CONSECUTIVE_FAILURES = 3

# ADR-2 占位符：运行期解析统一认 {{NAME}}。大小写处理分两层（纵深防御，彻底消除脆弱性）：
#   1) 抽取/校验层强制模板占位符为大写（extract_signals.validate_placeholder_case，
#      如 {{HOST}} 合法、{{host}} 非法），保证模板书写规范；
#   2) 运行期解析对「变量名」大小写不敏感（_resolve_args 按小写查找），且生产者写入
#      变量池时 Key 强制小写（_set_pool_var），因此无论 {{HOST}}/{{host}}/{{Host}}
#      均能命中池内小写 Key，无需依赖「produces 名必须大写」的全局隐式约定；
#      未命中（如生产者尚未产出该变量）的占位符保留原样，不抛异常，交由下层处理。
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_.]+)\}\}")

# 写操作子命令词表（与 kb-service.extract_signals.WRITE_OP_SUB_COMMANDS 保持一致）。
# 执行层以此做纵深防御：即便信号 schema 未带 require_human_confirm，只要 command
# 命中写动词，也绝不自动执行，必须人工授权。
_WRITE_OP_SUB_COMMANDS: set[str] = {
    "start", "stop", "shutdown", "restart", "suspend", "resume",
    "migrate", "clone", "snapshot", "reset", "reboot",
    "delete", "remove", "del", "rm", "format", "wipe", "destroy",
    # 注：上文之外补充 qfk_system 常见包裹动作
    "enable", "disable", "kill", "killall", "pkill",
    "up", "down", "set", "create", "add", "modify", "update",
}


def _signal_requires_human(signal: dict | None) -> bool:
    """写操作/处置动作信号判定：诊断阶段绝不自动执行，必须人工授权。

    判定优先级：
      1) 信号已显式标记 require_human_confirm / phase=solution（抽取层已标注）
      2) 纵深防御：backend（qfk_*）信号 command 命中写动词词表
    """
    if not signal:
        return False
    if (signal.get("review") or {}).get("require_human_confirm"):
        return True
    if (signal.get("orchestrate") or {}).get("phase") == "solution":
        return True
    acquire = signal.get("acquire") or {}
    acquirer = acquire.get("tool", "")
    if acquirer.startswith("qfk_"):
        a = acquire.get("args") or {}
        sub = str(a.get("command") or "")
        tokens = set(re.split(r"[\s|/]+", sub.strip()))
        if tokens & _WRITE_OP_SUB_COMMANDS:
            return True
    return False


@dataclass
class StepResult:
    """单步执行结果记录（用于报告生成和审计追踪）。"""

    tool_name: str
    tool_args: dict
    raw_output: str | None  # 工具执行的原始输出字符串
    error: str | None  # 执行错误（非 None 时此步骤无法判断）
    match_kbd_ids: set[str] = field(default_factory=set)  # 判断后匹配的 KBD ID 集合


@dataclass
class KBDDiagResult:
    """KBD 差异诊断最终结果。"""

    matched_kbds: list[KBD]  # 最终候选 KBD（按匹配度排序）
    steps_executed: list[StepResult]  # 已执行步骤序列
    is_definitive: bool  # True = 恰好锁定 1 个 KBD
    diagnosis_report: str  # LLM 生成的结构化诊断报告


class KBDDiagnostic:
    """KBD 差异诊断引擎。

    使用方式：
        diag = KBDDiagnostic(ai_registry, tool_executor)
        async for event in diag.diagnose(candidates, env_context, session_id):
            yield event
        result = diag.get_result()

    线程安全：单实例不应并发调用 diagnose()，每次对话应创建独立实例。
    """

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        tool_executor: Any,  # 实现 ToolExecutor Protocol 的对象
        diagnostic_item_client: Any | None = None,  # DiagnosticItemClient（可选）
        conversation_id: str | None = None,  # 会话 ID（用于 INSERT）
        case_id: str | None = None,  # 工单 ID（用于 terminal_bridge 会话路由）
        assistant_type: str = "htp-agent",
        early_stop_threshold: int = EARLY_STOP_THRESHOLD,
        db_session_factory: Any | None = None,  # DB 会话工厂（用于从 prompt 管理加载 Prompt）
    ) -> None:
        self._ai_registry = ai_registry
        self._tool_executor = tool_executor
        self._diagnostic_item_client = diagnostic_item_client
        self._conversation_id = conversation_id
        self._case_id = case_id  # 工单 ID，用于 QFK 信号执行时路由到正确的 SSH 会话
        self._assistant_type = assistant_type
        self._early_stop = early_stop_threshold
        self._db_session_factory = db_session_factory
        self._result: KBDDiagResult | None = None
        # 会话级变量池（黑板）：阶段 A 生产者(QKV)写入，阶段 B 消费者(QFK)读取
        self._variable_pool: dict[str, Any] = {}

    def get_result(self) -> KBDDiagResult | None:
        """获取最近一次 diagnose() 调用的结果（调用前返回 None）。"""
        return self._result

    async def _load_prompt(self, name: str, placeholders: list[str]) -> str:
        """从 prompt 管理（system_prompt 表）加载 Prompt 模板。

        生产环境经 db_session_factory 走 StrictPromptLoader（DB 缺失则按设计 fail-loud）；
        DB 会话工厂为空（如部分单测）时回退到 create_mock_session_factory 的基准模板，
        保证逻辑路径与线上一致且单测可解析。
        """
        from shared.utils.prompt_loader import StrictPromptLoader, create_mock_session_factory

        factory = self._db_session_factory or create_mock_session_factory()
        async with factory() as session:
            return await StrictPromptLoader.load_and_validate(session, name, placeholders)

    def _set_pool_var(self, name: str, value: Any) -> None:
        """写入会话变量池（黑板）的规范化入口。

        生产侧静默规范化：Key 强制统一小写存储，从源头消除变量池内部大小写
        不一致（如 HoSt / host / HOST 并存），保证 debug 打印、前端展示、
        落库存储时 Key 永远干净统一（host、vm_id）。
        消费侧 _resolve_args 同步做大小写不敏感解析作为纵深，二者组合彻底消除
        大小写脆弱性。与抽取层「占位符必须大写」的校验互不冲突——模板大写仅约束
        书写方，运行期解析兼容任意大小写。

        P2 增强：记录变量池变更日志，便于追溯信号执行链路。
        """
        key = name.strip().lower()
        old_value = self._variable_pool.get(key)
        self._variable_pool[key] = value

        # P2: 变量池变更日志
        value_preview = str(value)[:100] if value is not None else None
        if len(str(value)) > 100:
            value_preview = value_preview + "...(截断)"
        logger.info(
            event="variable_pool_update",
            name=name,
            key=key,
            value_preview=value_preview,
            is_new=key not in self._variable_pool or old_value is None,
            session_id=self._conversation_id or "",
        )

    async def diagnose(
        self,
        candidates: list[KBD],
        env_context: dict[str, str],
        session_id: str,
        user_id: str = "",
    ) -> AsyncGenerator[AgentEvent, None]:
        """执行 KBD 差异诊断主循环，流式输出推理阶段事件。

        Args:
            candidates: 候选 KBD 列表（来自 kb_client.search_cases_with_steps()）
            env_context: 环境上下文，用于填充工具参数占位符
                         如 {"vm_name": "vm-001", "host_id": "CVM-1", "cluster_id": "cluster-01"}
            session_id: 当前对话会话 ID（用于日志追踪）
            user_id: 用户 ID（传给 LLM 判断请求）

        Yields:
            AgentStageUpdate(stage="kbd_diag_start")    — 开始诊断
            AgentStageUpdate(stage="kbd_diag_step")     — 每步执行前
            AgentStageUpdate(stage="kbd_diag_running")  — 进入报告生成
            AgentStageUpdate(stage="kbd_diag_complete") — 诊断完成
        """
        if not candidates:
            yield AgentTextChunk(content="⚠️ 未找到匹配 KBD，建议联系 HCI 技术支持。")
            self._result = KBDDiagResult(
                matched_kbds=[],
                steps_executed=[],
                is_definitive=False,
                diagnosis_report="未找到匹配 KBD",
            )
            return

        remaining = list(candidates)
        # 确定性排序（第一性原理：诊断结论必须可复现）。
        # 以「相似度降序 + KBD id 升序」为稳定次序，保证：
        #   - remaining[0] 始终是首选候选（报告/S4 根因锁定一致）
        #   - 贪心循环与确认分支在并列时的选择可复现，消除顺序随机性
        remaining.sort(key=lambda k: (-k.similarity, str(k.id)))
        steps_executed: list[StepResult] = []
        consecutive_failures = 0

        # ─── S2：批量插入假设条目 ──────────────────────────────────────────
        if self._diagnostic_item_client and self._conversation_id:
            hypotheses_data = [
                {
                    "content": {
                        "kbd_id": kbd.id,
                        "kbd_name": kbd.name,
                        "root_cause": kbd.root_cause,
                        "similarity": kbd.similarity,
                    },
                    "probability": kbd.similarity,  # 用相似度作为初始概率
                    "status": "pending",
                }
                for kbd in candidates
            ]
            await self._diagnostic_item_client.batch_create_items(
                conversation_id=uuid.UUID(self._conversation_id),
                stage="S2",
                type="hypothesis",
                items_data=hypotheses_data,
            )
            logger.info(
                event="s2_hypotheses_inserted",
                conversation_id=self._conversation_id,
                count=len(hypotheses_data),
                session_id=session_id,
            )

        yield AgentStageUpdate(
            stage="kbd_diag_start",
            metadata={
                "candidate_count": len(remaining),
                "session_id": session_id,
            },
        )

        # ─── 阶段 A：生产者先跑，填充会话变量池（黑板）─────────────────
        await self._run_producers(remaining, env_context, session_id)

        # ─── 贪心消除主循环 ──────────────────────────────────────────
        while len(remaining) > self._early_stop:
            executed_tools = {s.tool_name for s in steps_executed}

            # 1. 选择本轮最具区分度的工具
            best_tool_name = self._pick_best_step(remaining, executed_tools)
            if best_tool_name is None:
                # 所有共享步骤均已执行，退出
                break

            # 2. 构建工具执行参数（替换 env_context ∪ 变量池 占位符）
            representative_step = self._get_representative_step(remaining, best_tool_name)
            tool_args = self._resolve_args(representative_step.tool_args_template, env_context, self._variable_pool)

            yield AgentStageUpdate(
                stage="kbd_diag_step",
                metadata={
                    "tool": best_tool_name,
                    "args": tool_args,
                    "remaining_candidates": len(remaining),
                    "step_index": len(steps_executed) + 1,
                },
            )

            # 写操作/处置动作门禁（诊断只读原则）：绝不自动执行，交由人工授权。
            # 即便贪心循环选中了某写操作信号，也只记录并请求授权，不进入 judge/eliminate。
            rep_signal = remaining[0].get_signal(best_tool_name) if remaining else None
            if _signal_requires_human(rep_signal):
                logger.warning(
                    event="kbd_diag_write_op_skipped",
                    tool=best_tool_name,
                    session_id=session_id,
                )
                steps_executed.append(
                    StepResult(
                        tool_name=best_tool_name,
                        tool_args=tool_args,
                        raw_output=None,
                        error="写操作/处置动作：诊断阶段不自动执行，需人工授权",
                        match_kbd_ids=set(remaining),
                    )
                )
                yield AgentStageUpdate(
                    stage="write_op_blocked",
                    metadata={"tool": best_tool_name, "reason": "require_human_confirm"},
                )
                yield AgentInteractiveRequest(
                    request_id=f"human-auth-{best_tool_name}-{session_id}",
                    acp_session_id=session_id,
                    kind="info_request",
                    title=f"需人工授权的高危操作：{best_tool_name}",
                    prompt=(
                        f"信号 {best_tool_name} 为写操作/处置动作，诊断阶段不会自动执行。"
                        "如需执行请人工确认授权。"
                    ),
                    options=[
                        {"optionId": "approve", "name": "授权执行"},
                        {"optionId": "deny", "name": "拒绝"},
                    ],
                    metadata={"tool": best_tool_name, "risk": (rep_signal or {}).get("risk", 2)},
                )
                continue

            # 3. 执行工具（按 acquirer 路由：qkv→QKV / qfk→QFK / 其他→通用 tool_executor）
            raw_output: str | None = None
            error: str | None = None
            pre_matched: bool | None = None
            try:
                raw_output, error, pre_matched = await self._execute_acquirer(
                    representative_step, env_context, session_id, user_id
                )
                consecutive_failures = 0  # 重置连续失败计数
            except Exception as exc:
                error = str(exc)
                consecutive_failures += 1
                logger.warning(
                    event="kbd_diag_step_error",
                    tool_name=best_tool_name,
                    error=error,
                    session_id=session_id,
                    consecutive_failures=consecutive_failures,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        event="kbd_diag_too_many_failures",
                        message="工具执行连续失败超限，中止诊断循环",
                        session_id=session_id,
                    )
                    break

            # 4. 判断各 KBD 匹配度（执行成功时）
            match_ids: set[str] = set()
            if raw_output is not None:
                match_ids = await self._judge_matches(
                    tool_name=best_tool_name,
                    actual_output=raw_output,
                    kbds=remaining,
                    user_id=user_id,
                    pre_matched=pre_matched,
                )

            step_result = StepResult(
                tool_name=best_tool_name,
                tool_args=tool_args,
                raw_output=raw_output,
                error=error,
                match_kbd_ids=match_ids,
            )
            steps_executed.append(step_result)

            # ─── S3：插入验证步骤条目 ──────────────────────────────────────
            if self._diagnostic_item_client and self._conversation_id:
                await self._diagnostic_item_client.create_item(
                    conversation_id=uuid.UUID(self._conversation_id),
                    stage="S3",
                    type="verification_step",
                    seq=len(steps_executed),
                    content={
                        "tool_name": best_tool_name,
                        "tool_args": tool_args,
                        # T0-2：替换固定截断为 smart_truncate，优先保留错误关键字行
                        "raw_output": smart_truncate(raw_output or "", max_chars=500),
                        "error": error,
                        "match_kbd_ids": list(match_ids) if match_ids else [],
                        "eliminated_count": 0,  # 下一步过滤后计算
                    },
                    status="confirmed" if error is None else "rejected",
                )
                logger.info(
                    event="s3_verification_step_inserted",
                    conversation_id=self._conversation_id,
                    seq=len(steps_executed),
                    tool_name=best_tool_name,
                    session_id=session_id,
                )

            # 5. 过滤：只保留匹配 KBD（执行失败时不过滤，继续下一步）
            if match_ids:
                before_count = len(remaining)
                remaining = [kbd for kbd in remaining if kbd.id in match_ids]
                eliminated = before_count - len(remaining)
                logger.info(
                    event="kbd_diag_elimination",
                    tool_name=best_tool_name,
                    eliminated=eliminated,
                    remaining=len(remaining),
                    session_id=session_id,
                )

        # ─── 关键信号确认阶段（治本：杜绝“未用关键信号确认就下结论”）─────────
        # 背景：贪心消除主循环仅在 len(remaining) > early_stop 时进入。当候选 KBD 数量
        # ≤ early_stop（高度同质化分类的典型场景，如“虚拟机-003 开机失败”常只有单条匹配
        # KBD），主循环一次都不执行。此前此处只重放 backend 关键信号、并 skip 前端生产者，
        # 导致“前端信号优先级 > 后端”原则被违反，且最可能具区分度的前端关键信号
        # （如 qkv_task 启动虚拟机失败）被排除在证据之外 —— 正是 Q2026071755113 工单
        # “首步可见步骤是 acli vm start 而非 qkv_task”的根因之一。故当主循环未产出任何
        # 步骤时，强制按 KBD 内容顺序补跑剩余候选的【全部】信号（前端生产者 + 后端消费者），
        # 作为结论的现场证据（确认语义：记录证据，不剔除候选）。前端生产者已在阶段 A 静默
        # 跑过并填充变量池，此处重放为只读、可幂等，仅用于补全证据链与顺序，使可见顺序
        # 与 KBD 内容顺序一致。
        if not steps_executed:
            confirmed_tools: set[str] = set()
            for kbd in remaining:
                for s in kbd.signals:
                    # 按 KBD 内容顺序遍历全部信号（含前端生产者），不再 skip 前端
                    tool = _acquire_tool(s)
                    if not tool or tool in confirmed_tools:
                        continue
                    confirmed_tools.add(tool)
                    step = kbd.get_step(tool) or _signal_to_step(s)
                    if step is None:
                        continue

                    tool_args = self._resolve_args(
                        step.tool_args_template, env_context, self._variable_pool
                    )
                    yield AgentStageUpdate(
                        stage="kbd_diag_confirm",
                        metadata={
                            "tool": tool,
                            "category": _signal_category(s),
                            "args": tool_args,
                            "remaining_candidates": len(remaining),
                            "purpose": "关键信号确认（按 KBD 内容顺序，含前端生产者）",
                        },
                    )

                    # 写操作/处置动作门禁（confirm 分支同样适用）：不自动执行，请求人工授权
                    if _signal_requires_human(kbd.get_signal(tool)):
                        steps_executed.append(
                            StepResult(
                                tool_name=tool,
                                tool_args=tool_args,
                                raw_output=None,
                                error="写操作/处置动作：诊断阶段不自动执行，需人工授权",
                                match_kbd_ids=set(),
                            )
                        )
                        yield AgentStageUpdate(
                            stage="write_op_blocked",
                            metadata={"tool": tool, "reason": "require_human_confirm"},
                        )
                        yield AgentInteractiveRequest(
                            request_id=f"human-auth-{tool}-{session_id}",
                            acp_session_id=session_id,
                            kind="info_request",
                            title=f"需人工授权的高危操作：{tool}",
                            prompt=(
                                f"信号 {tool} 为写操作/处置动作，诊断阶段不会自动执行。"
                                "如需执行请人工确认授权。"
                            ),
                            options=[
                                {"optionId": "approve", "name": "授权执行"},
                                {"optionId": "deny", "name": "拒绝"},
                            ],
                            metadata={"tool": tool, "risk": (kbd.get_signal(tool) or {}).get("risk", 2)},
                        )
                        continue

                    raw_output = None
                    error = None
                    pre_matched = None
                    try:
                        raw_output, error, pre_matched = await self._execute_acquirer(
                            step, env_context, session_id, user_id
                        )
                    except Exception as exc:
                        error = str(exc)
                        logger.warning(
                            event="kbd_diag_confirm_error",
                            tool_name=tool,
                            error=error,
                            session_id=session_id,
                        )

                    match_ids: set[str] = set()
                    if raw_output is not None:
                        match_ids = await self._judge_matches(
                            tool_name=tool,
                            actual_output=raw_output,
                            kbds=remaining,
                            user_id=user_id,
                            pre_matched=pre_matched,
                        )

                    steps_executed.append(
                        StepResult(
                            tool_name=tool,
                            tool_args=tool_args,
                            raw_output=raw_output,
                            error=error,
                            match_kbd_ids=match_ids,
                        )
                    )

                    if self._diagnostic_item_client and self._conversation_id:
                        await self._diagnostic_item_client.create_item(
                            conversation_id=uuid.UUID(self._conversation_id),
                            stage="S3",
                            type="verification_step",
                            seq=len(steps_executed),
                            content={
                                "tool_name": tool,
                                "tool_args": tool_args,
                                "raw_output": smart_truncate(raw_output or "", max_chars=500),
                                "error": error,
                                "match_kbd_ids": list(match_ids),
                                "is_confirmation": True,
                            },
                            status="confirmed" if error is None else "rejected",
                        )

        # ─── 生成最终诊断报告 ─────────────────────────────────────────
        yield AgentStageUpdate(
            stage="kbd_diag_running",
            metadata={"final_candidates": len(remaining)},
        )

        report = await self._generate_report(remaining, steps_executed, user_id=user_id)

        self._result = KBDDiagResult(
            matched_kbds=remaining,
            steps_executed=steps_executed,
            is_definitive=(len(remaining) == 1),
            diagnosis_report=report,
        )

        # ─── S4：插入根因确认条目 ─────────────────────────────────────────
        if self._diagnostic_item_client and self._conversation_id and remaining:
            top_kbd = remaining[0]
            await self._diagnostic_item_client.create_item(
                conversation_id=uuid.UUID(self._conversation_id),
                stage="S4",
                type="root_cause",
                seq=1,
                content={
                    "kbd_id": top_kbd.id,
                    "kbd_name": top_kbd.name,
                    "root_cause": top_kbd.root_cause,
                    "solution": top_kbd.solution,
                    "is_definitive": len(remaining) == 1,
                    "matched_kbds_count": len(remaining),
                    "steps_executed_count": len(steps_executed),
                },
                probability=top_kbd.similarity,
                status="confirmed",
            )
            logger.info(
                event="s4_root_cause_inserted",
                conversation_id=self._conversation_id,
                kbd_id=top_kbd.id,
                is_definitive=len(remaining) == 1,
                session_id=session_id,
            )

        yield AgentStageUpdate(
            stage="kbd_diag_complete",
            metadata={
                "matched_count": len(remaining),
                "steps_count": len(steps_executed),
                "is_definitive": len(remaining) == 1,
            },
        )

    # ─── 算法内部方法 ─────────────────────────────────────────────────

    def _pick_best_step(
        self,
        candidates: list[KBD],
        executed_tools: set[str],
    ) -> str | None:
        """贪心选择：返回覆盖最多候选 KBD 且尚未执行的工具名称。

        覆盖频率相同时，Counter.most_common 返回第一个（即字母序较小者）。
        """
        counter: Counter[str] = Counter()
        for kbd in candidates:
            for tool_name in kbd.step_tool_names:
                if tool_name not in executed_tools:
                    counter[tool_name] += 1

        if not counter:
            return None

        best, _freq = counter.most_common(1)[0]
        return best

    def _get_representative_step(self, candidates: list[KBD], tool_name: str) -> KBDStep:
        """从候选 KBD 中取第一个含指定 tool_name 的步骤定义（用于构建 args 模板）。"""
        for kbd in candidates:
            step = kbd.get_step(tool_name)
            if step is not None:
                return step
        # 不应发生（_pick_best_step 保证工具来自某个 KBD）
        return KBDStep(tool_name=tool_name, tool_args_template={}, expected_pattern="", matcher=None)

    @staticmethod
    def _resolve_args(
        template: dict,
        env_context: dict[str, str] | None = None,
        variable_pool: dict | None = None,
    ) -> dict:
        """将 args 模板中的 {{NAME}} 替换为 env_context ∪ variable_pool 中的实际值。

        ADR-2：统一占位符为 {{VAR}}。运行期把“环境上下文”与“会话变量池（黑板）”合并后做单遍
        正则替换（同一套渲染、同一个池）；未命中（如生产者尚未产出该变量）的占位符保留原样。

        示例：
            template = {"scope": "{{HOST}}"}
            variable_pool = {"HOST": "node-001"}
            → {"scope": "node-001"}
        """
        merged: dict[str, Any] = dict(env_context or {})
        if variable_pool:
            merged.update(variable_pool)

        # 防御性归一：占位符解析大小写不敏感（ADR-2）。
        # 生产者（QKV/tool_def）可能以任意大小写写入变量池（如 HOST/host/HoSt），
        # 消费侧统一按小写查找，确保 {{HOST}}/{{host}}/{{Host}} 均可命中池内小写
        # Key，无需依赖「produces 名必须大写」的全局隐式约定。
        lower_merged = {k.lower(): v for k, v in merged.items()}

        def _rep(m: re.Match[str]) -> str:
            name = m.group(1).strip().lower()
            return str(lower_merged[name]) if name in lower_merged else m.group(0)

        resolved = {}
        for k, v in template.items():
            if isinstance(v, str):
                v = _PLACEHOLDER_RE.sub(_rep, v)
            resolved[k] = v
        return resolved

    async def _judge_matches(
        self,
        tool_name: str,
        actual_output: str,
        kbds: list[KBD],
        user_id: str = "",
        pre_matched: bool | None = None,
    ) -> set[str]:
        """判断实际输出与各 KBD 期望模式的匹配度，返回匹配 KBD 的 ID 集合。

        策略（优先规则/类型化判断，降低 LLM 调用次数）：
          - Matcher dict（来自信号 schema）：按 type 做 5 类定型求值（§6）
          - __REGEX__: 正则匹配 → 程序判断
          - __CONTAINS__: 包含文本 → 程序判断
          - __MATCHER__:<json>：解析为 Matcher dict 后类型化求值
          - 自然语言 / 无法定求值 → LLM 批量判断
        """
        rule_results: dict[str, bool] = {}  # kbd_id → 规则判断结果
        llm_kbds: list[KBD] = []  # 需要 LLM 判断的 KBD

        for kbd in kbds:
            # 优先使用信号 schema 中的 Matcher dict 做类型化定值（§6）
            matcher = kbd.get_matcher(tool_name)
            if matcher is not None:
                if pre_matched is not None and matcher.get("type") == "keyword":
                    # qfk 引擎已对 keyword 类型完成布尔判定，直接采信
                    rule_results[kbd.id] = pre_matched
                    continue
                ev = self._evaluate_matcher(matcher, actual_output)
                if ev is not None:
                    rule_results[kbd.id] = ev
                    continue
                llm_kbds.append(kbd)
                continue

            # 兼容旧 KBD 的 expected_pattern 三态
            pattern = kbd.get_expected_pattern(tool_name)
            if pattern is None:
                # KBD 无此步骤定义 → 不应出现，保守地保留该 KBD
                rule_results[kbd.id] = True
                continue

            if pattern.startswith(PATTERN_REGEX_PREFIX):
                regex_str = pattern[len(PATTERN_REGEX_PREFIX) :]
                try:
                    matched = bool(re.search(regex_str, actual_output, re.IGNORECASE | re.DOTALL))
                except re.error:
                    matched = False
                rule_results[kbd.id] = matched

            elif pattern.startswith(PATTERN_CONTAINS_PREFIX):
                keyword = pattern[len(PATTERN_CONTAINS_PREFIX) :]
                rule_results[kbd.id] = keyword.lower() in actual_output.lower()

            elif pattern.startswith(PATTERN_MATCHER_PREFIX):
                # 旧 Matcher 序列化：解析为 dict 后类型化定值
                try:
                    matcher = json.loads(pattern[len(PATTERN_MATCHER_PREFIX):])
                except (json.JSONDecodeError, ValueError):
                    rule_results[kbd.id] = True
                    continue
                ev = self._evaluate_matcher(matcher, actual_output)
                if ev is not None:
                    rule_results[kbd.id] = ev
                    continue
                llm_kbds.append(kbd)

            else:
                # 自然语言描述 → 推迟到 LLM 判断
                llm_kbds.append(kbd)

        # LLM 批量判断（若有）
        llm_results: dict[str, bool] = {}
        if llm_kbds:
            llm_results = await self._llm_judge_batch(tool_name, actual_output, llm_kbds, user_id)

        # 合并判断结果
        matched_ids: set[str] = set()
        for kbd in kbds:
            if kbd.id in rule_results:
                if rule_results[kbd.id]:
                    matched_ids.add(kbd.id)
            elif llm_results.get(kbd.id, True):  # LLM 无法判断时保守保留
                matched_ids.add(kbd.id)

        return matched_ids

    # ─── Matcher 类型化求值（§6：5 类定型 valuator）────────────────────────────

    def _evaluate_matcher(self, matcher: dict[str, Any], actual_output: str) -> bool | None:
        """对单条 Matcher 契约做确定性（非 LLM）布尔求值（委托单一真相源）。

        返回 True/False 表示“符合期望”；返回 None 表示无法定值（交由 LLM 兜底）。
        支持类型：keyword / regex / state / threshold / json_path / exists。

        实现已统一迁移至 app.tools.qfk.matcher.evaluate_matcher，此处仅作委托，
        确保 KBD 差异诊断与 QFK 引擎使用同一套求值逻辑、证据链与 or/and/not 语义，
        消除此前两份 keyword 实现可能漂移的隐患。
        """
        from app.tools.qfk.matcher import evaluate_matcher

        return evaluate_matcher(matcher, actual_output).matched

    # ─── 阶段 A/B：生产者/消费者执行与变量池 ───────────────────────────────────

    async def _run_producers(
        self,
        candidates: list[KBD],
        env_context: dict[str, str],
        session_id: str,
    ) -> None:
        """阶段 A：跑全部生产者信号（QKV），填充会话变量池（黑板）。

        去重（acquirer + args 相同只跑一次）；执行失败仅告警，不阻断诊断。
        """
        producers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for kbd in candidates:
            for s in kbd.signals:
                if _signal_category(s) != "frontend":
                    continue
                key = (
                    _acquire_tool(s),
                    json.dumps((s.get("acquire") or {}).get("args") or {}, sort_keys=True, ensure_ascii=False),
                )
                if key in seen:
                    continue
                seen.add(key)
                producers.append(s)

        for s in producers:
            try:
                fsignal = self._signal_to_qkv(s, env_context)
                if fsignal is None:
                    logger.warning(
                        event="kbd_diag_producer_skip",
                        acquirer=_acquire_tool(s),
                        session_id=session_id,
                    )
                    continue
                from app.tools.qkv.engine import qkv_exec

                res = await qkv_exec(
                    signal=fsignal,
                    conversation_id=self._conversation_id or session_id,
                    node_ip=env_context.get("node_ip"),
                    exec_id=None,
                )
                if res.success:
                    self._fill_pool_from_qkv(s, res)
                else:
                    logger.warning(
                        event="kbd_diag_producer_failed",
                        acquirer=_acquire_tool(s),
                        error=res.error,
                        session_id=session_id,
                    )
            except Exception as exc:
                logger.warning(
                    event="kbd_diag_producer_error",
                    acquirer=_acquire_tool(s),
                    error=str(exc),
                    session_id=session_id,
                )

    def _fill_pool_from_qkv(self, signal: dict[str, Any], res: Any) -> None:
        """将 QKV 产出(produces)写入变量池：produces=[{name, path}] -> pool[name]=value。

        注意：parse_frontend_value._extract_by_produces 返回的 dict key 是 name.lower()，
        所以这里用 name.lower() 查找，而非 path（path 是原始 JSON 字段路径，用于提取阶段）。

        让 tool_definition 生效：当 signals_json 未配置 produces 时，回退到
        admin-ui 配置的 tool_definition 默认值（与 _signal_to_qkv 保持一致）。
        """
        produces = (signal.get("orchestrate") or {}).get("produces")
        if not produces:
            produces = self._tool_def_default(_acquire_tool(signal), "produces") or []
        if not res.values:
            return
        first = res.values[0]
        for spec in produces:
            name = spec.get("name") if isinstance(spec, dict) else None
            if not name:
                continue
            # 提取后的 dict key 是 name.lower()（见 parser._extract_by_produces line 90）
            val = first.get(name.lower()) if isinstance(first, dict) else None
            if val is not None:
                self._set_pool_var(name, val)

    async def _execute_acquirer(
        self,
        step: KBDStep,
        env_context: dict[str, str],
        session_id: str,
        user_id: str,
    ) -> tuple[str | None, str | None, bool | None]:
        """按 acquirer 路由执行：qkv→QKV 引擎 / qfk→QFK 引擎 / 其他→通用 tool_executor。

        返回 (raw_output, error, pre_matched)：
          - raw_output: 渲染给 _judge_matches 的文本（None 表示执行失败）
          - error: 错误信息
          - pre_matched: qfk keyword 类型已由引擎判定时直接给出布尔，否则 None
        """
        acquirer = step.tool_name

        # P2 增强：信号匹配日志 - 记录即将执行的信号
        logger.info(
            event="signal_executing",
            acquirer=acquirer,
            tool_args_template=step.tool_args_template,
            session_id=session_id,
            conversation_id=self._conversation_id,
        )

        if acquirer.startswith("qkv_"):
            # 生产者：QKV 引擎取数并写变量池（通常已在阶段 A 跑过，此处为兜底）
            try:
                fsignal = self._signal_to_qkv_from_step(step, env_context)
                if fsignal is None:
                    logger.warning(
                        event="signal_build_failed",
                        acquirer=acquirer,
                        reason="无法构建前端信号",
                        session_id=session_id,
                    )
                    return None, "无法构建前端信号", None
                from app.tools.qkv.engine import qkv_exec

                res = await qkv_exec(
                    signal=fsignal,
                    conversation_id=self._conversation_id or session_id,
                    node_ip=env_context.get("node_ip"),
                    exec_id=None,
                )
                if not res.success:
                    logger.warning(
                        event="signal_exec_failed",
                        acquirer=acquirer,
                        error=res.error,
                        session_id=session_id,
                    )
                    return None, res.error, None
                self._fill_pool_from_qkv_on_step(step, res)

                # P2: 信号执行成功日志
                logger.info(
                    event="signal_exec_success",
                    acquirer=acquirer,
                    values_count=len(res.values) if res.values else 0,
                    session_id=session_id,
                )
                return res.to_observation(), None, None
            except Exception as exc:
                logger.error(
                    event="signal_exec_exception",
                    acquirer=acquirer,
                    error=str(exc),
                    session_id=session_id,
                )
                return None, str(exc), None

        if acquirer.startswith("qfk_"):
            # 消费者：QFK 引擎取数并判定；keyword 类型直接采信引擎布尔，其余交由 _judge_matches
            try:
                bsignal = self._signal_to_qfk(step)
                if bsignal is None:
                    return None, "无法构建后端信号", None
                from app.tools.qfk.engine import qfk_exec

                res = await qfk_exec(
                    signal=bsignal,
                    conversation_id=self._conversation_id or session_id,
                    node_ip=env_context.get("node_ip"),
                    case_id=self._case_id,  # 透传工单 ID，确保 terminal_bridge 能路由到正确的 SSH 会话
                    exec_id=None,
                )
                if res.error:
                    return res.to_observation(), res.error, None
                matcher = step.matcher or {}
                if matcher.get("type") == "keyword":
                    return res.to_observation(), None, res.matched
                return res.to_observation(), None, None
            except Exception as exc:
                return None, str(exc), None

        # 遗留/通用工具（acli_* 等）：走既有 tool_executor
        try:
            args = self._resolve_args(step.tool_args_template, env_context, self._variable_pool)
            result = await self._tool_executor.execute(acquirer, args)
            return (str(result) if result is not None else "", None, None)
        except Exception as exc:
            return None, str(exc), None

    def _tool_def_default(self, tool_name: str, key: str) -> Any | None:
        """从 tool_definition 注册表读取某参数的默认值（produces / matcher 等）。

        这是「让 tool_definition 生效」的关键闭环：当 SOP/signals_json 未填写
        produces / matcher 时，回退到运营在 admin-ui 配置并写入 tool_definition
        的默认值。运营人员无需改 signals_json 即可调整默认产出 / 判定。
        """
        from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY

        tool = TOOL_REGISTRY.get(tool_name)
        if not tool or not isinstance(tool.parameters, dict):
            return None
        prop = tool.parameters.get("properties", {}).get(key)
        if isinstance(prop, dict):
            return prop.get("default")
        return None

    def _signal_to_qkv(self, signal: dict[str, Any], env_context: dict[str, str]) -> Any:
        """从生产者信号 dict 构造 qkv/signal.FrontendSignal（解析占位符后再构造）。"""
        from app.tools.qkv.signal import FrontendQueryType, FrontendSignal

        acquire = signal.get("acquire") or {}
        acquirer = acquire.get("tool", "")
        parts = acquirer.split("_", 1)
        if len(parts) != 2 or parts[0] != "qkv":
            return None
        try:
            query = FrontendQueryType(parts[1])
        except ValueError:
            return None
        args = self._resolve_args(acquire.get("args") or {}, env_context, {})
        produces = (signal.get("orchestrate") or {}).get("produces")
        if not produces:
            # 让 tool_definition 生效：signals_json 未配置 produces 时，
            # 回退到 admin-ui 配置的 tool_definition 默认值。
            produces = self._tool_def_default(acquirer, "produces") or []
        try:
            return FrontendSignal(
                query=query,
                keyword=str(args.get("keyword", "")),
                is_failed=bool(args.get("is_failed", False)),
                limit=int(args.get("limit", 100)),
                produces=produces,
            )
        except Exception:
            return None

    def _signal_to_qkv_from_step(self, step: KBDStep, env_context: dict[str, str]) -> Any:
        """从 KBDStep 构造前端信号（兜底路径）。"""
        return self._signal_to_qkv(
            {"acquire": {"tool": step.tool_name, "args": step.tool_args_template}}, env_context
        )

    def _fill_pool_from_qkv_on_step(self, step: KBDStep, res: Any) -> None:
        """兜底填充变量池（从 step 的 matcher/args 推断 produces 信息）。"""
        # step 不携带 produces，仅做尽力而为：若 raw values 为单字段则按 acquirer 后缀写入
        if not res.values:
            return
        first = res.values[0]
        if isinstance(first, dict) and len(first) == 1:
            (name, val), = first.items()
            if val is not None:
                self._set_pool_var(name, val)

    def _signal_to_qfk(self, step: KBDStep) -> Any:
        """从消费者 KBDStep 构造 qfk/signal.BackendSignal（namespace 字符串路由）。"""
        from app.tools.qfk.signal import BackendSignal

        acquirer = step.tool_name
        parts = acquirer.split("_", 1)
        if len(parts) != 2 or parts[0] != "qfk":
            return None
        namespace = parts[1]  # log/service/system/vm/...

        args = step.tool_args_template or {}

        # 获取 matcher
        matcher = step.matcher
        if not matcher:
            matcher = self._tool_def_default(acquirer, "matcher") or {}

        # 提取关键字
        keywords: list[str] = []
        if matcher.get("type") == "keyword":
            p = matcher.get("pattern", "")
            keywords = [p] if isinstance(p, str) else list(p or [])

        # 容器默认值按 namespace 语义：qfk_service 为服务组(asv)，qfk_system 为执行容器(asv-con)
        if namespace == "service":
            _container_default = "asv"
        elif namespace == "system":
            _container_default = "asv-con"
        else:
            _container_default = None

        # 构建 v2 扁平信号数据（字段名与 acquirer_args 契约一致）
        signal_data = {
            "namespace": namespace,
            "keyword": keywords,
            "match_mode": matcher.get("mode", "or"),
            "expected": bool(matcher.get("expected", True)),

            # v2 扁平字段
            "instruction": args.get("instruction"),
            "host": args.get("host"),
            "timeout": args.get("timeout", 10),

            # 特有字段
            "command": args.get("command"),
            "container": args.get("container", _container_default),
            "file": args.get("file"),
            "time_window": args.get("time_window"),
        }

        # qfk_service 契约用 resource_keyword(服务名)/command(动作)，而运行时 BackendSignal
        # 用 service/action 字段。PR#611 删除 _coerce_legacy_fields 后该映射遗漏，导致
        # signal.service 恒为 None、ServiceHandler 抛 CommandBuildError。此处补回映射。
        if namespace == "service":
            signal_data["service"] = args.get("resource_keyword")
            signal_data["action"] = args.get("command") or "status"

        try:
            return BackendSignal.from_dict(signal_data)
        except Exception:
            return None

    async def _llm_judge_batch(
        self,
        tool_name: str,
        actual_output: str,
        kbds: list[KBD],
        user_id: str = "",
    ) -> dict[str, bool]:
        """批量 LLM 判断，返回 {kbd_id: bool}。

        出错时保守地返回全 True（保留所有候选 KBD，避免误删）。
        """
        ai_client = self._ai_registry.get_client(self._assistant_type)
        if not ai_client:
            logger.warning(
                event="kbd_diag_judge_no_client",
                message="LLM 客户端不可用，保守地保留所有候选 KBD",
            )
            return {kbd.id: True for kbd in kbds}
        # 智能截取输出防止 prompt 过长（2000 字符以智能保留关键特征）
        truncated_output = smart_truncate(actual_output, 2000)

        kbd_expectations = [
            {
                "id": kbd.id,
                "name": kbd.name,
                "expected": kbd.get_expected_pattern(tool_name) or "无明确期望",
            }
            for kbd in kbds
        ]

        judge_prompt = await self._load_prompt(
            "s3_kbd_judge_v1",
            ["tool_name", "truncated_output", "kbd_expectations"],
        )
        judge_prompt = judge_prompt.format(
            tool_name=tool_name,
            truncated_output=truncated_output,
            kbd_expectations=json.dumps(kbd_expectations, ensure_ascii=False, indent=2),
        )

        try:
            result = await ai_client.invoke(
                messages=[{"role": "user", "content": judge_prompt}],
                response_format={"type": "json_object"},
                user_id=self._conversation_id or user_id,
                case_id=self._conversation_id or "",
            )
            if result.content:
                data = json.loads(result.content)
                return {k: bool(v) for k, v in data.get("matches", {}).items()}
        except Exception as exc:
            logger.warning(
                event="kbd_diag_judge_error",
                message=f"LLM 判断异常（{exc}），保守地保留所有候选 KBD",
                tool_name=tool_name,
            )
        return {kbd.id: True for kbd in kbds}

    # KBD 详情超链接模板（前端路由，部署时可据实际路径调整）
    KBD_DETAIL_URL_TEMPLATE = "/kbd/{id}"

    async def _generate_report(
        self,
        matched_kbds: list[KBD],
        steps_executed: list[StepResult],
        user_id: str = "",
    ) -> str:
        """生成诊断报告：仅返回命中 KBD 的 root_cause / solution 原始文本 + 现场证据 + 标题超链接。

        设计原则（诊断只读）：agent 只做"确认是哪篇 KBD 并返回根因/方案原文"，
        不重述、不改写，避免 LLM 二次加工引入偏差。证据即现场关键信号确认结果。
        """
        return self._build_diagnostic_report(matched_kbds, steps_executed)

    @staticmethod
    def _build_diagnostic_report(
        matched_kbds: list[KBD],
        steps_executed: list[StepResult],
        kbd_detail_url: str = KBD_DETAIL_URL_TEMPLATE,
    ) -> str:
        """构建诊断报告：根因/方案原文 + 证据 + KBD 标题可点击超链接。"""
        if not matched_kbds:
            return "诊断未能锁定具体 KBD，请联系 HCI 技术支持并提供详细故障描述。"
        blocks: list[str] = []
        for kbd in matched_kbds[:5]:
            title_link = f"[{kbd.name}]({kbd_detail_url.format(id=kbd.id)})"
            evidence: list[str] = []
            for s in steps_executed:
                if s.error and "人工授权" in s.error:
                    evidence.append("- `{s.tool_name}`：⚠️ 写操作/处置动作，需人工授权（未自动执行）")
                elif s.error is None:
                    evidence.append(f"- `{s.tool_name}`：✓ {smart_truncate(s.raw_output or '', max_chars=200)}")
                else:
                    evidence.append(f"- `{s.tool_name}`：✗ {s.error}")
            blocks.append(
                f"### 诊断结论：{title_link}\n\n"
                f"**根因（原始文本）**：\n{kbd.root_cause or '（无）'}\n\n"
                f"**解决方案（原始文本）**：\n{kbd.solution or '（无）'}\n\n"
                f"**现场证据（关键信号确认）**：\n" + ("\n".join(evidence) if evidence else "- 无")
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _fallback_report(
        matched_kbds: list[KBD],
        steps_executed: list[StepResult],
    ) -> str:
        """LLM 不可用时的降级文本报告（与 _generate_report 同源）。"""
        return KBDDiagnostic._build_diagnostic_report(matched_kbds, steps_executed)
