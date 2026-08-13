"""
Matcher 求值单一真相源（Single Source of Truth）

统一处理 KBD 差异诊断（全部 7 类）与 QFK 引擎（keyword 类）的 Matcher 求值逻辑，
消除在线和离线模式中重复且可能漂移的实现，并统一证据链结构。

支持类型：keyword / regex / state / threshold / delta / trend / exists。

求值契约
--------
``evaluate_matcher(matcher, text) -> MatcherResult``
  - matched: 确定性布尔；None 表示无法定值（交由 LLM 兜底）
  - detail : 结构化信息（命中关键字、原始 hit、模式等），供调用方构造证据链
  - evidence: 人类可读的求值证据片段

关键字组合语义（match_mode）
--------------------------
  - or  ：任一关键字命中即判定为真
  - and ：全部关键字命中才判定为真
  - not ：所有关键字均不出现才判定为真（取代旧 expected=False 的取反语义，
          因此 not 模式忽略 expected，直接以"均不出现"为命中）
其余类型（regex/state/threshold/exists）按 expected 翻转。

禁止 import ``app.*`` 或 ``shared.observability.*``，确保在线/离线/回放完全一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from shared.signals.extractor import QFKExtractionError, extract_output_values


def _normalize_mode(mode: str | None) -> str:
    return (mode or "or").lower()


@dataclass
class MatcherResult:
    """Matcher 求值结果。

    matched=None 表示无法做确定性求值（调用方应降级到 LLM 兜底）。
    """

    matched: bool | None
    detail: dict[str, Any] = field(default_factory=dict)
    evidence: str = ""


@dataclass
class _KeywordEval:
    hit: bool
    matched_kws: list[str]


def _eval_keyword(kws: list[str], records: list[str], mode: str) -> _KeywordEval:
    """逐条候选记录求值；AND 不能由跨行分别出现的关键字拼成。"""

    normalized_records = [record.casefold() for record in records]
    matched_kws = [k for k in kws if any(k.casefold() in record for record in normalized_records)]
    if mode == "and":
        hit = bool(kws) and any(
            all(keyword.casefold() in record for keyword in kws)
            for record in normalized_records
        )
    elif mode == "not":
        hit = len(matched_kws) == 0
    else:  # or（默认）
        hit = bool(matched_kws)
    return _KeywordEval(hit=hit, matched_kws=matched_kws)


def evaluate_matcher(
    matcher: dict[str, Any] | None,
    text: str,
    *,
    precomputed_values: list[float] | None = None,
    precomputed_detail: dict[str, Any] | None = None,
) -> MatcherResult:
    """对单条 Matcher 契约做确定性（非 LLM）布尔求值。

    参数：
        matcher: Matcher dict（来自信号 schema 或 KBD expected_pattern 序列化）。
        text: 实际工具输出文本。
    返回：
        MatcherResult：matched 为 None 时调用方应交给 LLM 兜底。
    """
    if not isinstance(matcher, dict):
        return MatcherResult(matched=None)
    mtype = matcher.get("type", "")
    expected = bool(matcher.get("expected", True))
    if not isinstance(matcher.get("extract"), dict):
        return MatcherResult(
            matched=None,
            detail={"error": "QFK_EXTRACT_INVALID_SPEC: Matcher 必须配置新版 extract"},
            evidence="【Matcher 求值】取值配置缺失",
        )

    if mtype == "keyword":
        p = matcher.get("pattern", "")
        kws = [p] if isinstance(p, str) else list(p or [])
        kws = [k for k in kws if k]
        if not kws:
            return MatcherResult(matched=None)
        mode = _normalize_mode(matcher.get("mode"))
        predicate_text, extraction_detail, extraction_error = _extract_predicate_text(text, matcher, "string")
        if extraction_error and not extraction_error.startswith("QFK_NO_MATCH:"):
            return MatcherResult(
                matched=None,
                detail={"error": extraction_error, **extraction_detail},
                evidence=f"【Matcher 求值 (keyword)】取值失败: {extraction_error}",
            )
        predicate_text = predicate_text or ""
        extracted_values = ((extraction_detail.get("extract") or {}).get("values") or [])
        records = [str(value) for value in extracted_values] or [predicate_text]
        ev = _eval_keyword(kws, records, mode)
        hit, matched_kws = ev.hit, ev.matched_kws

        matched = hit if mode == "not" else (hit if expected else not hit)
        mode_str = mode.upper()
        evidence = (
            f"【Matcher 求值 (keyword/{mode_str})】\n"
            f"目标关键字: {kws}\n"
            f"命中关键字: {matched_kws}\n"
            f"原始命中(hit): {hit}\n"
            f"期望 expected: {expected}\n"
            f"最终判定: {matched}"
        )
        return MatcherResult(
            matched=matched,
            detail={"matched_keywords": matched_kws, "hit": hit, "mode": mode, **extraction_detail},
            evidence=evidence,
        )

    if mtype == "regex":
        p = matcher.get("pattern", "")
        if not isinstance(p, str) or not p:
            return MatcherResult(matched=None)
        predicate_text, extraction_detail, extraction_error = _extract_predicate_text(text, matcher, "string")
        if extraction_error and not extraction_error.startswith("QFK_NO_MATCH:"):
            return MatcherResult(
                matched=None,
                detail={"error": extraction_error, **extraction_detail},
                evidence=f"【Matcher 求值 (regex)】取值失败: {extraction_error}",
            )
        try:
            hit = bool(re.search(p, predicate_text or "", re.IGNORECASE | re.DOTALL))
        except re.error:
            return MatcherResult(
                matched=False,
                detail={"hit": False, "error": "invalid_regex"},
                evidence=f"【Matcher 求值 (regex)】非法正则 pattern: {p}\n最终判定: False",
            )
        matched = hit if expected else not hit
        return MatcherResult(
            matched=matched,
            detail={"hit": hit, **extraction_detail},
            evidence=(
                f"【Matcher 求值 (regex)】\npattern: {p}\n"
                f"原始命中(hit): {hit}\n期望 expected: {expected}\n最终判定: {matched}"
            ),
        )

    if mtype == "state":
        p = matcher.get("pattern", "")
        patterns = [p] if isinstance(p, str) else list(p or [])
        patterns = [str(item).strip().casefold() for item in patterns if str(item).strip()]
        if not patterns:
            return MatcherResult(matched=None)
        values, extraction_detail, extraction_error = _extract_predicate_values(text, matcher, "string")
        if extraction_error and not extraction_error.startswith("QFK_NO_MATCH:"):
            return MatcherResult(
                matched=None,
                detail={"error": extraction_error, **extraction_detail},
                evidence=f"【Matcher 求值 (state)】取值失败: {extraction_error}",
            )
        hit = any(str(value).strip().casefold() in patterns for value in values)
        matched = hit if expected else not hit
        return MatcherResult(
            matched=matched,
            detail={"hit": hit, **extraction_detail},
            evidence=(
                f"【Matcher 求值 (state)】\n期望状态: {p}\n"
                f"原始命中(hit): {hit}\n期望 expected: {expected}\n最终判定: {matched}"
            ),
        )

    if mtype == "threshold":
        aggregation = str(matcher.get("aggregation") or "first_number")
        if aggregation in {"line_count", "duration_seconds"}:
            predicate_text, extraction_detail, extraction_error = _extract_predicate_text(text, matcher, "string")
            values: list[float] = []
        else:
            predicate_text = text
            values, extraction_detail, extraction_error = _resolve_numeric_values(
                text,
                matcher,
                precomputed_values=precomputed_values,
                precomputed_detail=precomputed_detail,
            )
        if extraction_error:
            return MatcherResult(
                matched=None,
                detail={"error": extraction_error, **extraction_detail},
                evidence=f"【Matcher 求值 (threshold)】文本取值失败: {extraction_error}",
            )
        if aggregation == "line_count":
            selected = (extraction_detail.get("extract") or {}).get("selected_lines") or []
            val = float(len(selected))
        elif aggregation == "duration_seconds":
            val = _extract_duration_seconds(predicate_text)
        elif aggregation == "last_number":
            val = values[-1] if values else None
        elif aggregation == "max":
            val = max(values) if values else None
        elif aggregation == "min":
            val = min(values) if values else None
        elif aggregation == "sum":
            val = sum(values) if values else None
        else:
            val = values[0] if values else None
        target = matcher.get("value")
        op = matcher.get("operator", ">")
        if val is None or target is None:
            return MatcherResult(matched=None)
        try:
            target = float(target)
        except (TypeError, ValueError):
            return MatcherResult(matched=None)
        cmp = _compare_threshold(val, target, op)
        if cmp is None:
            return MatcherResult(matched=None)
        matched = cmp if expected else not cmp
        return MatcherResult(
            matched=matched,
            detail={
                "hit": cmp,
                "value": val,
                "target": target,
                "operator": op,
                "aggregation": aggregation,
                "metric": matcher.get("metric"),
                **extraction_detail,
            },
            evidence=(
                f"【Matcher 求值 (threshold/{aggregation})】\n提取数值: {val} {op} {target}\n"
                f"比较结果: {cmp}\n期望 expected: {expected}\n最终判定: {matched}"
            ),
        )

    if mtype == "delta":
        values, extraction_detail, extraction_error = _resolve_numeric_values(
            text,
            matcher,
            precomputed_values=precomputed_values,
            precomputed_detail=precomputed_detail,
        )
        if extraction_error:
            return MatcherResult(
                matched=None,
                detail={"error": extraction_error, **extraction_detail},
                evidence=f"【Matcher 求值 (delta)】文本取值失败: {extraction_error}",
            )
        if len(values) < int(matcher.get("minimum_samples") or 2):
            return MatcherResult(
                matched=None,
                detail={"sample_count": len(values), "error": "insufficient_samples", **extraction_detail},
                evidence=f"【Matcher 求值 (delta)】样本不足: {len(values)}",
            )
        delta = values[-1] - values[0]
        target = matcher.get("value", 0)
        try:
            target = float(target)
        except (TypeError, ValueError):
            return MatcherResult(matched=None)
        op = str(matcher.get("operator") or ">")
        hit = _compare_threshold(delta, target, op)
        if hit is None:
            return MatcherResult(matched=None)
        matched = hit if expected else not hit
        return MatcherResult(
            matched=matched,
            detail={
                "hit": hit,
                "first": values[0],
                "last": values[-1],
                "delta": delta,
                "sample_count": len(values),
                "operator": op,
                "target": target,
                "metric": matcher.get("metric"),
                **extraction_detail,
            },
            evidence=(
                f"【Matcher 求值 (delta)】\nmetric: {matcher.get('metric') or '(all)'}\n"
                f"首值/末值/差值: {values[0]} / {values[-1]} / {delta}\n"
                f"比较: {delta} {op} {target} => {hit}\n最终判定: {matched}"
            ),
        )

    if mtype == "trend":
        values, extraction_detail, extraction_error = _resolve_numeric_values(
            text,
            matcher,
            precomputed_values=precomputed_values,
            precomputed_detail=precomputed_detail,
        )
        if extraction_error:
            return MatcherResult(
                matched=None,
                detail={"error": extraction_error, **extraction_detail},
                evidence=f"【Matcher 求值 (trend)】文本取值失败: {extraction_error}",
            )
        minimum_samples = int(matcher.get("minimum_samples") or 3)
        if len(values) < minimum_samples:
            return MatcherResult(
                matched=None,
                detail={"sample_count": len(values), "error": "insufficient_samples", **extraction_detail},
                evidence=f"【Matcher 求值 (trend)】样本不足: {len(values)} < {minimum_samples}",
            )
        direction = str(matcher.get("direction") or "increasing")
        min_step = float(matcher.get("value") or 0)
        deltas = [right - left for left, right in zip(values, values[1:], strict=False)]
        if direction == "decreasing":
            hit = all(delta <= -min_step for delta in deltas)
        elif direction == "stable":
            hit = all(abs(delta) <= min_step for delta in deltas)
        else:
            hit = all(delta >= min_step for delta in deltas)
        matched = hit if expected else not hit
        return MatcherResult(
            matched=matched,
            detail={
                "hit": hit,
                "values": values,
                "deltas": deltas,
                "direction": direction,
                "minimum_step": min_step,
                "sample_count": len(values),
                "metric": matcher.get("metric"),
                **extraction_detail,
            },
            evidence=(
                f"【Matcher 求值 (trend)】\nmetric: {matcher.get('metric') or '(all)'}\n"
                f"样本: {values}\n相邻差值: {deltas}\n"
                f"方向/最小步长: {direction}/{min_step}\n趋势命中: {hit}\n最终判定: {matched}"
            ),
        )

    if mtype == "exists":
        values, extraction_detail, extraction_error = _extract_predicate_values(text, matcher, "string")
        if extraction_error and not extraction_error.startswith("QFK_NO_MATCH:"):
            return MatcherResult(
                matched=None,
                detail={"error": extraction_error, **extraction_detail},
                evidence=f"【Matcher 求值 (exists)】取值失败: {extraction_error}",
            )
        present = bool(values)
        matched = present if expected else not present
        return MatcherResult(
            matched=matched,
            detail={"hit": present, **extraction_detail},
            evidence=(f"【Matcher 求值 (exists)】\n存在性: {present}\n期望 expected: {expected}\n最终判定: {matched}"),
        )

    # 未知类型 → 交 LLM
    return MatcherResult(matched=None)


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具函数
# ─────────────────────────────────────────────────────────────────────────────


def _extract_predicate_values(
    text: str,
    matcher: dict[str, Any],
    value_type: str,
) -> tuple[list[Any], dict[str, Any], str | None]:
    extract = matcher.get("extract")
    if not isinstance(extract, dict):
        return [], {}, "QFK_EXTRACT_INVALID_SPEC: Matcher 必须配置新版 extract"
    try:
        result = extract_output_values(text, extract, value_type)
    except QFKExtractionError as exc:
        detail = {"extract": {"status": "no_match" if exc.code == "QFK_NO_MATCH" else "error", "code": exc.code}}
        return [], detail, str(exc)
    return (
        result.values,
        {
            "extract": {
                "status": "ok",
                "matched_lines": result.matched_lines,
                "selected_lines": result.selected_lines,
                "matched_line_numbers": result.matched_line_numbers,
                "selected_line_numbers": result.selected_line_numbers,
                "raw_values": result.raw_values,
                "values": result.values,
                "raw_records": result.raw_records,
                "records": result.records,
                "value_key": result.value_key,
                "value_type": result.value_type,
            }
        },
        None,
    )


def _extract_predicate_text(
    text: str,
    matcher: dict[str, Any],
    value_type: str,
) -> tuple[str, dict[str, Any], str | None]:
    values, detail, error = _extract_predicate_values(text, matcher, value_type)
    return "\n".join(str(value) for value in values), detail, error


def _extract_match_values(
    text: str,
    matcher: dict[str, Any],
) -> tuple[list[float], dict[str, Any], str | None]:
    """用 Produces 与 Matcher 共用的提取器生成数值样本。"""

    extract = matcher.get("extract")
    if not isinstance(extract, dict):
        return [], {}, "QFK_EXTRACT_INVALID_SPEC: Matcher 必须配置新版 extract"
    extracted, detail, error = _extract_predicate_values(text, matcher, "number")
    if error:
        return [], detail, error
    try:
        values = [float(value) for value in extracted]
    except (TypeError, ValueError):
        return [], detail, "QFK_TYPE_CAST_FAILED: 文本取值结果不是数值"
    detail["extract"]["values"] = values
    return values, detail, None


def _resolve_numeric_values(
    text: str,
    matcher: dict[str, Any],
    *,
    precomputed_values: list[float] | None = None,
    precomputed_detail: dict[str, Any] | None = None,
) -> tuple[list[float], dict[str, Any], str | None]:
    """让数值 Matcher 统一消费确定性或已溯源的 AI 取值结果。"""

    if precomputed_values is not None:
        detail = dict(precomputed_detail or {})
        extract_detail = dict(detail.get("extract") or {})
        extract_detail.update(
            {
                "status": "ok",
                "values": list(precomputed_values),
                "value_source": "ai_grounded",
            }
        )
        detail["extract"] = extract_detail
        return list(precomputed_values), detail, None
    return _extract_match_values(text, matcher)


def _extract_duration_seconds(text: str) -> float | None:
    """解析 POSIX ``time`` 的 ``real 0m21.615s``，不受路径中数字干扰。"""

    match = re.search(r"(?m)^real\s+(?:(\d+)m)?(\d+(?:\.\d+)?)s\s*$", text or "")
    if not match:
        return None
    minutes = float(match.group(1) or 0)
    return minutes * 60 + float(match.group(2))


def _compare_threshold(val: float, target: float, op: str) -> bool | None:
    if op == ">":
        return val > target
    if op == ">=":
        return val >= target
    if op == "<":
        return val < target
    if op == "<=":
        return val <= target
    if op == "==" or op == "=":
        return val == target
    if op == "!=":
        return val != target
    return None
