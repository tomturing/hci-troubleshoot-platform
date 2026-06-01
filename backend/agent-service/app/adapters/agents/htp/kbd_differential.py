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
    PATTERN_REGEX_PREFIX,
    KBDStep,
)
from app.domain.agent_port import AgentEvent, AgentStageUpdate, AgentTextChunk

logger = get_logger("kbd-differential")

# 候选 KBD 数 ≤ 此值时停止贪心消除，直接进入报告生成
EARLY_STOP_THRESHOLD = 2

# 工具执行连续失败超过此次数后停止，防止在损坏环境中无限等待
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class StepResult:
    """单步执行结果记录（用于报告生成和审计追踪）。"""

    tool_name: str
    tool_args: dict
    raw_output: str | None          # 工具执行的原始输出字符串
    error: str | None               # 执行错误（非 None 时此步骤无法判断）
    match_kbd_ids: set[str] = field(default_factory=set)  # 判断后匹配的 KBD ID 集合


@dataclass
class KBDDiagResult:
    """KBD 差异诊断最终结果。"""

    matched_kbds: list[KBD]           # 最终候选 KBD（按匹配度排序）
    steps_executed: list[StepResult]  # 已执行步骤序列
    is_definitive: bool               # True = 恰好锁定 1 个 KBD
    diagnosis_report: str             # LLM 生成的结构化诊断报告


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
        assistant_type: str = "htp-agent",
        early_stop_threshold: int = EARLY_STOP_THRESHOLD,
    ) -> None:
        self._ai_registry = ai_registry
        self._tool_executor = tool_executor
        self._diagnostic_item_client = diagnostic_item_client
        self._conversation_id = conversation_id
        self._assistant_type = assistant_type
        self._early_stop = early_stop_threshold
        self._result: KBDDiagResult | None = None

    def get_result(self) -> KBDDiagResult | None:
        """获取最近一次 diagnose() 调用的结果（调用前返回 None）。"""
        return self._result

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

        # ─── 贪心消除主循环 ──────────────────────────────────────────
        while len(remaining) > self._early_stop:
            executed_tools = {s.tool_name for s in steps_executed}

            # 1. 选择本轮最具区分度的工具
            best_tool_name = self._pick_best_step(remaining, executed_tools)
            if best_tool_name is None:
                # 所有共享步骤均已执行，退出
                break

            # 2. 构建工具执行参数（替换 env_context 占位符）
            representative_step = self._get_representative_step(remaining, best_tool_name)
            tool_args = self._resolve_args(representative_step.tool_args_template, env_context)

            yield AgentStageUpdate(
                stage="kbd_diag_step",
                metadata={
                    "tool": best_tool_name,
                    "args": tool_args,
                    "remaining_candidates": len(remaining),
                    "step_index": len(steps_executed) + 1,
                },
            )

            # 3. 执行工具
            raw_output: str | None = None
            error: str | None = None
            try:
                result = await self._tool_executor.execute(best_tool_name, tool_args)
                raw_output = str(result) if result is not None else ""
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
                        "raw_output": (raw_output or "")[:500],  # 截取前500字符
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
        return KBDStep(tool_name=tool_name, tool_args_template={}, expected_pattern="")

    @staticmethod
    def _resolve_args(template: dict, env_context: dict[str, str]) -> dict:
        """将 args 模板中的 {{placeholder}} 替换为 env_context 中的实际值。

        示例：
            template = {"vm_name": "{{vm_name}}"}
            env_context = {"vm_name": "vm-001"}
            → {"vm_name": "vm-001"}
        """
        resolved = {}
        for k, v in template.items():
            if isinstance(v, str):
                for ctx_key, ctx_val in env_context.items():
                    v = v.replace(f"{{{{{ctx_key}}}}}", ctx_val)
            resolved[k] = v
        return resolved

    async def _judge_matches(
        self,
        tool_name: str,
        actual_output: str,
        kbds: list[KBD],
        user_id: str = "",
    ) -> set[str]:
        """判断实际输出与各 KBD 期望模式的匹配度，返回匹配 KBD 的 ID 集合。

        策略（优先规则判断，降低 LLM 调用次数）：
          - __REGEX__: 正则匹配 → 程序判断
          - __CONTAINS__: 包含文本 → 程序判断
          - 自然语言 → LLM 批量判断
        """
        rule_results: dict[str, bool] = {}  # kbd_id → 规则判断结果
        llm_kbds: list[KBD] = []            # 需要 LLM 判断的 KBD

        for kbd in kbds:
            pattern = kbd.get_expected_pattern(tool_name)
            if pattern is None:
                # KBD 无此步骤定义 → 不应出现，保守地保留该 KBD
                rule_results[kbd.id] = True
                continue

            if pattern.startswith(PATTERN_REGEX_PREFIX):
                regex_str = pattern[len(PATTERN_REGEX_PREFIX):]
                try:
                    matched = bool(re.search(regex_str, actual_output, re.IGNORECASE | re.DOTALL))
                except re.error:
                    matched = False
                rule_results[kbd.id] = matched

            elif pattern.startswith(PATTERN_CONTAINS_PREFIX):
                keyword = pattern[len(PATTERN_CONTAINS_PREFIX):]
                rule_results[kbd.id] = keyword.lower() in actual_output.lower()

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

        # 截取输出防止 prompt 过长（2000 字符足以包含关键特征）
        truncated_output = actual_output[:2000]
        if len(actual_output) > 2000:
            truncated_output += f"\n... （已截取，共 {len(actual_output)} 字符）"

        kbd_expectations = [
            {
                "id": kbd.id,
                "name": kbd.name,
                "expected": kbd.get_expected_pattern(tool_name) or "无明确期望",
            }
            for kbd in kbds
        ]

        judge_prompt = (
            "你是 HCI 智能运维排障助手，正在执行 KBD 差异诊断。\n\n"
            f"已执行诊断工具：**{tool_name}**\n\n"
            "实际工具输出：\n"
            "```\n"
            f"{truncated_output}\n"
            "```\n\n"
            "请判断以上输出是否符合以下各 KBD 在此步骤的期望特征：\n\n"
            f"{json.dumps(kbd_expectations, ensure_ascii=False, indent=2)}\n\n"
            "判断规则：\n"
            "- 若实际输出包含 KBD 期望的关键特征 → true\n"
            "- 若实际输出明确不符合 KBD 期望 → false\n"
            "- 若无法确定（信息不足）→ 保守地返回 true\n\n"
            "严格返回 JSON，不要有任何额外说明：\n"
            '{"matches": {"KBD_ID_1": true, "KBD_ID_2": false}}'
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

    async def _generate_report(
        self,
        matched_kbds: list[KBD],
        steps_executed: list[StepResult],
        user_id: str = "",
    ) -> str:
        """生成结构化诊断报告，优先用 LLM，降级使用模板。"""
        ai_client = self._ai_registry.get_client(self._assistant_type)
        if not ai_client or not matched_kbds:
            return self._fallback_report(matched_kbds, steps_executed)

        steps_summary = "\n".join(
            "- 步骤 {idx}：`{name}` {status}".format(
                idx=i + 1,
                name=s.tool_name,
                status=(
                    f"✓ 输出：{(s.raw_output or '')[:200]}"
                    if s.error is None
                    else f"✗ 失败：{s.error}"
                ),
            )
            for i, s in enumerate(steps_executed)
        )

        kbds_summary = "\n".join(
            f"- **{kbd.name}**（相似度 {kbd.similarity:.0%}）\n  根因：{kbd.root_cause}\n  方案：{kbd.solution}"
            for kbd in matched_kbds[:5]
        )

        prompt = (
            "你是 HCI 智能运维排障助手，已完成 KBD 差异诊断，请生成结构化诊断报告。\n\n"
            f"诊断步骤执行情况（共 {len(steps_executed)} 步）：\n{steps_summary}\n\n"
            f"匹配 KBD（共 {len(matched_kbds)} 个）：\n{kbds_summary}\n\n"
            "报告要求（Markdown 格式）：\n"
            "1. **故障确认**：最可能的根因（1-2句）\n"
            "2. **诊断依据**：关键步骤发现的具体异常\n"
            "3. **处理建议**：按优先级列出 3-5 个操作步骤\n"
            "4. **参考文档**：最匹配 KBD 名称及编号\n\n"
            "面向 HCI 运维工程师，简洁专业，不要有多余废话。"
        )

        try:
            result = await ai_client.invoke(
                messages=[{"role": "user", "content": prompt}],
                user_id=self._conversation_id or user_id,
                case_id=self._conversation_id or "",
            )
            return result.content or self._fallback_report(matched_kbds, steps_executed)
        except Exception as exc:
            logger.warning(event="kbd_diag_report_error", error=str(exc))
            return self._fallback_report(matched_kbds, steps_executed)

    @staticmethod
    def _fallback_report(
        matched_kbds: list[KBD],
        steps_executed: list[StepResult],
    ) -> str:
        """LLM 不可用时的降级文本报告。"""
        if not matched_kbds:
            return "诊断未能锁定具体 KBD，请联系 HCI 技术支持并提供详细故障描述。"
        top = matched_kbds[0]
        return (
            f"**诊断结果**\n\n"
            f"最匹配 KBD：{top.name}\n\n"
            f"根因分析：{top.root_cause}\n\n"
            f"处理建议：{top.solution}\n\n"
            f"诊断共执行 {len(steps_executed)} 个步骤。"
        )
