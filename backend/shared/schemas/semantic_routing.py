"""在线、离线与审核试运行共用的非执行语义路由。

只返回待验证候选；不生成命令、不写变量池、不裁决根因。
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from shared.observability.langfuse import observe_workflow, update_observation
from shared.observability.logger import get_logger
from shared.observability.metrics import KBD_WORKFLOW_DURATION_SECONDS, KBD_WORKFLOW_ITEMS_TOTAL
from shared.observability.redaction import redact_observation_value
from shared.resolution.product_versions import product_version_matches
from shared.schemas.semantic_entry import (
    has_strong_producer,
    lexical_profile_score,
    normalize_case_context,
    profile_digest,
    profile_text,
    semantic_context_text,
    semantic_entry_profile,
    semantic_segment_weights,
)

logger = get_logger("semantic-routing")
ROUTING_VERSION = "semantic-route-v2"
MAX_EXECUTION_CANDIDATES = 3
_VECTOR_CACHE: OrderedDict[tuple[str, str], list[list[float]]] = OrderedDict()
REASON_LABELS = {
    "strong_producer_matched": "任务、告警或弹框已有命中，保持强证据路径",
    "strong_producer_unavailable": "强生产者查询不完整或不可用，不能当作未命中",
    "strong_producer_absent": "分类内没有强生产者入口，按客户描述选择待验证候选",
    "strong_producer_no_match": "强生产者已确认未命中，进入语义候选验证",
    "no_safe_semantic_candidate": "没有找到满足筛选条件的候选",
    "missing_description": "缺少当前故障描述",
    "ambiguous_candidates": "相似候选过多，需先补充区分信息",
    "guidance_only": "仅能提供人工补证据指引，不能自动确认根因",
    "capability_gap": "当前平台没有可信验证能力",
    "consumer_not_executable": "消费者未通过执行契约检查",
    "exclusion_anchor_hit": "描述命中不适用条件",
    "positive_anchor_missing": "描述未命中必须出现的关键信息",
}
SCOPE_LABELS = {
    "product": "产品",
    "product_version": "产品版本",
    "component": "组件",
    "object_type": "对象类型",
    "operation": "故障发生时的操作",
}


def reason_text(reason: str) -> str:
    prefix, _, field = reason.partition(":")
    if prefix == "missing_scope":
        return "尚未提供" + SCOPE_LABELS.get(field, field)
    if prefix == "scope_mismatch":
        return SCOPE_LABELS.get(field, field) + "不在该 KBD 的适用范围内"
    return REASON_LABELS.get(reason, reason)


def text_chunks(text: str, limit: int = 500) -> list[str]:
    """全文有界切片，长描述的尾部也参与检索；片段绝不转成执行变量。"""
    return [
        text[offset : offset + limit]
        for offset in range(0, min(len(text), 16000), limit)
        if text[offset : offset + limit].strip()
    ]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right) or not all(math.isfinite(v) for v in [*left, *right]):
        raise ValueError("向量维度或数值无效")
    denominator = math.sqrt(sum(v * v for v in left) * sum(v * v for v in right))
    return max(0.0, sum(a * b for a, b in zip(left, right, strict=True)) / denominator) if denominator else 0.0


def scope_rejection(profile: dict[str, Any], context: Any) -> str | None:
    scope = profile.get("applicability") or {}
    source = context if isinstance(context, dict) else {}
    for field, values in scope.items():
        if not values:
            continue
        actual = str(source.get(field) or "").strip()
        if not actual:
            return f"missing_scope:{field}"
        if field == "product_version":
            matched = any(product_version_matches(actual, str(rule)) for rule in values)
        else:
            matched = actual.casefold() in {str(v).casefold() for v in values}
        if not matched:
            return f"scope_mismatch:{field}"
    return None


async def resolve_candidates(
    *,
    entries: list[dict[str, Any]],
    context: Any,
    strong_status: str,
    embed: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
    embedding_namespace: str = "",
    top_k: int = 5,
    mode: str = "online",
) -> dict[str, Any]:
    """从服务端权威分类快照选择候选，并返回可读的全部排除原因。"""
    # 可读观测仍必须去除显式密码／令牌，不把秘密交给外部向量服务。
    started = time.monotonic()
    context = redact_observation_value(context)
    segments = normalize_case_context(context)
    with observe_workflow(
        name="semantic_fallback_route",
        input={"segments": segments, "strong_producer_status": strong_status},
        metadata={"mode": mode, "routing_version": ROUTING_VERSION},
    ) as observation:
        result = await _resolve(entries, context, segments, strong_status, embed, embedding_namespace, top_k)
        result["routing_version"] = ROUTING_VERSION
        result["reason_text"] = reason_text(result["reason"])
        result["embedding_namespace"] = embedding_namespace or None
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        for candidate in result["filtered_candidates"]:
            candidate["reason_text"] = reason_text(candidate["reason"])
        KBD_WORKFLOW_ITEMS_TOTAL.labels(
            workflow="semantic_fallback",
            stage=mode,
            status=result["decision"],
            error_code="embedding_unavailable" if result.get("degraded") else "",
        ).inc()
        KBD_WORKFLOW_DURATION_SECONDS.labels(workflow="semantic_fallback", stage=mode).observe(
            time.monotonic() - started
        )
        update_observation(observation, output=result)
        return result


async def _resolve(entries, context, segments, strong_status, embed, namespace, top_k):
    result: dict[str, Any] = {
        "decision": "inconclusive",
        "reason": "no_safe_semantic_candidate",
        "candidates": [],
        "filtered_candidates": [],
        "context_segments": segments,
        "degraded": embed is None,
    }
    strong_defined = any(has_strong_producer(entry.get("signals_json")) for entry in entries)
    if strong_status not in {"no_match", "not_applicable"} or (strong_status == "not_applicable" and strong_defined):
        result.update(
            decision="strong_producer_first",
            reason="strong_producer_matched" if strong_status == "matched" else "strong_producer_unavailable",
        )
        return result
    if not segments:
        result.update(
            reason="missing_description",
            next_action={"type": "ask_clarifying_question", "question": "请补充当前故障现象和完整报错。"},
        )
        return result
    text = semantic_context_text(context)
    ranked = []
    for entry in entries:
        profile = semantic_entry_profile(entry.get("signals_json"))
        if not profile:
            continue
        capability = profile.get("diagnosis_capability")
        rejected = "capability_gap" if capability == "capability_gap" else scope_rejection(profile, context)
        if capability == "executable" and entry.get("executable") is False:
            rejected = "consumer_not_executable"
        score, hits, excluded = lexical_profile_score(profile, text)
        rejected = rejected or ("exclusion_anchor_hit" if excluded else "positive_anchor_missing" if not hits else None)
        identity = {
            "kbd_id": str(entry["id"]),
            "support_id": entry.get("support_id"),
            "title": entry.get("title") or entry.get("name"),
            "profile_digest": profile_digest(profile),
            "resource_revision": entry.get("resource_revision"),
        }
        if rejected:
            result["filtered_candidates"].append({**identity, "reason": rejected, "exclusion_anchor_hits": excluded})
            continue
        ranked.append(
            (
                entry,
                profile,
                {
                    **identity,
                    "diagnosis_capability": capability,
                    "score": score,
                    "matched_positive_anchors": hits,
                    "canonical_symptoms": profile.get("canonical_symptoms", []),
                    "source_refs": profile.get("source_refs", []),
                    "applicability": profile.get("applicability", {}),
                    "manual_evidence_request": profile.get("manual_evidence_request", []),
                    "score_parts": {"anchor_score": score},
                },
            )
        )
    total_executable = sum(candidate["diagnosis_capability"] == "executable" for _, _, candidate in ranked)
    total_guidance = sum(candidate["diagnosis_capability"] == "guidance_only" for _, _, candidate in ranked)
    # 先明确范围与文字门禁，再有界排序，避免大分类逐篇请求外部模型。
    # 总数在截断前统计，不能因只重排十篇就隐瞒其余歧义候选。
    ranked.sort(
        key=lambda item: (item[2]["diagnosis_capability"] != "executable", -item[2]["score"], str(item[0]["id"]))
    )
    result["ranking_pool_limit"] = 10
    ranked = ranked[:10]
    if ranked and embed:
        try:
            segment_inputs = [(name, chunk) for name, value in segments.items() for chunk in text_chunks(value)]
            queries = await embed([value for _, value in segment_inputs])
            if len(queries) != len(segment_inputs):
                raise ValueError("输入向量数量不一致")
            weights = semantic_segment_weights(segments)
            for _, profile, candidate in ranked:
                key = (namespace, profile_digest(profile))
                vectors = _VECTOR_CACHE.get(key) if namespace else None
                if vectors is None:
                    chunks = text_chunks(redact_observation_value(profile_text(profile)))
                    vectors = await embed(chunks)
                    if len(vectors) != len(chunks) or not vectors:
                        raise ValueError("画像向量数量不一致")
                    if namespace:
                        _VECTOR_CACHE[key] = vectors
                        _VECTOR_CACHE.move_to_end(key)
                        while len(_VECTOR_CACHE) > 512:
                            _VECTOR_CACHE.popitem(last=False)
                per_segment: dict[str, float] = {}
                for (name, _), query in zip(segment_inputs, queries, strict=True):
                    per_segment[name] = max(per_segment.get(name, 0), max(_cosine(query, value) for value in vectors))
                vector_score = sum(weights[name] * value for name, value in per_segment.items())
                candidate["score"] = round(0.45 * candidate["score_parts"]["anchor_score"] + 0.55 * vector_score, 6)
                candidate["score_parts"].update(
                    vector_score=vector_score, vector_segment_scores=per_segment, vector_segment_weights=weights
                )
        except Exception as exc:
            logger.warning(event="semantic_entry_embedding_failed", error=str(exc), fallback="anchor_score")
            result["degraded"] = True
            # 整批一致降级，不能让前半批向量分数与后半批文字分数竞争。
            for _, _, candidate in ranked:
                candidate["score"] = candidate["score_parts"]["anchor_score"]
                candidate["score_parts"] = {"anchor_score": candidate["score"]}
    ranked.sort(key=lambda item: (-item[2]["score"], str(item[0]["id"])))
    executable = [candidate for _, _, candidate in ranked if candidate["diagnosis_capability"] == "executable"]
    guidance = [candidate for _, _, candidate in ranked if candidate["diagnosis_capability"] == "guidance_only"]
    result["candidate_count"] = total_executable or total_guidance
    result["candidates"] = (executable or guidance)[: max(1, min(top_k, 10))]
    if executable and total_executable <= MAX_EXECUTION_CANDIDATES:
        result.update(
            decision="executable",
            reason="strong_producer_absent" if strong_status == "not_applicable" else "strong_producer_no_match",
        )
    else:
        questions = [
            question
            for _, profile, _ in ranked
            for question in profile.get("clarifying_questions", [])
            if question.strip()
        ]
        requests = [
            question for candidate in guidance for question in candidate["manual_evidence_request"] if question.strip()
        ]
        missing = sorted(
            {
                item["reason"].split(":", 1)[1]
                for item in result["filtered_candidates"]
                if item["reason"].startswith("missing_scope:")
            }
        )
        if not questions and missing:
            questions = [
                "请补充已确认的"
                + "、".join(SCOPE_LABELS.get(field, field) for field in missing)
                + "；这些信息只用于选择候选，不会作为命令执行目标。"
            ]
        if not questions and len(executable) > MAX_EXECUTION_CANDIDATES:
            anchors = {
                anchor: sum(anchor in candidate["matched_positive_anchors"] for candidate in executable)
                for candidate in executable
                for anchor in candidate["matched_positive_anchors"]
            }
            distinguishers = list(
                dict.fromkeys(
                    symptom
                    for _, profile, _ in ranked
                    for symptom in profile.get("canonical_symptoms", [])
                    if symptom not in text
                )
            )[:3]
            if distinguishers and anchors:
                questions = ["以下哪项更符合当前现象？请补充实际报错，不要按猜测选择：" + "；".join(distinguishers)]
        result.update(
            reason="ambiguous_candidates" if executable else "guidance_only" if guidance else result["reason"],
            next_action={
                "type": "manual_evidence_request" if guidance else "ask_clarifying_question",
                "question": next(
                    iter(questions or requests), "请补充完整报错、故障发生的对象和操作；若仍无法区分，请转人工复核。"
                ),
            },
        )
    return result
