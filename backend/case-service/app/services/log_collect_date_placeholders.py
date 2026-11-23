"""日志收集时间占位符：统一展开与校验。

第一性原理：`{date}` 不是"一个日期"，而是"路径期望的时间串形态"。同一个 HCI 现场，
`/sf/log/{date}.tar.gz` 按天序号（``%d``）命名，``/sf/log/blackbox/{date}.tar.gz``
按 ``%Y%m%d`` 命名——两条路径在同一份清单里用了两种格式。

旧实现只认一种：``expand_scenario_paths`` 一律展开成 ``YYYY-MM-DD``。于是
``format`` 字段成了摆设，猜错格式的路径会被 ``os.path.exists`` 判假后**静默跳过**，
用户看到的是"采完了但没收到这几类日志"，而不是一个错误。

本模块是唯一展开口径：平台展开、清单校验、管理端保存都调这里；
``format`` 只允许白名单内的 strftime 子集，避免 ``%`` 指令带来的本地化差异。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

# 占位符只支持 {date}：{start}/{end} 在日志收集链路里从未被实现，
# 放行它们等于让下发的路径带着花括号去 os.path.exists 判假。
SUPPORTED_PLACEHOLDER = "{date}"

DEFAULT_DATE_FORMAT = "%Y-%m-%d"

# 白名单而不是自由 strftime：不同 libc 的 %x/%c 输出不同，且现场只用到这四种。
_SUPPORTED_FORMATS: dict[str, str] = {
    "%Y-%m-%d": "ISO 日期",
    "%Y%m%d": "紧凑日期",
    "%Y%m": "年月",
    "%d": "两位日",
}


def supported_formats() -> dict[str, str]:
    """给管理端/错误提示用的可读清单。"""
    return dict(_SUPPORTED_FORMATS)


def format_day(day: date, fmt: str) -> str:
    """按白名单格式渲染单个日期；未支持的格式直接抛错，绝不静默回退。"""
    text = str(fmt or "").strip() or DEFAULT_DATE_FORMAT
    if text == "%Y-%m-%d":
        return f"{day.year:04d}-{day.month:02d}-{day.day:02d}"
    if text == "%Y%m%d":
        return f"{day.year:04d}{day.month:02d}{day.day:02d}"
    if text == "%Y%m":
        return f"{day.year:04d}{day.month:02d}"
    if text == "%d":
        return f"{day.day:02d}"
    raise ValueError(
        f"不支持的时间占位符格式：{text}（仅支持 {' / '.join(_SUPPORTED_FORMATS)}）"
    )


def item_date_format(item: dict[str, Any]) -> str:
    """取某条清单项声明的 ``{date}`` 格式；没声明时用默认 ISO 日期。

    兼容历史数据：早期自定义场景没有 ``date_placeholders``，当时平台按
    ``YYYY-MM-DD`` 展开，保持同一行为，不引入行为回退。
    """
    for placeholder in item.get("date_placeholders") or []:
        if not isinstance(placeholder, dict):
            continue
        if str(placeholder.get("placeholder") or "").strip() != SUPPORTED_PLACEHOLDER:
            continue
        raw = str(placeholder.get("format") or "").strip()
        return raw or DEFAULT_DATE_FORMAT
    return DEFAULT_DATE_FORMAT


def validate_item_placeholders(item: dict[str, Any]) -> list[str]:
    """校验单条清单项的占位符声明，返回问题清单（空列表 = 通过）。

    校验点只有三个，但每个都对应一次真实误采：
    1. 路径里出现了不支持的占位符 —— 下发后路径必然判假；
    2. 声明了 ``{date}`` 但格式不在白名单 —— 插件与平台对同一路径渲染不同；
    3. 声明了 ``{date}`` 但路径里没有它，或反之 —— 声明与实际不一致。
    """
    # 过滤规则用 pattern 而不是 path：两者都可能带 {date}，同一套校验。
    path = str(item.get("path") or item.get("pattern") or "")
    problems: list[str] = []

    declared = [
        placeholder
        for placeholder in (item.get("date_placeholders") or [])
        if isinstance(placeholder, dict)
    ]
    path_has_date = SUPPORTED_PLACEHOLDER in path

    for placeholder in declared:
        token = str(placeholder.get("placeholder") or "").strip()
        if token != SUPPORTED_PLACEHOLDER:
            problems.append(
                f"路径 {path or '(空)'} 使用了不支持的占位符 {token or '(空)'}；"
                f"仅支持 {SUPPORTED_PLACEHOLDER}"
            )
            continue
        raw = str(placeholder.get("format") or "").strip()
        if raw and raw not in _SUPPORTED_FORMATS:
            problems.append(
                f"路径 {path or '(空)'} 的时间格式 {raw} 不受支持；"
                f"仅支持 {' / '.join(_SUPPORTED_FORMATS)}"
            )
        if not path_has_date:
            problems.append(f"路径 {path or '(空)'} 声明了 {SUPPORTED_PLACEHOLDER} 但路径里没有它")

    if path_has_date and not declared:
        # 不报错：历史数据靠默认格式展开。仅当路径里出现其它花括号时才拒绝。
        pass

    for token in _other_tokens(path):
        problems.append(
            f"路径 {path} 使用了不支持的占位符 {token}；仅支持 {SUPPORTED_PLACEHOLDER}"
        )
    return problems


def _other_tokens(path: str) -> list[str]:
    """找出 ``{date}`` 之外的花括号占位符（含 ``{start}``/``{end}``）。"""
    tokens: list[str] = []
    index = 0
    while True:
        start = path.find("{", index)
        if start < 0:
            break
        end = path.find("}", start)
        if end < 0:
            break
        token = path[start : end + 1]
        if token != SUPPORTED_PLACEHOLDER:
            tokens.append(token)
        index = end + 1
    return tokens


def expand_path_with_format(path: str, fmt: str, date_from: date, date_to: date) -> list[str]:
    """按声明格式逐天展开；无占位符时原样返回。"""
    if SUPPORTED_PLACEHOLDER not in path:
        return [path]
    days = (date_to - date_from).days + 1
    return [
        path.replace(SUPPORTED_PLACEHOLDER, format_day(date_from + timedelta(days=offset), fmt))
        for offset in range(days)
    ]
