"""标题优先的只读案例推荐；检索分数不进入 CDD，不生成执行目标或命令。"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from typing import Any

from shared.observability.logger import get_logger
from shared.observability.redaction import redact_observation_value
from shared.schemas.semantic_entry import capability_of, lexical_profile_score, profile_text, semantic_context_text

logger = get_logger("kbd-recommendation")
_VECTORS: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_GENERIC = ("虚拟机", "请问", "如何", "怎么", "问题", "故障", "帮忙", "处理", "解决")


def _terms(text: str) -> set[str]:
    """中文双字词配合完整英文/错误码；泛化对象词不单独构成命中依据。"""
    for word in _GENERIC:
        text = text.replace(word, " ")
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_.-]+", text.casefold()))
    for part in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.update(part[i : i + 2] for i in range(len(part) - 1))
    return tokens


def _current_text(text: str) -> str:
    """保守丢弃否定/历史分句，防止向量把已否定的旧症状重新召回。"""
    return "。".join(
        clause.strip()
        for clause in re.split(r"[，,。；;\n]", text)
        if clause.strip()
        and not re.search(r"以前|曾经|此前已解决|之前已解决|未出现|没有出现|没有发生|并非|不是|排除", clause)
        and not clause.strip().startswith("没有")
    )


async def recommend_cases(
    *,
    entries: list[dict[str, Any]],
    context: Any,
    embed=None,
    namespace: str = "",
    top_k: int = 5,
) -> dict[str, Any] | None:
    """同一候选池比较标题/正文问题字段/可选画像；仅返回未验证的参考。"""
    from shared.schemas.semantic_routing import _cosine, scope_rejection

    query = _current_text(semantic_context_text(context))
    query_terms = _terms(query)
    if len(query_terms) < 2:
        return None
    pool = []
    for entry in entries:
        # 旧画像可能只有锚点和补证据问题，缺正文时保留原有人工指引路径。
        if not any(
            str(entry.get(field) or "").strip()
            for field in ("problem_description", "content_md", "root_cause", "solution")
        ):
            continue
        document = entry.get("signals_json") or {}
        capability = capability_of(document)
        if capability == "capability_gap":
            continue
        if capability not in {"reference_only", "guidance_only"} and entry.get("executable") is not True:
            continue
        profile = (document.get("semantic_entry_profile") or {}) if isinstance(document, dict) else {}
        # 已有 Verification Contract 的适用范围同样约束标题推荐。
        contract_scope = (document.get("verification_contract") or {}).get("scope") or {}
        applicability = dict(profile.get("applicability") or {})
        for source, target in (("products", "product"), ("versions", "product_version"), ("components", "component")):
            if contract_scope.get(source) and target not in applicability:
                applicability[target] = contract_scope[source]
        profile = {**profile, "applicability": applicability}
        scope_issue = scope_rejection(profile, context)
        if scope_issue and scope_issue.startswith("scope_mismatch:"):
            continue
        _, anchors, excluded = lexical_profile_score(profile, semantic_context_text(context))
        if excluded:
            continue
        answers = context.get("semantic_answers", {}) if isinstance(context, dict) else {}
        if any(
            choice.get("effect") == "exclude" and answers.get(question.get("id")) == choice.get("id")
            for question in profile.get("semantic_disambiguation", [])
            for choice in question.get("choices", [])
        ):
            continue
        title = str(entry.get("title") or entry.get("name") or "").strip()
        if not title:
            continue
        problem = str(entry.get("problem_description") or "")[:2500]
        title_terms = _terms(title)
        problem_terms = _terms(problem + " " + str(entry.get("alert_info") or "") + " " + profile_text(profile))
        overlap = query_terms & (title_terms | problem_terms)
        title_score = len(query_terms & title_terms) / max(1, len(query_terms))
        problem_score = len(query_terms & problem_terms) / max(1, len(query_terms))
        lexical = max(title_score, 0.8 * problem_score, min(1, 0.35 * len(anchors)))
        # 根因/解决方案只用于展示，不拼入问题侧向量，避免把历史答案当作客户事实。
        retrieval_text = redact_observation_value(
            f"标题：{title[:500]}\n问题描述：{problem}\n报错：{str(entry.get('alert_info') or '')[:1000]}"
            f"\n可选画像：{profile_text(profile)[:1000]}"
        )
        digest = hashlib.sha256(retrieval_text.encode()).hexdigest()
        pool.append(
            {
                "entry": entry,
                "text": retrieval_text,
                "digest": digest,
                "lexical": lexical,
                "overlap": sorted(overlap),
                "anchors": anchors,
                "scope_issue": scope_issue,
                "applicability": profile.get("applicability", {}),
                "vector": 0.0,
            }
        )
    degraded = embed is None
    if pool and embed:
        try:
            query_vectors = await embed([query])
            if len(query_vectors) != 1:
                raise ValueError("问题向量数量不一致")
            missing = list(
                {item["digest"]: item for item in pool if (namespace, item["digest"]) not in _VECTORS}.values()
            )
            vectors = {}
            # 批量覆盖整个分类候选，不能先按字面匹配截断而漏掉同义表达。
            for offset in range(0, len(missing), 32):
                batch = missing[offset : offset + 32]
                result = await embed([item["text"] for item in batch])
                if len(result) != len(batch):
                    raise ValueError("案例向量数量不一致")
                for item, vector in zip(batch, result, strict=True):
                    _cosine(query_vectors[0], vector)
                    vectors[item["digest"]] = vector
            for item in pool:
                key = (namespace, item["digest"])
                vector = vectors.get(item["digest"], _VECTORS.get(key))
                item["vector"] = _cosine(query_vectors[0], vector)
            if namespace:
                for digest, vector in vectors.items():
                    _VECTORS[namespace, digest] = vector
                while len(_VECTORS) > 1024:
                    _VECTORS.popitem(last=False)
        except Exception as exc:
            logger.warning(event="kbd_recommendation_embedding_failed", error=str(exc))
            degraded = True
            for item in pool:
                item["vector"] = 0.0
    candidates = []
    for item in pool:
        lexical = item["lexical"]
        vector = item["vector"]
        # 初始保守召回阈值，仅代表相关性，不是根因概率；保留多篇供用户核对。
        if not ((lexical >= 0.45 and len(item["overlap"]) >= 2) or item["anchors"] or vector >= 0.78):
            continue
        entry = item["entry"]
        score = max(lexical, 0.55 * vector + 0.45 * lexical)
        candidates.append(
            {
                "kbd_id": str(entry["id"]),
                "support_id": entry.get("support_id"),
                "title": entry.get("title") or entry.get("name"),
                "resource_revision": entry.get("resource_revision"),
                "profile_digest": item["digest"],
                "diagnosis_capability": capability_of(entry.get("signals_json")) or "executable",
                "score": round(score, 6),
                "score_parts": {"lexical": lexical, "vector": vector},
                "matched_positive_anchors": item["anchors"] or item["overlap"][:8],
                "recommendation_conclusion": str(entry.get("root_cause") or "")[:2000],
                "recommendation_solution": str(entry.get("solution") or "")[:3000],
                "problem_excerpt": str(entry.get("problem_description") or entry.get("content_md") or "")[:1500],
                "applicability": item["applicability"],
                "scope_warning": item["scope_issue"],
                "verified": False,
                "retrieval_source": "title_problem_profile",
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["kbd_id"]))
    if not candidates:
        return None
    ambiguous = len(candidates) > 1 and candidates[0]["score"] - candidates[1]["score"] < 0.12
    return {
        "decision": "case_recommendations",
        "reason": "case_recommendations",
        "candidates": candidates[: max(1, min(top_k, 5))],
        "candidate_count": len(candidates),
        "filtered_candidates": [],
        "degraded": degraded,
        "ambiguous": ambiguous,
        "next_action": {
            "type": "compare_cases" if ambiguous else "review_reference",
            "question": "请对照这些案例的问题描述，补充当前完整报错或适用版本以区分。" if ambiguous else "",
        },
    }
