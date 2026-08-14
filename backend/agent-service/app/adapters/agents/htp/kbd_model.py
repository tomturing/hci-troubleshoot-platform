"""KBD 模型旧导入路径兼容层；实现统一由 ``shared.cdd.kbd_model`` 提供。"""

from shared.cdd.kbd_model import KBD, KBDStep, _acquire_tool, _signal_category, _signal_to_step, kbd_from_dict

__all__ = [
    "KBD",
    "KBDStep",
    "_acquire_tool",
    "_signal_category",
    "_signal_to_step",
    "kbd_from_dict",
]
