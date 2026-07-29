"""
Matcher 求值单一真相源（Single Source of Truth）

统一处理 KBD 差异诊断（全部 6 类）与 QFK 引擎（keyword 类）的 Matcher 求值逻辑，
消除 `handlers.FunctionHandler.evaluate` 与 `kbd_differential._evaluate_matcher`
中重复且可能漂移的 keyword 实现，并统一证据链结构。

支持类型：keyword / regex / state / threshold / json_path / exists

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
        if aggregation == "line_count":
            val = float(sum(1 for line in (text or "").splitlines() if line.strip()))
        elif aggregation == "duration_seconds":
            val = _extract_duration_seconds(text)
        else:
            val = _extract_number(text)
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
            },
            evidence=(
                f"【Matcher 求值 (threshold/{aggregation})】\n提取数值: {val} {op} {target}\n"
                f"比较结果: {cmp}\n期望 expected: {expected}\n最终判定: {matched}"
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
