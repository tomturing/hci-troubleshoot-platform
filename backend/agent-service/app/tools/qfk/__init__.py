"""
QFK 后端信号谓词引擎入口
"""

from app.tools.qfk.engine import QFKResult, qfk_exec, qfk_load
from app.tools.qfk.handlers import HandlerRegistry, LogKeywordHandler
from app.tools.qfk.matcher import evaluate_matcher
from app.tools.qfk.signal import BackendSignal, BackendSignalTarget
from app.tools.qfk.template import BACKEND_SIGNAL_JSON_SCHEMA, BACKEND_SIGNAL_PROMPT_TEMPLATE

__all__ = [
    "BackendSignalTarget",
    "BackendSignal",
    "HandlerRegistry",
    "LogKeywordHandler",
    "evaluate_matcher",
    "QFKResult",
    "qfk_load",
    "qfk_exec",
    "BACKEND_SIGNAL_PROMPT_TEMPLATE",
    "BACKEND_SIGNAL_JSON_SCHEMA",
]
