"""Matcher 求值（兼容模块）。

所有 Matcher 逻辑已移至 ``shared.signals.matcher``。
本模块保留纯重新导出以保持向后兼容。
"""

from shared.signals.matcher import (  # noqa: F401
    MatcherResult,
    _compare_threshold,
    _eval_keyword,
    _extract_duration_seconds,
    _extract_match_values,
    _extract_predicate_text,
    _extract_predicate_values,
    _KeywordEval,
    _normalize_mode,
    _resolve_numeric_values,
    evaluate_matcher,
)

__all__ = [
    "MatcherResult",
    "evaluate_matcher",
]
