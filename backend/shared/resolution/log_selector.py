"""QFK 日志粗筛选择器的共享编译规则。"""

from __future__ import annotations

import re
from typing import Any

from shared.schemas.log_source_catalog import LOG_MATCHER_TYPES


def build_log_selector(
    *,
    matcher: dict[str, Any] | None,
    keywords: list[str] | None = None,
    filter_keywords: list[str] | None = None,
    resource_keyword: str | None = None,
    request_id: str | None = None,
) -> tuple[str | None, bool, str]:
    """把结构化 Matcher 编译为 ``acli log get`` 的只读粗筛条件。

    返回值依次为选择器、是否使用扩展正则和 Matcher 类型。AND、排除、阈值等
    最终判定仍由 Matcher 在完整记录上执行；这里仅缩小离线与在线的采集范围。
    """

    matcher = matcher or {}
    keywords = keywords or []
    filter_keywords = filter_keywords or []
    matcher_type = str(matcher.get("type") or ("keyword" if keywords else ""))
    if matcher_type and matcher_type not in LOG_MATCHER_TYPES:
        raise ValueError(f"qfk_log 不支持 matcher.type={matcher_type}")

    if filter_keywords:
        unique = sorted({str(item) for item in filter_keywords if str(item)})
        if unique:
            return "|".join(re.escape(item) for item in unique), True, matcher_type or "producer"

    pattern = matcher.get("pattern")
    if matcher_type == "keyword":
        raw_items = pattern if isinstance(pattern, list) else [pattern] if pattern else keywords
        unique = sorted({str(item) for item in raw_items if str(item)})
        if unique:
            return "|".join(re.escape(item) for item in unique), True, matcher_type
    elif matcher_type == "regex":
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("qfk_log regex matcher 必须提供非空 pattern")
        if len(pattern) > 2048 or "\n" in pattern or "\r" in pattern:
            raise ValueError("qfk_log regex pattern 过长或包含换行")
        return pattern, True, matcher_type
    elif matcher_type == "state":
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("qfk_log state matcher 必须提供非空 pattern")
        return re.escape(pattern), True, matcher_type
    elif matcher_type in {"threshold", "delta", "trend"}:
        metric = matcher.get("metric") or resource_keyword
        if not isinstance(metric, str) or not metric:
            # 数值 Matcher 可以由 AI 从有界日志行提取类型化数值；此时 metric
            # 不是必填项，但仍必须从持久化的 rows.include（权威来源）或调用方
            # 派生的 filter/keyword 得到粗筛条件，禁止退化为整文件回传。
            extract = matcher.get("extract") if isinstance(matcher, dict) else None
            ai_instruction = ""
            rows_include: list[Any] = []
            if isinstance(extract, dict):
                ai_extract = extract.get("ai_extract")
                if isinstance(ai_extract, dict):
                    ai_instruction = str(ai_extract.get("instruction") or "").strip()
                rows = extract.get("rows")
                include = rows.get("include") if isinstance(rows, dict) else None
                if isinstance(include, (list, tuple)):
                    rows_include = list(include)
            if ai_instruction:
                ai_filter = rows_include or filter_keywords or keywords
                unique = sorted({str(item) for item in ai_filter if str(item)})
                if unique:
                    return "|".join(re.escape(item) for item in unique), True, matcher_type
            raise ValueError(f"qfk_log {matcher_type} matcher 必须提供 metric")
        return re.escape(metric), True, matcher_type
    elif matcher_type == "exists":
        return ".", True, matcher_type

    if resource_keyword:
        return re.escape(resource_keyword), True, matcher_type or "producer"
    if request_id:
        return None, False, matcher_type or "producer"
    if keywords:
        raise ValueError("关键字全部为空：至少需要一个非空关键字")
    raise ValueError("qfk_log 必须提供关键字 matcher、resource_keyword 或 request_id 以限制日志输出")
