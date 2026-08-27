"""QFK 命令输出的 AI 提取器（兼容模块）。

所有 AI 提取与后处理核心逻辑已移至 ``shared.signals.ai_extractor``。
本模块重新导出公共符号以保持向后兼容。
"""

from __future__ import annotations

from shared.signals.ai_extractor import (  # noqa: F401 — 重新导出以保持向后兼容
    AI_PROCESSING_PROMPT_NAME,
    AI_PROCESSING_PROMPT_PLACEHOLDERS,
    MAX_AI_EXTRACT_INPUT_BYTES,
    MAX_AI_EXTRACT_LINES,
    AIExtractionResult,
    _load_ai_processing_system_prompt,
    ai_value_type_for_matcher,
    extract_ai_value,
    has_ai_extract,
)

__all__ = [
    "AI_PROCESSING_PROMPT_NAME",
    "AI_PROCESSING_PROMPT_PLACEHOLDERS",
    "MAX_AI_EXTRACT_INPUT_BYTES",
    "MAX_AI_EXTRACT_LINES",
    "AIExtractionResult",
    "_load_ai_processing_system_prompt",
    "ai_value_type_for_matcher",
    "extract_ai_value",
    "has_ai_extract",
]
