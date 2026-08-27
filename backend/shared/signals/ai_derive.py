"""AI 智能推导的共享配置与确定性归一化。

AI 只负责从已筛选的原文行中标注可回查的源值。任何时间解析、时区绑定和数值
归一化都必须在此模块完成，避免模型直接产出业务计算结论。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AI_EXTRACT_MODES = {"extract", "derive"}
DERIVE_NORMALIZERS = {"datetime_epoch"}


def ai_extract_mode(config: Any) -> str:
    """返回 AI 处理模式；历史配置缺省时保持原文取值语义。"""

    if not isinstance(config, dict):
        return "extract"
    return str(config.get("mode") or "extract")


def validate_ai_extract_config(config: Any) -> str:
    """验证 AI 配置并返回模式，供 Schema 和运行时共用。"""

    if not isinstance(config, dict):
        raise ValueError("AI 处理必须配置 ai_extract 对象")
    instruction = config.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("AI 处理必须填写非空处理说明")
    if len(instruction) > 1000:
        raise ValueError("AI 处理说明不能超过 1000 个字符")

    mode = ai_extract_mode(config)
    if mode not in AI_EXTRACT_MODES:
        raise ValueError(f"AI 处理模式不受支持: {mode}")
    derive = config.get("derive")
    if mode == "extract":
        if derive is not None:
            raise ValueError("原文取值模式不能配置 derive")
        return mode
    if not isinstance(derive, dict):
        raise ValueError("智能推导模式必须配置 derive")
    unknown = set(derive) - {"normalizer", "formats", "timezone"}
    if unknown:
        raise ValueError(f"智能推导包含未注册字段: {sorted(unknown)}")
    if derive.get("normalizer") not in DERIVE_NORMALIZERS:
        raise ValueError("智能推导 normalizer 仅支持 datetime_epoch")
    formats = derive.get("formats")
    if (
        not isinstance(formats, list)
        or not formats
        or len(formats) > 8
        or any(not isinstance(item, str) or not item or len(item) > 128 for item in formats)
    ):
        raise ValueError("智能推导 formats 必须是 1 到 8 个非空时间格式")
    timezone = derive.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("智能推导必须配置 IANA timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"智能推导 timezone 不存在: {timezone}") from exc
    return mode


def normalize_derived_values(source_values: list[str], config: dict[str, Any]) -> list[float]:
    """以配置的确定性规则把已溯源的源文本归一为数值。"""

    validate_ai_extract_config(config)
    derive = config["derive"]
    if derive["normalizer"] != "datetime_epoch":
        raise ValueError(f"不支持的智能推导 normalizer: {derive['normalizer']}")
    timezone = ZoneInfo(str(derive["timezone"]))
    formats = list(derive["formats"])
    normalized: list[float] = []
    for source_value in source_values:
        parsed: datetime | None = None
        for value_format in formats:
            parsed = _parse_datetime(source_value, value_format, timezone)
            if parsed is not None:
                break
        if parsed is None:
            raise ValueError(f"源值 {source_value!r} 不符合配置的时间格式")
        normalized.append(parsed.timestamp())
    return normalized


def _parse_datetime(source_value: str, value_format: str, timezone: ZoneInfo) -> datetime | None:
    """解析源时间，并在格式含时区缩写时校验其与配置时区一致。"""

    if "%Z" not in value_format:
        try:
            return datetime.strptime(source_value, value_format).replace(tzinfo=timezone)
        except ValueError:
            return None

    # Python 对 %Z 可识别的缩写随运行环境变化。移除一个候选缩写后仍用同一
    # 格式解析，并要求该缩写等于配置 IANA 时区在对应时间的名称，避免宽松接受。
    if value_format.count("%Z") != 1:
        return None
    format_without_timezone = value_format.replace("%Z", "")
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9_+/-]*", source_value):
        timezone_name = match.group(0)
        source_without_timezone = source_value[: match.start()] + source_value[match.end() :]
        try:
            parsed = datetime.strptime(source_without_timezone, format_without_timezone)
        except ValueError:
            continue
        localized = parsed.replace(tzinfo=timezone)
        if localized.tzname() == timezone_name:
            return localized
    return None
