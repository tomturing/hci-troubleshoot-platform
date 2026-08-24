"""虚拟机控制台视觉证据的共享核心（近黑检测与结构化观察契约）。

在线（agent-service）与离线（diagnosis-service）两侧复用同一套常量与判定逻辑，
保证"同一图片 Fixture 在线/离线得到相同视觉状态与匹配语义"。
"""

from shared.vision.near_black import (
    NEAR_BLACK_ALGORITHM_REVISION,
    NEAR_BLACK_EDGE_DENSITY_MAX,
    NEAR_BLACK_MEAN_LUMA_MAX,
    NEAR_BLACK_NON_BLACK_RATIO_MAX,
    NON_BLACK_LUMA_THRESHOLD,
    analyze_ppm_near_black,
    compute_quality_metrics,
    is_near_black,
    parse_ppm,
)
from shared.vision.png_derive import derive_png_isolated
from shared.vision.vm_console_observation import (
    DISPLAY_STATE_VOCABULARY,
    VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION,
    VM_CONSOLE_MODEL_REVISION,
    VM_CONSOLE_PROMPT_REVISION,
    VmConsoleObservation,
    unavailable_observation,
)

__all__ = [
    "derive_png_isolated",
    "NEAR_BLACK_ALGORITHM_REVISION",
    "NEAR_BLACK_EDGE_DENSITY_MAX",
    "NEAR_BLACK_MEAN_LUMA_MAX",
    "NEAR_BLACK_NON_BLACK_RATIO_MAX",
    "NON_BLACK_LUMA_THRESHOLD",
    "analyze_ppm_near_black",
    "compute_quality_metrics",
    "is_near_black",
    "parse_ppm",
    "DISPLAY_STATE_VOCABULARY",
    "VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION",
    "VM_CONSOLE_MODEL_REVISION",
    "VM_CONSOLE_PROMPT_REVISION",
    "VmConsoleObservation",
    "unavailable_observation",
]
