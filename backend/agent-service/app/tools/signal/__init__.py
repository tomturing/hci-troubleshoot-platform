"""
关键信号模块（Signal Module）

提供关键信号的完整架构体系：
- KeySignal：抽象基类
- FrontendSignal：前端信号（故障现场元数据提取）
- BackendSignal：后端信号（运行时健康度判定）
- SignalExtractor：从 KBD/SOP 文本提取信号
- VariablePool：变量池管理（生产者-消费者模式）
"""

from app.tools.signal.backend import (
    BackendSignal,
    BackendSignalTarget,
    BackendSignalType,
)
from app.tools.signal.base import KeySignal, SignalCategory
from app.tools.signal.extractor import SignalExtractionError, SignalExtractor
from app.tools.signal.frontend import FrontendQueryType, FrontendSignal
from app.tools.signal.variable_pool import VariablePool

__all__ = [
    # 基类与枚举
    "KeySignal",
    "SignalCategory",
    # 前端信号
    "FrontendSignal",
    "FrontendQueryType",
    # 后端信号
    "BackendSignal",
    "BackendSignalType",
    "BackendSignalTarget",
    # 提取器
    "SignalExtractor",
    "SignalExtractionError",
    # 变量池
    "VariablePool",
]


