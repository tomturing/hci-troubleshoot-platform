"""
关键信号抽取工具包（signal）

还原 PR498 的 SignalExtractor 设计，并将 Prompt 统一收敛到 system_prompt 表，
由 admin-ui 管理、StrictPromptLoader 热加载。
"""

from app.tools.signal.base import KeySignal, SignalCategory
from app.tools.signal.backend import (
    BackendSignal,
    BackendSignalTarget,
    BackendSignalType,
)
from app.tools.signal.frontend import FrontendQueryType, FrontendSignal
from app.tools.signal.extractor import (
    SignalExtractor,
    SignalExtractionError,
)

__all__ = [
    "KeySignal",
    "SignalCategory",
    "FrontendSignal",
    "FrontendQueryType",
    "BackendSignal",
    "BackendSignalType",
    "BackendSignalTarget",
    "SignalExtractor",
    "SignalExtractionError",
]
