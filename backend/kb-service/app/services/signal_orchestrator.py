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
import re
from typing import Any, Callable

from app.services.signal_asset_service import SignalAssetService
from shared.database.postgres import DatabaseManager
from shared.utils.prompt_loader import StrictPromptLoader

logger = logging.getLogger("signal_orchestrator")

# 13 类封闭受限 Catalog (5 QKV + 8 QFK)
VALID_CATALOG_TOOLS = frozenset({
    "qkv_task", "qkv_alert", "qkv_dialog", "qkv_vm_console", "qkv_effect",
    "qfk_log", "qfk_system", "qfk_vm", "qfk_service", "qfk_network",
    "qfk_storage", "qfk_hardware", "qfk_platform"
})

DEFAULT_SHARED_VARIABLES = ["HOST", "VM", "REQUEST_ID", "STORAGE_ID", "STATUS", "ERRCODE_TRACING", "TARGET", "END"]


class SignalExtractionOrchestrator:
    def __init__(
        self,
        db_manager: DatabaseManager,
        llm_caller: Callable[[str, str], Any] | None = None,
    ):
        self.db_manager = db_manager
        self.llm_caller = llm_caller

    async def _invoke_llm(self, prompt: str, stage_name: str) -> dict[str, Any]:
        """统一调用 LLM 并进行安全 JSON 反序列化"""
        if self.llm_caller is not None:
            res = await self.llm_caller(prompt, stage_name)
            if isinstance(res, dict):
                return res
            if isinstance(res, str):
                return self._parse_json_from_text(res)
            return {}

        # 默认回退到系统全局 openai/llm 接口 (从 extract_signals 导入 _call_llm)
        from app.routes.extract_signals import _call_llm
        res = await _call_llm(prompt, source_type="multi_agent", source_id=0)
        return res if isinstance(res, dict) else {}

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
        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            "kbd_signal_count_v1",
            ["composite_text", "steps_text"],
            consumer="kb-service.signal_extract.count",
        )
        prompt = prompt_template.format(
            composite_text=composite_text[:4000],
            steps_text=steps_text[:6000],
        )

        try:
            res = await self._invoke_llm(prompt, "count")
            signal_count = int(res.get("signal_count", 0))
            intents = res.get("intents") or []
            if not isinstance(intents, list) or (signal_count == 0 and not intents):
                uncountable = res.get("uncountable_reason") or "计数 Agent 返回意图为空"
                await SignalAssetService.record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="count",
                    raw_content=f"{composite_text}\n{steps_text}",
                    reason="UNCOUNTABLE",
                    detail_payload={"llm_response": res, "message": uncountable},
                )
                return 0, []
            return signal_count, intents
        except Exception as exc:
            logger.warning("计数 Agent 执行异常 kbd_id=%d: %s", kbd_id, exc)
            await SignalAssetService.record_failure(
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
        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            "kbd_signal_classify_v1",
            ["core_entity", "evidence_raw", "composite_text", "steps_text", "acquirer_catalog", "category_baseline"],
            consumer="kb-service.signal_extract.classify",
        )
        core_entity = str(intent.get("core_entity") or "")
        evidence_raw = str(intent.get("evidence_raw") or "")

        prompt = prompt_template.format(
            core_entity=core_entity,
            evidence_raw=evidence_raw,
            composite_text=composite_text[:2000],
            steps_text=steps_text[:4000],
            acquirer_catalog=acquirer_catalog_text,
            category_baseline=category_baseline,
        )

        try:
            res = await self._invoke_llm(prompt, "classify")
            tool_name = str(res.get("tool_name") or "").strip()
            if tool_name not in VALID_CATALOG_TOOLS:
                await SignalAssetService.record_failure(
                    session,
                    kbd_id=kbd_id,
                    stage="classify",
                    raw_content=f"entity: {core_entity}\nevidence: {evidence_raw}",
                    reason="UNCLASSIFIED",
                    detail_payload={"llm_response": res, "invalid_tool": tool_name},
                )
                return {"tool_name": "unclassified", "valid": False, "intent": intent}
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
            await SignalAssetService.record_failure(
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
    ) -> dict[str, Any] | None:
        """执行建模 Agent：注入同类型黄金最佳实践与全局变量契约"""
        tool_name = classified["tool_name"]
        intent = classified["intent"]
        core_entity = str(intent.get("core_entity") or "")
        evidence_raw = str(intent.get("evidence_raw") or "")

        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            "kbd_signal_model_v1",
            ["tool_name", "core_entity", "evidence_raw", "shared_variables", "best_practices", "acli_catalog"],
            consumer="kb-service.signal_extract.model",
        )

        # 动态拉取同类型黄金实践
        best_practices = await SignalAssetService.get_best_practices_by_tool(session, tool_name, limit=3)
        bp_formatted = json.dumps(
            [{"category": b["pattern_category"], "signal": b["signal_json"], "notes": b["design_notes"]} for b in best_practices],
            ensure_ascii=False, indent=2
        ) if best_practices else "当前暂无特定实例，请遵循通用契约"

        shared_vars_text = ", ".join(DEFAULT_SHARED_VARIABLES)
        prompt = prompt_template.format(
            tool_name=tool_name,
            core_entity=core_entity,
            evidence_raw=evidence_raw,
            shared_variables=shared_vars_text,
            best_practices=bp_formatted,
            acli_catalog=acli_catalog_text,
        )

        try:
            res = await self._invoke_llm(prompt, "model")
            # 校验是否返回合法单信号结构
            if isinstance(res, dict) and "acquire" in res:
                return res
            # 若包裹在 signals 中
            if isinstance(res, dict) and isinstance(res.get("signals"), list) and res["signals"]:
                return res["signals"][0]
            await SignalAssetService.record_failure(
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
            await SignalAssetService.record_failure(
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

        # 若存在阻断错误，启动 1 轮智能自愈修复回路
        if gate_issues:
            logger.info("验证 Agent 发现门禁阻断 issues，启动智能自愈修正: %s", gate_issues)
            try:
                prompt_template = await StrictPromptLoader.load_and_validate(
                    session,
                    "kbd_signal_verify_v1",
                    ["signals_json", "rejected_candidates", "raw_count", "kbd_context", "gate_issues"],
                    consumer="kb-service.signal_extract.verify",
                )
                prompt = prompt_template.format(
                    signals_json=json.dumps(validated + [r.get("signal", r) for r in all_rejected], ensure_ascii=False, indent=2),
                    rejected_candidates=json.dumps(all_rejected, ensure_ascii=False),
                    raw_count=raw_count,
                    kbd_context=kbd_context[:3000],
                    gate_issues="; ".join(gate_issues),
                )
                heal_res = await self._invoke_llm(prompt, "verify")
                healed_signals = heal_res.get("signals") if isinstance(heal_res, dict) else []
                if isinstance(healed_signals, list) and healed_signals:
                    # 自愈后复检
                    v2, r2, issues2 = gate_checker_fn(healed_signals)
                    if not issues2 or len(v2) > len(validated):
                        logger.info("自愈修正成功：原通过 %d -> 自愈通过 %d", len(validated), len(v2))
                        return v2, list(all_rejected) + list(r2)
            except Exception as exc:
                logger.warning("自愈回路异常 kbd_id=%d: %s", kbd_id, exc)

        # 若未自愈或无自愈，返回当前结果并记录复盘日志
        if all_rejected:
            await SignalAssetService.record_failure(
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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        """多 Agent 分层协同主调度管线"""
        title = entry_data.get("title", "")
        problem_desc = entry_data.get("problem_description", "")
        alert_info = entry_data.get("alert_info", "")
        steps_text = entry_data.get("steps_text", "")
        category_id = entry_data.get("category_id", "")

        composite_text = f"【标题】：{title}\n【问题描述】：{problem_desc}\n【告警信息】：{alert_info}"
        kbd_context = f"{composite_text}\n【排查步骤】：{steps_text}"

        # 阶段 1: 计数 Agent
        raw_count, intents = await self.run_count_agent(session, kbd_id, composite_text, steps_text)
        if not intents:
            logger.warning("计数 Agent 未提取出有效意图 kbd_id=%d", kbd_id)
            return [], [], 0

        # 阶段 2: 分类 Agent (并发双向对抗)
        classify_tasks = [
            self.run_classify_agent(
                session, kbd_id, intent, composite_text, steps_text, acquirer_catalog_text, category_id
            )
            for intent in intents
        ]
        classified_results = await asyncio.gather(*classify_tasks)

        valid_classified = [c for c in classified_results if c.get("valid")]
        rejected_candidates = [
            {"signal": c.get("intent"), "reason_code": "run_failed", "reason": "无法映射到 13 类受限 Catalog"}
            for c in classified_results if not c.get("valid")
        ]

        # 阶段 3: 建模 Agent (并发注入最佳实践与全局变量契约)
        model_tasks = [
            self.run_model_agent(session, kbd_id, c, acli_catalog_text)
            for c in valid_classified
        ]
        modeled_signals = await asyncio.gather(*model_tasks)

        raw_signals = []
        for idx, sig in enumerate(modeled_signals):
            if sig is not None:
                # 赋予规范 id
                if "id" not in sig or not sig["id"]:
                    sig["id"] = f"sig_{idx+1:03d}"
                raw_signals.append(sig)
            else:
                rejected_candidates.append({
                    "signal": valid_classified[idx].get("intent"),
                    "reason_code": "run_failed",
                    "reason": "建模 Agent 生成失败",
                })

        # 阶段 4: 验证 Agent (对账、DAG 拓扑与自愈门禁)
        validated, final_rejected = await self.run_verify_and_self_heal(
            session, kbd_id, raw_count, raw_signals, rejected_candidates, kbd_context, gate_checker_fn
        )

        return validated, final_rejected, raw_count
