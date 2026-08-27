"""在线与离线诊断共用的确定性与 AI 信号评估组件。

供 agent-service、diagnosis-service 与各微服务通过同一代码路径执行证据取值、
matcher 判定以及统一 AI 信号再加工。
"""

from shared.signals.ai_extractor import (
    AIExtractionResult,
    extract_ai_value,
    has_ai_extract,
)
from shared.signals.ai_processing import (
    AIEvidence,
    AIProcessingResult,
    ai_item_type,
    ai_output_type,
    ai_processing_config,
    ai_processing_mode,
    validate_ai_output,
    validate_ai_processing_config,
    validate_ai_response,
)
from shared.signals.extractor import (
    DEFAULT_OUTPUT_MAX_BYTES,
    HARD_OUTPUT_MAX_BYTES,
    ExtractionResult,
    QFKExtractionError,
    extract_output_values,
    extract_value,
)
from shared.signals.matcher import (
    MatcherResult,
    evaluate_matcher,
)
from shared.signals.qkv_output_processing import (
    QKVAssertion,
    QKVProcessingError,
    QKVProcessingResult,
    apply_output_processing_async,
)

__all__ = [
    "AIEvidence",
    "AIExtractionResult",
    "AIProcessingResult",
    "DEFAULT_OUTPUT_MAX_BYTES",
    "HARD_OUTPUT_MAX_BYTES",
    "ExtractionResult",
    "MatcherResult",
    "QFKExtractionError",
    "QKVAssertion",
    "QKVProcessingError",
    "QKVProcessingResult",
    "ai_item_type",
    "ai_output_type",
    "ai_processing_config",
    "ai_processing_mode",
    "apply_output_processing_async",
    "evaluate_matcher",
    "extract_ai_value",
    "extract_output_values",
    "extract_value",
    "has_ai_extract",
    "validate_ai_output",
    "validate_ai_processing_config",
    "validate_ai_response",
]
