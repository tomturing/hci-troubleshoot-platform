"""在线与离线诊断共用的确定性信号评估组件（纯 Python，零服务依赖）。

提取自 agent-service 的 QFK 引擎，供 agent-service 与 diagnosis-service
通过同一代码路径执行证据取值和 matcher 判定。

禁止 import ``app.*`` 或 ``shared.observability.*``，确保所有消费者
（在线、离线、回放、测试）得到完全相同的求值结果。
"""

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

__all__ = [
    "DEFAULT_OUTPUT_MAX_BYTES",
    "HARD_OUTPUT_MAX_BYTES",
    "ExtractionResult",
    "MatcherResult",
    "QFKExtractionError",
    "evaluate_matcher",
    "extract_output_values",
    "extract_value",
]
