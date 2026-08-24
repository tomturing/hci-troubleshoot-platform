"""控制台截图回放 Fixture（设计文档 §7.2）。

为 Admin 审核页提供五种确定性回放样本：正常画面、黑屏、Kernel Panic、蓝屏、
不可用（畸形输入）。回放只执行**确定性阶段**（PPM 解析、近黑质量检查、
受控 PNG 派生、观察 Schema 契约校验）；视觉模型分类属于策略门禁后的
非确定性阶段，不在回放范围内（词表与降级规则由契约校验覆盖）。
"""

from __future__ import annotations

from typing import Any

from shared.vision.near_black import analyze_ppm_near_black
from shared.vision.png_derive import derive_png_isolated
from shared.vision.vm_console_observation import (
    DISPLAY_STATE_VOCABULARY,
    VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION,
    VmConsoleObservation,
)

_FIXTURE_SIZE = (64, 48)


def _ppm(width: int, height: int, pixel_fn) -> bytes:
    header = f"P6\n{width} {height}\n255\n".encode()
    body = bytearray()
    for y in range(height):
        for x in range(width):
            body += bytes(pixel_fn(x, y))
    return header + bytes(body)


def build_fixture_scenarios() -> list[dict[str, Any]]:
    """构造五种回放场景（合成 PPM + 期望语义标注）。"""

    width, height = _FIXTURE_SIZE

    def normal_pixel(x: int, y: int) -> tuple[int, int, int]:
        # 白底 + 深色文本条带，模拟正常控制台。
        if (y // 4) % 2 == 0 and 8 <= x < 56:
            return (240, 240, 240)
        return (25, 25, 25)

    def panic_pixel(x: int, y: int) -> tuple[int, int, int]:
        # 黑底白字的高对比画面（Kernel Panic 文本区）。
        if 12 <= y < 36 and 4 <= x < 60 and (x + y) % 7 < 3:
            return (235, 235, 235)
        return (5, 5, 5)

    def bsod_pixel(x: int, y: int) -> tuple[int, int, int]:
        # 蓝底白字，模拟 BSOD。
        if 16 <= y < 32 and 8 <= x < 56 and (x * y) % 11 < 4:
            return (255, 255, 255)
        return (0, 80, 180)

    return [
        {
            "name": "normal",
            "label": "正常画面",
            "expected_display_state": "desktop",
            "expected_near_black": False,
            "ppm": _ppm(width, height, normal_pixel),
        },
        {
            "name": "black_screen",
            "label": "黑屏",
            "expected_display_state": "black_screen",
            "expected_near_black": True,
            "ppm": _ppm(width, height, lambda x, y: (0, 0, 0)),
        },
        {
            "name": "kernel_panic",
            "label": "Kernel Panic",
            "expected_display_state": "kernel_panic",
            "expected_near_black": False,
            "ppm": _ppm(width, height, panic_pixel),
        },
        {
            "name": "bsod",
            "label": "Windows 蓝屏",
            "expected_display_state": "bsod",
            "expected_near_black": False,
            "ppm": _ppm(width, height, bsod_pixel),
        },
        {
            "name": "unavailable",
            "label": "不可用（畸形输入）",
            "expected_display_state": "unknown",
            "expected_near_black": False,
            "ppm": b"P5\n64 48\n255\n" + b"\x00" * 16,
        },
    ]


def run_replay() -> list[dict[str, Any]]:
    """对五种 Fixture 执行确定性回放，返回结构化结果。"""

    results: list[dict[str, Any]] = []
    for scenario in build_fixture_scenarios():
        quality = analyze_ppm_near_black(scenario["ppm"])
        entry: dict[str, Any] = {
            "name": scenario["name"],
            "label": scenario["label"],
            "expected_display_state": scenario["expected_display_state"],
            "expected_near_black": scenario["expected_near_black"],
            "parse_ok": bool(quality.get("parse_ok")),
            "near_black": bool(quality.get("near_black")),
            "metrics": quality.get("metrics") or {},
            "png_derived": False,
            "observation_contract_ok": False,
        }
        if entry["parse_ok"]:
            try:
                png, png_width, png_height = derive_png_isolated(scenario["ppm"])
                entry["png_derived"] = png.startswith(b"\x89PNG")
                entry["png_size"] = {"width": png_width, "height": png_height}
            except (ValueError, TimeoutError) as exc:
                entry["png_error"] = str(exc)
        # 观察 Schema 契约回放：词表外值降级 unknown、artifact_id 必填。
        try:
            observation = VmConsoleObservation(
                display_state=scenario["expected_display_state"],
                summary=f"回放 Fixture：{scenario['label']}",
                confidence=0.9 if entry["parse_ok"] else 0.0,
                artifact_id=f"fixture-{scenario['name']}",
            )
            entry["observation_contract_ok"] = (
                observation.display_state in DISPLAY_STATE_VOCABULARY
                and observation.display_state_vocabulary_revision
                == VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION
            )
        except Exception:
            entry["observation_contract_ok"] = False
        # 近黑判定与期望一致性校验（确定性回放的核心断言）。
        entry["near_black_matches_expectation"] = entry["near_black"] == scenario["expected_near_black"]
        results.append(entry)
    return results
