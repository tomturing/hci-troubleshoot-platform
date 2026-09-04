"""
backend/kb-service/app/services/signal_orchestrator.py
关键信号多 Agent 分层建模调度器 (SignalExtractionOrchestrator):
1. 计数 Agent (CountAgent): 纯内容驱动边界切分与角色感知去重
2. 分类 Agent (ClassifyAgent): 13 类受限 Catalog 双重视角对抗审查
3. 建模 Agent (ModelAgent): 注入最佳实践黄金案例与全局共享变量契约
4. 验证 Agent (VerifyAgent): 全局对账、DAG 拓扑连通性校验与门禁自愈闭环
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any

from shared.database.postgres import DatabaseManager
from shared.observability.otel import get_current_trace_id
from shared.utils.prompt_loader import StrictPromptLoader

from app.services.signal_asset_service import SignalAssetService

logger = logging.getLogger("signal_orchestrator")

# 13 类封闭受限 Catalog (5 QKV + 8 QFK)
VALID_CATALOG_TOOLS = frozenset(
    {
        "qkv_task",
        "qkv_alert",
        "qkv_dialog",
        "qkv_vm_console",
        "qkv_effect",
        "qfk_log",
        "qfk_system",
        "qfk_vm",
        "qfk_service",
        "qfk_network",
        "qfk_storage",
        "qfk_hardware",
        "qfk_platform",
    }
)

DEFAULT_SHARED_VARIABLES = [
    "HOST",
    "VM",
    "REQUEST_ID",
    "STORAGE_ID",
    "LOG_DATE",
    "STATUS",
    "ERRCODE_TRACING",
    "TARGET",
    "END",
    "DATE",
]
VALID_ROLE_TYPES = frozenset({"producer", "consumer"})

# 候选发现只负责从原文找出“可能是信号”的证据，不负责替代分类或建模。
_PRODUCER_MARKERS = re.compile(r"失败|报错|异常|错误|故障|不可用|无法|超时|拒绝|中断|告警|恢复|成功")
_CONSUMER_ACTIONS = re.compile(
    r"查看|检查|确认|执行|获取|收集|查询|读取|分析|验证|观察|登录|重试|清理|导出|过滤|对比|定位"
)
_COMMAND_MARKER = re.compile(
    r"(?:^|\s)(?:sudo\s+)?(?:acli|virsh|kubectl|systemctl|journalctl|dmesg|grep|egrep|awk|sed|cat|tail|head|ps|df|du|mount|ping|ip|curl)\b",
    re.I,
)
_LOG_FILE_MARKER = re.compile(
    r"(?:[A-Za-z0-9_.-]+\.(?:log|out|err)|/var/log/[^\s，。；;]+|/sf/log/[^\s，。；;]+)", re.I
)


def discover_signal_candidates(composite_text: str, steps_text: str) -> list[dict[str, Any]]:
    """基于原文的高召回候选发现。

    该层刻意不推断工具和 JSON，只输出可追溯证据。LLM 计数结果可覆盖或补充它，
    但不能让一个 malformed 候选抹掉其他候选。
    """
    candidates: list[dict[str, Any]] = []

    def lines(value: str) -> list[str]:
        chunks = re.split(r"[\r\n]+|(?<=[。！？!?；;])\s*", value or "")
        return [re.sub(r"\s+", " ", chunk).strip(" \t-•") for chunk in chunks if chunk.strip()]

    def add(raw: str, *, source_kind: str, role_type: str, method: str = "rule") -> None:
        evidence = raw.strip()
        if not evidence:
            return
        # 用证据而非字段标签生成稳定 key，保证复合字段与步骤重复时可去重。
        key = re.sub(r"[^\w\u4e00-\u9fff]+", "", evidence).casefold()
        if not key or any(item["_dedup_key"] == key for item in candidates):
            return
        candidates.append(
            {
                "candidate_id": f"discovered_{len(candidates) + 1:03d}",
                "intent_id": f"discovered_{len(candidates) + 1:03d}",
                "core_entity": evidence,
                "evidence_raw": evidence,
                "source_kind": source_kind,
                "role_type": role_type,
                "discovery_method": method,
                "uncertain": True,
                "_dedup_key": key,
            }
        )

    for sentence in lines(composite_text):
        if _PRODUCER_MARKERS.search(sentence):
            add(sentence, source_kind="composite", role_type="producer")

    # 排查步骤通常包含“动作、命令、预期结果、判断依据”多行说明；一个编号步骤只
    # 形成一个消费者候选，避免把解释性文字误计为多个信号。
    step_blocks = re.split(r"(?m)(?=^\s*\d+[.、)]\s*)", steps_text or "")
    step_blocks = [block for block in step_blocks if block.strip()]
    if not step_blocks:
        step_blocks = lines(steps_text)
    for block in step_blocks:
        block_lines = lines(block)
        evidence = next(
            (line for line in block_lines if _COMMAND_MARKER.search(line) or _LOG_FILE_MARKER.search(line)),
            next((line for line in block_lines if _CONSUMER_ACTIONS.search(line)), ""),
        )
        if evidence:
            add(evidence, source_kind="steps", role_type="consumer")

    # 同一故障常在标题、问题描述、告警中逐层展开；长证据已覆盖短证据时只保留
    # 信息量最大的句子。过短片段不参与包含去重，避免把“失败”等词误当实体。
    kept: list[dict[str, Any]] = []
    for item in sorted(candidates, key=lambda value: len(value["evidence_raw"]), reverse=True):
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", item["evidence_raw"]).casefold()
        if len(normalized) >= 6 and any(
            normalized in re.sub(r"[^\w\u4e00-\u9fff]+", "", other["evidence_raw"]).casefold()
            and item["role_type"] == other["role_type"]
            for other in kept
        ):
            continue
        kept.append(item)
    candidates = kept
    # 复合字段是同一故障的多段叙述，默认最多保留 3 个生产者候选；步骤候选不设
    # 固定上限，遵循“一步一信号”的业务约束。
    composite_candidates = [item for item in candidates if item["source_kind"] == "composite"]
    step_candidates = [item for item in candidates if item["source_kind"] != "composite"]
    candidates = (
        sorted(composite_candidates, key=lambda value: len(value["evidence_raw"]), reverse=True)[:3] + step_candidates
    )
    for item in candidates:
        item.pop("_dedup_key", None)
    return candidates


class SignalExtractionOrchestrator:
    def __init__(
        self,
        db_manager: DatabaseManager,
        llm_caller: Callable[[str, str], Any] | None = None,
        *,
        persist_failures: bool = True,
        prompt_templates: dict[str, str] | None = None,
        best_practices: dict[str, list[dict[str, Any]]] | None = None,
    ):
        self.db_manager = db_manager
        self.llm_caller = llm_caller
        self.persist_failures = persist_failures
        self.prompt_templates = prompt_templates or {}
        self.best_practices = best_practices or {}
        # Shadow 评估使用的阶段指标，不写入业务表。
        self.last_diagnostics: dict[int, dict[str, Any]] = {}
        concurrency = max(1, int(os.environ.get("SIGNAL_MULTI_AGENT_CONCURRENCY", "3")))
        self._llm_semaphore = asyncio.Semaphore(concurrency)

    async def _load_prompt(self, name: str, placeholders: list[str], consumer: str) -> str:
        """每次加载 Prompt 使用独立 session，避免并发 Agent 共享 AsyncSession。"""
        if name in self.prompt_templates:
            template = self.prompt_templates[name]
            actual = StrictPromptLoader.get_template_placeholders(template)
            if actual != set(placeholders):
                raise ValueError(f"离线 Prompt {name} 占位符不匹配: {sorted(actual)}")
            return template
        async with self.db_manager.async_session_factory() as prompt_session:
            return await StrictPromptLoader.load_and_validate(
                prompt_session,
                name,
                placeholders,
                consumer=consumer,
                trace_id=get_current_trace_id(),
            )

    async def _record_failure(
        self,
        session: Any,
        *,
        kbd_id: int,
        stage: str,
        raw_content: str,
        reason: str,
        detail_payload: dict[str, Any] | None = None,
    ) -> int:
        """异常复盘优先走独立事务，避免随业务 session 回滚。"""
        return await SignalAssetService.record_failure(
            session,
            db_manager=self.db_manager,
            kbd_id=kbd_id,
            stage=stage,
            raw_content=raw_content,
            reason=reason,
            detail_payload=detail_payload,
            persist=self.persist_failures,
        )

    async def _get_best_practices(self, tool_name: str, limit: int = 3) -> list[dict[str, Any]]:
        """最佳实践查询使用独立 session，避免与并发建模任务共享会话。"""
        if tool_name in self.best_practices:
            return self.best_practices[tool_name][:limit]
        async with self.db_manager.async_session_factory() as asset_session:
            return await SignalAssetService.get_best_practices_by_tool(asset_session, tool_name, limit=limit)

    async def _invoke_llm(self, prompt: str, stage_name: str, *, kbd_id: int) -> dict[str, Any]:
        """统一调用 LLM 并进行安全 JSON 反序列化"""
        if self.llm_caller is not None:
            async with self._llm_semaphore:
                res = await self.llm_caller(prompt, stage_name)
            if isinstance(res, dict):
                logger.info("多 Agent LLM 响应阶段=%s 字段=%s", stage_name, sorted(res.keys()))
                return res
            if isinstance(res, str):
                return self._parse_json_from_text(res)
            return {}

        # 默认回退到系统全局 openai/llm 接口 (从 extract_signals 导入 _call_llm)
        from app.routes.extract_signals import _call_llm

        async with self._llm_semaphore:
            res = await _call_llm(prompt, source_type=f"multi_agent:{stage_name}", source_id=kbd_id)
        if isinstance(res, dict):
            logger.info("多 Agent LLM 响应阶段=%s 字段=%s", stage_name, sorted(res.keys()))
            return res
        return {}

    @staticmethod
    def _parse_json_from_text(content: str) -> dict[str, Any]:
        normalized = content.strip().lstrip("\ufeff")
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", normalized, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            normalized = fenced.group(1).strip()
        payload = json.loads(normalized)
        if not isinstance(payload, dict):
            raise ValueError("LLM 响应必须为顶层 JSON 对象")
        return payload

    async def run_count_agent(
        self,
        session: Any,
        kbd_id: int,
        composite_text: str,
        steps_text: str,
    ) -> tuple[int, list[dict[str, Any]]]:
        """执行计数 Agent：边界切分与角色感知去重"""
        try:
            prompt_template = await self._load_prompt(
                "kbd_signal_count_v1", ["composite_text", "steps_text"], "kb-service.signal_extract.count"
            )
            prompt = prompt_template.format(
                composite_text=composite_text[:4000],
                steps_text=steps_text[:6000],
            )
            res = await self._invoke_llm(prompt, "count", kbd_id=kbd_id)
            intents = res.get("intents") or res.get("signal_intents") or res.get("candidates") or []
            signal_count = int(res.get("signal_count", len(intents) if intents else 0))
            if not isinstance(intents, list) or (signal_count == 0 and not intents):
                uncountable = res.get("uncountable_reason") or "计数 Agent 返回意图为空"
                await self._record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="count",
                    raw_content=f"{composite_text}\n{steps_text}",
                    reason="UNCOUNTABLE",
                    detail_payload={"llm_response": res, "message": uncountable},
                )
                return 0, []
            if signal_count <= 0:
                await self._record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="count",
                    raw_content=f"{composite_text}\n{steps_text}",
                    reason="COUNT_MISMATCH",
                    detail_payload={"signal_count": signal_count, "intent_count": len(intents)},
                )
                return 0, []
            malformed = [
                item
                for item in intents
                if not isinstance(item, dict)
                or not str(item.get("core_entity") or "").strip()
                or not str(item.get("evidence_raw") or "").strip()
                or item.get("role_type") not in VALID_ROLE_TYPES
            ]
            if malformed:
                await self._record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="count",
                    raw_content=f"{composite_text}\n{steps_text}",
                    reason="COUNT_INTENT_INVALID",
                    detail_payload={"invalid_intents": malformed},
                )
                intents = [item for item in intents if item not in malformed]
            if not intents:
                return 0, []
            if composite_text.strip() and not any(item.get("role_type") == "producer" for item in intents):
                await self._record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="count",
                    raw_content=f"{composite_text}\n{steps_text}",
                    reason="COUNT_PRODUCER_MISSING",
                    detail_payload={"intent_count": len(intents)},
                )
                # 规则候选层会补充 producer；不阻断当前有效 consumer。
            source_text = f"{composite_text}\n{steps_text}"
            source_normalized = re.sub(r"\s+", "", source_text).casefold()

            def grounded(evidence: str) -> bool:
                # 允许模型用省略号压缩同一字段内的长证据，但每个保留片段都必须逐字存在。
                fragments = [part for part in re.split(r"\.{3,}|…{1,}", evidence) if part.strip()]
                return all(re.sub(r"\s+", "", fragment).casefold() in source_normalized for fragment in fragments)

            ungrounded = [item for item in intents if not grounded(str(item["evidence_raw"]))]
            if ungrounded:
                await self._record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="count",
                    raw_content=source_text,
                    reason="COUNT_EVIDENCE_UNGROUNDED",
                    detail_payload={"ungrounded_intents": ungrounded},
                )
                intents = [item for item in intents if item not in ungrounded]
            if not intents:
                return 0, []
            deduplicated: dict[str, dict[str, Any]] = {}
            for item in intents:
                key = re.sub(r"[\W_]+", "", str(item["core_entity"]), flags=re.UNICODE).casefold()
                existing = deduplicated.get(key)
                if existing is None or (item.get("source_kind") == "steps" and existing.get("source_kind") != "steps"):
                    deduplicated[key] = item
            normalized_intents = list(deduplicated.values())
            if signal_count != len(normalized_intents):
                await self._record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="count",
                    raw_content=source_text,
                    reason="COUNT_MISMATCH",
                    detail_payload={
                        "reported_count": signal_count,
                        "intent_count": len(intents),
                        "deduplicated_count": len(normalized_intents),
                    },
                )
            return len(normalized_intents), normalized_intents
        except Exception as exc:
            logger.warning("计数 Agent 执行异常 kbd_id=%d: %s", kbd_id, exc)
            await self._record_failure(
                session,
                kbd_id=kbd_id,
                stage="count",
                raw_content=f"{composite_text}\n{steps_text}",
                reason="COUNT_AGENT_EXCEPTION",
                detail_payload={"error": str(exc)},
            )
            return 0, []

    async def run_classify_agent(
        self,
        session: Any,
        kbd_id: int,
        intent: dict[str, Any],
        composite_text: str,
        steps_text: str,
        acquirer_catalog_text: str,
        category_baseline: str,
    ) -> dict[str, Any]:
        """执行分类 Agent：单意图 vs 全局上下文双向对抗审查"""
        core_entity = str(intent.get("core_entity") or "")
        evidence_raw = str(intent.get("evidence_raw") or "")

        try:
            prompt_template = await self._load_prompt(
                "kbd_signal_classify_v1",
                [
                    "core_entity",
                    "evidence_raw",
                    "composite_text",
                    "steps_text",
                    "acquirer_catalog",
                    "category_baseline",
                ],
                "kb-service.signal_extract.classify",
            )
            prompt = prompt_template.format(
                core_entity=core_entity,
                evidence_raw=evidence_raw,
                composite_text=composite_text[:2000],
                steps_text=steps_text[:4000],
                acquirer_catalog=acquirer_catalog_text,
                category_baseline=category_baseline,
            )
            res = await self._invoke_llm(prompt, "classify", kbd_id=kbd_id)
            tool_name = str(res.get("tool_name") or "").strip()
            if tool_name not in VALID_CATALOG_TOOLS:
                await self._record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="classify",
                    raw_content=f"entity: {core_entity}\nevidence: {evidence_raw}",
                    reason="UNCLASSIFIED",
                    detail_payload={"llm_response": res, "invalid_tool": tool_name},
                )
                return {"tool_name": "unclassified", "valid": False, "intent": intent}
            if not (0.0 <= float(res.get("confidence", 0.0)) <= 1.0):
                raise ValueError("分类 confidence 必须在 0 到 1 之间")
            return {
                "tool_name": tool_name,
                "category": res.get("category", "backend" if tool_name.startswith("qfk_") else "frontend"),
                "rationale": res.get("rationale", ""),
                "confidence": float(res.get("confidence", 0.9)),
                "valid": True,
                "intent": intent,
            }
        except Exception as exc:
            logger.warning("分类 Agent 执行异常 kbd_id=%d intent=%s: %s", kbd_id, core_entity, exc)
            await self._record_failure(
                session,
                kbd_id=kbd_id,
                stage="classify",
                raw_content=f"entity: {core_entity}\nevidence: {evidence_raw}",
                reason="CLASSIFY_AGENT_EXCEPTION",
                detail_payload={"error": str(exc)},
            )
            return {"tool_name": "unclassified", "valid": False, "intent": intent}

    async def run_model_agent(
        self,
        session: Any,
        kbd_id: int,
        classified: dict[str, Any],
        acli_catalog_text: str,
        dynamic_shared_variables: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """执行建模 Agent：注入同类型黄金最佳实践与全局变量契约"""
        tool_name = classified["tool_name"]
        intent = classified["intent"]
        core_entity = str(intent.get("core_entity") or "")
        evidence_raw = str(intent.get("evidence_raw") or "")
        candidate_id = str(intent.get("candidate_id") or intent.get("intent_id") or "").strip()

        # 动态拉取同类型黄金实践并加上防污染标识
        best_practices = await self._get_best_practices(tool_name, limit=3)
        bp_formatted = (
            json.dumps(
                [
                    {
                        "category": b["pattern_category"],
                        "signal": b["signal_json"],
                        "notes": f"[示例参考范式，严禁抄袭示例中的特定文件名/变量名] {b['design_notes']}",
                    }
                    for b in best_practices
                ],
                ensure_ascii=False,
                indent=2,
            )
            if best_practices
            else "当前暂无特定实例，请遵循通用契约"
        )

        effective_vars = dynamic_shared_variables if dynamic_shared_variables is not None else DEFAULT_SHARED_VARIABLES
        shared_vars_text = ", ".join(sorted(set(effective_vars)))
        try:
            prompt_template = await self._load_prompt(
                "kbd_signal_model_v1",
                ["tool_name", "core_entity", "evidence_raw", "shared_variables", "best_practices", "acli_catalog"],
                "kb-service.signal_extract.model",
            )
            prompt = prompt_template.format(
                tool_name=tool_name,
                core_entity=core_entity,
                evidence_raw=evidence_raw,
                shared_variables=shared_vars_text,
                best_practices=bp_formatted,
                acli_catalog=acli_catalog_text,
            )
            res = await self._invoke_llm(prompt, "model", kbd_id=kbd_id)
            # 校验是否返回合法单信号结构
            candidate = None
            if isinstance(res, dict) and "acquire" in res:
                candidate = res
            # 若包裹在 signals 中
            elif isinstance(res, dict) and isinstance(res.get("signals"), list) and res["signals"]:
                candidate = res["signals"][0]
            if isinstance(candidate, dict):
                acquire = candidate.get("acquire") or {}
                if acquire.get("tool") != tool_name or not isinstance(acquire.get("args"), dict):
                    raise ValueError("建模结果 acquire.tool/args 与分类结果不一致")
                if not isinstance(candidate.get("orchestrate"), dict):
                    raise ValueError("建模结果缺少 orchestrate 段")
                provenance = candidate.get("provenance") or {}
                model_evidence = str(provenance.get("evidence") or "").strip() if isinstance(provenance, dict) else ""
                source_evidence = re.sub(r"\s+", "", evidence_raw).casefold()
                generated_evidence = re.sub(r"\s+", "", model_evidence).casefold()
                if source_evidence:
                    if not generated_evidence:
                        raise ValueError("建模 provenance.evidence 为空")
                    is_exact_subset = generated_evidence in source_evidence
                    overlap_chars = len(set(generated_evidence) & set(source_evidence))
                    if not is_exact_subset:
                        if overlap_chars < 3:
                            # 与候选证据毫无字符交集，确属外部/Few-Shot 污染
                            raise ValueError("建模 provenance.evidence 未逐字取自候选原文，疑似混入 Few-Shot 内容")
                        # 若存在实质重叠（微小改写或修饰），自动回填候选原文证据，既阻断污染又避免误杀
                        logger.info("建模 evidence 存在改写，已安全纠偏回填候选原文: candidate_id=%s", candidate_id)
                        if not isinstance(candidate.get("provenance"), dict):
                            candidate["provenance"] = {}
                        candidate["provenance"]["evidence"] = evidence_raw

                # 清洗 orchestrate.requires 中未在白名单且未在 acquire 中引用的悬空变量（如模型臆造的 VM_DISK_PATH）
                orchestrate = candidate.get("orchestrate") or {}
                if isinstance(orchestrate, dict):
                    requires = orchestrate.get("requires")
                    if isinstance(requires, list):
                        allowed_var_set = set(effective_vars)
                        acquire_str = json.dumps(candidate.get("acquire") or {})
                        cleaned_requires = []
                        for req in requires:
                            req_name = str(req).strip()
                            if req_name in allowed_var_set or f"{{{{{req_name}}}}}" in acquire_str:
                                cleaned_requires.append(req_name)
                            else:
                                logger.info(
                                    "剔除未闭合且未被消费的悬空变量: candidate_id=%s var=%s", candidate_id, req_name
                                )
                        orchestrate["requires"] = cleaned_requires

                # 模型生成的 id 不具备身份权威性；最终信号 id 固定绑定原始候选。
                if candidate_id:
                    candidate["id"] = candidate_id
                return candidate
            await self._record_failure(
                session,
                kbd_id=kbd_id,
                stage="modeling",
                raw_content=f"tool: {tool_name}\nentity: {core_entity}",
                reason="UNMODELABLE",
                detail_payload={"llm_response": res},
            )
            return None
        except Exception as exc:
            logger.warning("建模 Agent 执行异常 kbd_id=%d tool=%s: %s", kbd_id, tool_name, exc)
            await self._record_failure(
                session,
                kbd_id=kbd_id,
                stage="modeling",
                raw_content=f"tool: {tool_name}\nentity: {core_entity}",
                reason="MODEL_AGENT_EXCEPTION",
                detail_payload={"error": str(exc)},
            )
            return None

    async def run_verify_and_self_heal(
        self,
        session: Any,
        kbd_id: int,
        raw_count: int,
        raw_signals: list[dict[str, Any]],
        rejected_candidates: list[dict[str, Any]],
        kbd_context: str,
        gate_checker_fn: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """执行验证 Agent：全局对账、DAG 连通性分析与门禁自愈闭环"""
        # 第一轮门禁检测
        validated, rejected, gate_issues = gate_checker_fn(raw_signals)
        all_rejected = list(rejected_candidates) + list(rejected)

        # 检查是否全部通过且无严重阻断
        if not gate_issues and len(validated) == raw_count:
            return validated, all_rejected

        count_mismatch = len(validated) + len(all_rejected) != raw_count
        # 存在门禁错误或数量不守恒时，启动 1 轮智能自愈修复回路。
        if gate_issues or count_mismatch:
            logger.info("验证 Agent 发现门禁阻断 issues，启动智能自愈修正: %s", gate_issues)
            try:
                prompt_template = await self._load_prompt(
                    "kbd_signal_verify_v1",
                    ["signals_json", "rejected_candidates", "raw_count", "kbd_context", "gate_issues"],
                    "kb-service.signal_extract.verify",
                )
                prompt = prompt_template.format(
                    signals_json=json.dumps(
                        validated + [r.get("signal", r) for r in all_rejected], ensure_ascii=False, indent=2
                    ),
                    rejected_candidates=json.dumps(all_rejected, ensure_ascii=False),
                    raw_count=raw_count,
                    kbd_context=kbd_context[:3000],
                    gate_issues="; ".join(gate_issues),
                )
                heal_res = await self._invoke_llm(prompt, "verify", kbd_id=kbd_id)
                healed_signals = heal_res.get("signals") if isinstance(heal_res, dict) else []
                allowed_ids = {str(item.get("id")) for item in raw_signals if isinstance(item, dict) and item.get("id")}
                allowed_ids.update(
                    str((item.get("signal") or {}).get("id"))
                    for item in all_rejected
                    if isinstance(item.get("signal"), dict) and (item.get("signal") or {}).get("id")
                )
                allowed_ids.update(str(item.get("candidate_id")) for item in all_rejected if item.get("candidate_id"))
                # 验证 Agent 只能修复原候选，禁止凭空新增没有 candidate id 的信号。
                healed_ids = (
                    {
                        str(item.get("candidate_id") or item.get("id"))
                        for item in healed_signals
                        if isinstance(item, dict) and item.get("id")
                    }
                    if isinstance(healed_signals, list)
                    else set()
                )
                if (
                    isinstance(healed_signals, list)
                    and healed_signals
                    and heal_res.get("verification_status") == "passed"
                    and len(healed_signals) == raw_count
                    and healed_ids
                    and healed_ids.issubset(allowed_ids)
                ):
                    # 自愈后复检
                    v2, r2, issues2 = gate_checker_fn(healed_signals)
                    if not issues2 and len(v2) + len(r2) == raw_count:
                        logger.info("自愈修正成功：原通过 %d -> 自愈通过 %d", len(validated), len(v2))
                        # 过滤掉已经成功自愈并进入 v2 的候选，避免幽灵残留
                        v2_ids = {
                            str(item.get("candidate_id") or item.get("id"))
                            for item in v2
                            if isinstance(item, dict) and (item.get("id") or item.get("candidate_id"))
                        }
                        remaining_rejected = [
                            r
                            for r in all_rejected
                            if str(r.get("candidate_id") or (r.get("signal") or {}).get("id")) not in v2_ids
                        ]
                        return v2, remaining_rejected + list(r2)
            except Exception as exc:
                logger.warning("自愈回路异常 kbd_id=%d: %s", kbd_id, exc)

        # 若未自愈或无自愈，返回当前结果并记录复盘日志
        if all_rejected or len(validated) != raw_count:
            await self._record_failure(
                session,
                kbd_id=kbd_id,
                stage="verification",
                raw_content=f"raw_count: {raw_count}, validated: {len(validated)}, rejected: {len(all_rejected)}",
                reason="VERIFY_REJECTED",
                detail_payload={"issues": gate_issues, "rejected_count": len(all_rejected)},
            )
        return validated, all_rejected

    async def extract_kbd_signals_pipeline(
        self,
        session: Any,
        kbd_id: int,
        entry_data: dict[str, Any],
        acquirer_catalog_text: str,
        acli_catalog_text: str,
        gate_checker_fn: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]],
        image_evidence_text: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        """多 Agent 分层协同主调度管线"""
        title = entry_data.get("title", "")
        problem_desc = entry_data.get("problem_description", "")
        alert_info = entry_data.get("alert_info", "")
        steps_text = entry_data.get("steps_text", "")
        category_baseline = entry_data.get("category_baseline") or entry_data.get("category_id", "")

        # 下游只接收字段内容；字段名不得成为分类信号，避免“告警信息”被误判为 qkv_alert。
        composite_text = "\n".join(part for part in (title, problem_desc, alert_info) if str(part).strip())
        if image_evidence_text.strip():
            # 图片 OCR 是诊断输入的一部分；不传字段标签，只追加已筛选的证据内容。
            composite_text = "\n".join(part for part in (composite_text, image_evidence_text) if part.strip())
        kbd_context = "\n".join(part for part in (composite_text, steps_text) if str(part).strip())

        # 阶段 0: 确定性候选发现，只输出可追溯证据，不推断工具或 JSON。
        discovered = discover_signal_candidates(composite_text, steps_text)
        diagnostics: dict[str, Any] = {
            "discovered_candidate_count": len(discovered),
            "valid_intent_count": 0,
            "count_rejected_count": 0,
            "classified_count": 0,
            "unclassified_count": 0,
            "modeled_count": 0,
            "modeling_failed_count": 0,
            "validated_count": 0,
            "rejected_count": 0,
        }

        # 阶段 1: 计数 Agent
        raw_count, intents = await self.run_count_agent(session, kbd_id, composite_text, steps_text)
        diagnostics["valid_intent_count"] = len(intents)
        diagnostics["count_rejected_count"] = max(0, len(discovered) - len(intents))
        if not intents:
            # 单个 malformed/越权响应不应阻断其他候选；规则候选仍需经过后续分类与门禁。
            intents = discovered
            raw_count = len(intents)
            if not intents:
                logger.warning("计数 Agent 与候选发现均未产出意图 kbd_id=%d", kbd_id)
                diagnostics["rejected_count"] = 1
                diagnostics["failure_stage"] = "count"
                self.last_diagnostics[kbd_id] = diagnostics
                return [], [{"reason_code": "run_failed", "reason": "未发现可追溯候选"}], 0

        # 合并 LLM 漏掉的规则候选，证据相同的候选只保留一条。
        known = {re.sub(r"[^\w\u4e00-\u9fff]+", "", str(item.get("evidence_raw") or "")).casefold() for item in intents}
        for candidate in discovered:
            key = re.sub(r"[^\w\u4e00-\u9fff]+", "", candidate["evidence_raw"]).casefold()
            # 证据只要有明显文本重叠就视为同一候选，避免同一故障的长短改写被重复
            # 拉起分类/建模 Agent。完全不同的规则候选才作为 LLM 漏检补充。
            overlaps = any(
                key in existing
                or existing in key
                or (
                    len(set(key) & set(existing)) / max(1, len(set(key) | set(existing))) >= 0.35
                    and candidate.get("source_kind") == "composite"
                )
                for existing in known
                if key and existing
            )
            if key and key not in known and not overlaps:
                intents.append(candidate)
                known.add(key)
        raw_count = len(intents)
        for index, intent in enumerate(intents, start=1):
            intent["candidate_id"] = f"kbd_{kbd_id}_candidate_{index:03d}"
        diagnostics["valid_intent_count"] = len(intents)

        # 阶段 2: 分类 Agent (并发双向对抗)
        classify_tasks = [
            self.run_classify_agent(
                session, kbd_id, intent, composite_text, steps_text, acquirer_catalog_text, str(category_baseline)
            )
            for intent in intents
        ]
        classified_results = await asyncio.gather(*classify_tasks)

        valid_classified = [c for c in classified_results if c.get("valid")]
        diagnostics["classified_count"] = len(valid_classified)
        diagnostics["unclassified_count"] = len(classified_results) - len(valid_classified)
        rejected_candidates = [
            {
                "candidate_id": (c.get("intent") or {}).get("candidate_id"),
                "signal": c.get("intent"),
                "reason_code": "run_failed",
                "reason": "无法映射到 13 类受限 Catalog",
            }
            for c in classified_results
            if not c.get("valid")
        ]

        # 阶段 3: 建模 Agent (按 DAG 拓扑分层建模：Producer 优先并导出动态符号表 -> 注入 Consumer)
        def _is_producer_candidate(c: dict[str, Any]) -> bool:
            tool = str(c.get("tool_name") or "")
            role = str((c.get("intent") or {}).get("role_type") or "")
            return tool.startswith("qkv_") or role == "producer"

        producer_indices = [i for i, c in enumerate(valid_classified) if _is_producer_candidate(c)]
        consumer_indices = [i for i, c in enumerate(valid_classified) if not _is_producer_candidate(c)]

        modeled_signals: list[dict[str, Any] | None] = [None] * len(valid_classified)

        # 3.1 优先并发建模生产者（QKV / producer）
        if producer_indices:
            producer_tasks = [
                self.run_model_agent(
                    session,
                    kbd_id,
                    valid_classified[i],
                    acli_catalog_text,
                    dynamic_shared_variables=DEFAULT_SHARED_VARIABLES,
                )
                for i in producer_indices
            ]
            producer_results = await asyncio.gather(*producer_tasks)
            for idx, res_sig in zip(producer_indices, producer_results, strict=True):
                modeled_signals[idx] = res_sig

        # 3.2 动态汇聚已成功生产的变量符号表，并在生产者信号中为 END 自动派生 DATE
        discovered_variables: set[str] = set()
        for sig in modeled_signals:
            if isinstance(sig, dict):
                orch = sig.get("orchestrate") or {}
                if isinstance(orch, dict):
                    produces = orch.get("produces")
                    if isinstance(produces, list):
                        has_end = False
                        has_date = False
                        end_path = "end"
                        for prod in produces:
                            if isinstance(prod, dict):
                                var_name = prod.get("name") or prod.get("alias")
                                if var_name and re.match(r"^[A-Z][A-Z0-9_]*$", str(var_name)):
                                    discovered_variables.add(str(var_name).strip())
                                if "END" in {str(prod.get("name")), str(prod.get("alias"))}:
                                    has_end = True
                                    end_path = prod.get("path") or "end"
                                if "DATE" in {str(prod.get("name")), str(prod.get("alias"))}:
                                    has_date = True
                        tool = str((sig.get("acquire") or {}).get("tool") or "")
                        if has_end and not has_date and (tool in {"qkv_alert", "qkv_task"} or tool.startswith("qkv_")):
                            produces.append({"name": "DATE", "path": end_path})
                            discovered_variables.add("DATE")

        if "END" in discovered_variables:
            discovered_variables.add("DATE")

        active_shared_variables = sorted(set(DEFAULT_SHARED_VARIABLES) | discovered_variables)
        diagnostics["discovered_variables"] = list(discovered_variables)

        # 3.3 将动态符号表上下文注入消费者（QFK / consumer）并发建模
        if consumer_indices:
            consumer_tasks = [
                self.run_model_agent(
                    session,
                    kbd_id,
                    valid_classified[i],
                    acli_catalog_text,
                    dynamic_shared_variables=active_shared_variables,
                )
                for i in consumer_indices
            ]
            consumer_results = await asyncio.gather(*consumer_tasks)
            for idx, res_sig in zip(consumer_indices, consumer_results, strict=True):
                modeled_signals[idx] = res_sig

        diagnostics["modeled_count"] = sum(sig is not None for sig in modeled_signals)
        diagnostics["modeling_failed_count"] = sum(sig is None for sig in modeled_signals)

        raw_signals = []
        for idx, sig in enumerate(modeled_signals):
            if sig is not None:
                # 赋予规范 id
                if "id" not in sig or not sig["id"]:
                    sig["id"] = f"sig_{idx + 1:03d}"
                raw_signals.append(sig)
            else:
                rejected_candidates.append(
                    {
                        "signal": valid_classified[idx].get("intent"),
                        "candidate_id": (valid_classified[idx].get("intent") or {}).get("candidate_id"),
                        "reason_code": "run_failed",
                        "reason": "建模 Agent 生成失败",
                    }
                )

        # 阶段 4: 验证 Agent (对账、DAG 拓扑与自愈门禁)
        validated, final_rejected = await self.run_verify_and_self_heal(
            session, kbd_id, raw_count, raw_signals, rejected_candidates, kbd_context, gate_checker_fn
        )

        diagnostics["validated_count"] = len(validated)
        diagnostics["rejected_count"] = len(final_rejected)
        self.last_diagnostics[kbd_id] = diagnostics

        return validated, final_rejected, raw_count
