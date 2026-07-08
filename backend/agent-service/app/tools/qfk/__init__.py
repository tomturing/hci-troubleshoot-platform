"""
QFK 关键信号谓词引擎入口
"""

from app.tools.qfk.engine import QFKResult, qfk_exec, qfk_load
from app.tools.qfk.signal import KeySignal, KeySignalTarget, SignalType
from app.tools.qfk.template import KEY_SIGNAL_JSON_SCHEMA, KEY_SIGNAL_PROMPT_TEMPLATE

__all__ = [
    "SignalType",
    "KeySignalTarget",
    "KeySignal",
    "QFKResult",
    "qfk_load",
    "qfk_exec",
    "KEY_SIGNAL_PROMPT_TEMPLATE",
    "KEY_SIGNAL_JSON_SCHEMA",
]
