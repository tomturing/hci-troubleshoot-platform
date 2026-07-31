"""
Matcher 求值单一真相源（Single Source of Truth）

统一处理 KBD 差异诊断（全部 6 类）与 QFK 引擎（keyword 类）的 Matcher 求值逻辑，
消除 `handlers.FunctionHandler.evaluate` 与 `kbd_differential._evaluate_matcher`
中重复且可能漂移的 keyword 实现，并统一证据链结构。

支持类型：keyword / regex / state / threshold / delta / trend / json_path / exists

求值契约
--------
`evaluate_matcher(matcher, text) -> MatcherResult`
  - matched: 确定性布尔；None 表示无法定值（交由 LLM 兜底）
  - detail : 结构化信息（命中关键字、原始 hit、模式等），供调用方构造证据链
  - evidence: 人类可读的求值证据片段

关键字组合语义（match_mode）
--------------------------
  - or  ：任一关键字命中即判定为真（向后兼容旧 any）
  - and ：全部关键字命中才判定为真（向后兼容旧 all）
  - not ：所有关键字均不出现才判定为真（取代旧 expected=False 的取反语义，
          因此 not 模式忽略 expected，直接以"均不出现"为命中）
其余类型（regex/state/threshold/json_path/exists）按 expected 翻转。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.tools.qfk.extractor import QFKExtractionError, extract_output_values

# mode 归一化：向后兼容旧 any/all 词表
_MODE_ALIASES = {"any": "or", "all": "and"}


def _normalize_mode(mode: str | None) -> str:
    mode = (mode or "or").lower()
    return _MODE_ALIASES.get(mode, mode)


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


def _eval_keyword(kws: list[str], out_l: str, mode: str) -> _KeywordEval:
    """纯客户端关键字求值（不涉服务端过滤）。返回原始 hit 与命中关键字列表。"""
    matched_kws = [k for k in kws if k.lower() in out_l]
    if mode == "and":
        hit = bool(kws) and len(matched_kws) == len(kws)
    elif mode == "not":
        # not 模式本身编码"取反"语义：均不出现才是命中
        hit = len(matched_kws) == 0
    else:  # or（默认）
        hit = bool(matched_kws)
    return _KeywordEval(hit=hit, matched_kws=matched_kws)


def evaluate_matcher(
    matcher: dict[str, Any] | None,
    text: str,
    *,
    server_pre_filtered: bool = False,
) -> MatcherResult:
    """对单条 Matcher 契约做确定性（非 LLM）布尔求值。

    Args:
        matcher: Matcher dict（来自信号 schema 或 KBD expected_pattern 序列化）。
        text: 实际工具输出文本。
        server_pre_filtered: 仅 keyword/or 模式有意义。为 True 时表示 text 已
            经服务端 `grep -E` 过滤（QFK 引擎 or 模式走 -E -k "kw1|kw2"），
            此时输出非空即代表至少一个关键字命中，作为权威结果。

    Returns:
        MatcherResult：matched 为 None 时调用方应交给 LLM 兜底。
    """
    if not isinstance(matcher, dict):
        return MatcherResult(matched=None)
    mtype = matcher.get("type", "")
    expected = bool(matcher.get("expected", True))
    out_l = (text or "").lower()

    if mtype == "keyword":
        p = matcher.get("pattern", "")
        kws = [p] if isinstance(p, str) else list(p or [])
        kws = [k for k in kws if k]
        if not kws:
            return MatcherResult(matched=None)
        mode = _normalize_mode(matcher.get("mode"))

        if mode == "or" and server_pre_filtered:
            # text 已由服务端 grep -E 过滤，非空即代表命中（权威结果）；
            # 同时尽力回填命中关键字用于证据链。
            hit = bool((text or "").strip())
            matched_kws = [k for k in kws if k.lower() in out_l]
        else:
            ev = _eval_keyword(kws, out_l, mode)
            hit, matched_kws = ev.hit, ev.matched_kws

        # not 模式已在 hit 内表达取反；其余模式按 expected 翻转
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
            detail={"matched_keywords": matched_kws, "hit": hit, "mode": mode},
            evidence=evidence,
        )

    if mtype == "regex":
        p = matcher.get("pattern", "")
        if not isinstance(p, str) or not p:
            return MatcherResult(matched=None)
        try:
            hit = bool(re.search(p, text or "", re.IGNORECASE | re.DOTALL))
        except re.error:
            # 非法正则属配置错误，不可能命中，确定性判为不匹配（不降级 LLM）
            return MatcherResult(
                matched=False,
                detail={"hit": False, "error": "invalid_regex"},
                evidence=f"【Matcher 求值 (regex)】非法正则 pattern: {p}\n最终判定: False",
            )
        matched = hit if expected else not hit
        return MatcherResult(
            matched=matched,
            detail={"hit": hit},
            evidence=(
                f"【Matcher 求值 (regex)】\npattern: {p}\n"
                f"原始命中(hit): {hit}\n期望 expected: {expected}\n最终判定: {matched}"
            ),
        )

    if mtype == "state":
        # 期望状态值（如 running/active/healthy）出现在输出即视为命中
        p = matcher.get("pattern", "")
        if not isinstance(p, str) or not p:
            return MatcherResult(matched=None)
        hit = p.lower() in out_l
        matched = hit if expected else not hit
        return MatcherResult(
            matched=matched,
            detail={"hit": hit},
            evidence=(
                f"【Matcher 求值 (state)】\n期望状态: {p}\n"
                f"原始命中(hit): {hit}\n期望 expected: {expected}\n最终判定: {matched}"
            ),
        )

    if mtype == "threshold":
        aggregation = str(matcher.get("aggregation") or "first_number")
        values, extraction_detail, extraction_error = _extract_match_values(text, matcher)
        if extraction_error:
            return MatcherResult(
                matched=None,
                detail={"error": extraction_error, **extraction_detail},
                evidence=f"【Matcher 求值 (threshold)】文本取值失败: {extraction_error}",
            )
        if aggregation == "line_count":
            val = float(sum(1 for line in (text or "").splitlines() if line.strip()))
        elif aggregation == "duration_seconds":
            val = _extract_duration_seconds(text)
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
        values, extraction_detail, extraction_error = _extract_match_values(text, matcher)
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
        values, extraction_detail, extraction_error = _extract_match_values(text, matcher)
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

    if mtype == "json_path":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return MatcherResult(matched=None)
        node = _read_json_path(data, matcher.get("path", ""))
        hit = (
            node == matcher.get("expected_value")
            if "expected_value" in matcher
            else node is not None
        )
        matched = hit if expected else not hit
        return MatcherResult(
            matched=matched,
            detail={"hit": hit, "node": node},
            evidence=(
                f"【Matcher 求值 (json_path)】\npath: {matcher.get('path', '')}\n"
                f"取值: {node}\n原始命中(hit): {hit}\n"
                f"期望 expected: {expected}\n最终判定: {matched}"
            ),
        )

    if mtype == "exists":
        # 存在性：输出非空且不含"不存在/not found"等否定标记
        present = bool(
            text
            and text.strip()
            and "不存在" not in out_l
            and "not found" not in out_l
        )
        matched = present if expected else not present
        return MatcherResult(
            matched=matched,
            detail={"hit": present},
            evidence=(
                f"【Matcher 求值 (exists)】\n存在性: {present}\n"
                f"期望 expected: {expected}\n最终判定: {matched}"
            ),
        )

    # 未知类型 → 交 LLM
    return MatcherResult(matched=None)


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具函数（原 kbd_differential 的静态方法，现为单一真相源一部分）
# ─────────────────────────────────────────────────────────────────────────────


def _extract_match_values(
    text: str,
    matcher: dict[str, Any],
) -> tuple[list[float], dict[str, Any], str | None]:
    """用 Produces 与 Matcher 共用的提取器生成数值样本。

    未配置 ``match.extract`` 的历史 Matcher 保持既有 metric/全局数字逻辑；新
    Matcher 一旦配置 extract，绝不再猜测文本中的第一个数字。错误返回给上层作为
    UNKNOWN，避免把“无法精确取值”误判为阈值未命中。
    """

    extract = matcher.get("extract")
    if not isinstance(extract, dict):
        return _extract_metric_numbers(text, matcher.get("metric")), {}, None
    try:
        result = extract_output_values(text, extract, "number")
    except QFKExtractionError as exc:
        return [], {}, str(exc)
    try:
        values = [float(value) for value in result.values]
    except (TypeError, ValueError):
        return [], {}, "QFK_TYPE_CAST_FAILED: 文本取值结果不是数值"
    return values, {
        "extract": {
            "matched_lines": result.matched_lines,
            "selected_lines": result.selected_lines,
            "raw_values": result.raw_values,
            "values": values,
        }
    }, None


def _extract_number(text: str) -> float | None:
    """从文本中提取首个数值（支持整数/小数/负数/百分号）。"""
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _extract_metric_numbers(text: str, metric: Any = None) -> list[float]:
    """提取数值样本；指定 metric 时只解析包含该字段的行并取该行末值。

    blackbox 行通常以时间戳开头，使用“首个数字”会把日期误当成计数器。因此日志阈值、
    差值和趋势统一按 metric 过滤后取行末数值；未指定 metric 时保留全局数字序列以兼容
    既有非日志 matcher。
    """

    source = text or ""
    if isinstance(metric, str) and metric:
        values: list[float] = []
        for line in source.splitlines():
            if metric.lower() not in line.lower():
                continue
            numbers = re.findall(r"-?\d+(?:\.\d+)?", line)
            if numbers:
                values.append(float(numbers[-1]))
        return values
    return [float(item) for item in re.findall(r"-?\d+(?:\.\d+)?", source)]


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


def _read_json_path(data: Any, path: str) -> Any:
    """按点分路径读取嵌套 JSON 节点（如 "a.b.0.c"）。"""
    if not path:
        return data
    node: Any = data
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node
