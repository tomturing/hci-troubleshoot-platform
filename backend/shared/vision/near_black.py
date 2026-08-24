"""确定性近黑检测（纯 Python P6 PPM 解析，零新依赖）。

设计来源：《虚拟机控制台视觉生产者信号设计与需求》§3.3。不能把"黑屏"完全交给
Vision 判断：在调用视觉模型前先运行确定性图像质量检查，只有命中"近黑/无有效画面"
阈值才可申请唤醒重截。

离线 Go 实现（offline-collector/nearblack.go）必须与本模块保持**同一算法修订与
阈值**；常量漂移由测试断言守护。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# 算法修订号：阈值或判定逻辑任何变更都必须提升该值，并同步 Go 侧常量。
NEAR_BLACK_ALGORITHM_REVISION = "near-black-v1"

# 判定阈值（与 Go 侧 nearblack.go 同值）：
# 平均亮度低于 8（0-255 标度）、非近黑像素比例低于 2%、边缘密度低于 0.5%。
NEAR_BLACK_MEAN_LUMA_MAX = 8.0
NEAR_BLACK_NON_BLACK_RATIO_MAX = 0.02
NEAR_BLACK_EDGE_DENSITY_MAX = 0.005
# 亮度高于该值视为"非近黑"像素。
NON_BLACK_LUMA_THRESHOLD = 24
# 边缘判定：相邻像素亮度差超过该值记为边缘。
EDGE_LUMA_DELTA = 16
# PPM 安全上限（与设计文档 max_capture_bytes=16MiB 对齐）。
MAX_PPM_BYTES = 16 * 1024 * 1024
# 采样上限：大图为控制纯 Python 计算耗时按步长采样，指标仍具代表性。
MAX_SAMPLED_PIXELS = 250_000


class PpmParseError(ValueError):
    """PPM 结构不合法（畸形头、尺寸异常、数据不足等）。"""


@dataclass(frozen=True)
class PpmImage:
    width: int
    height: int
    maxval: int
    # RGB 交错数据，长度 = width * height * 3（maxval < 256）。
    data: bytes


def parse_ppm(raw: bytes) -> PpmImage:
    """解析 P6（二进制）PPM；只支持 maxval<256 的单字节样本。

    控制台 screendump 输出固定为 P6；其他魔数（P1-P5）一律拒绝，避免把
    任意图片冒充为控制台截图。
    """

    if not isinstance(raw, (bytes, bytearray)):
        raise PpmParseError("PPM 输入必须是字节串")
    if len(raw) > MAX_PPM_BYTES:
        raise PpmParseError(f"PPM 超过大小上限 {MAX_PPM_BYTES} 字节")
    if len(raw) < 16:
        raise PpmParseError("PPM 数据过短")

    pos = 0

    def _next_token() -> bytes:
        nonlocal pos
        # 跳过空白与 '#' 注释行（PPM 规范允许头部出现注释）。
        while pos < len(raw):
            ch = raw[pos : pos + 1]
            if ch == b"#":
                while pos < len(raw) and raw[pos : pos + 1] != b"\n":
                    pos += 1
                continue
            if ch.isspace():
                pos += 1
                continue
            break
        start = pos
        while pos < len(raw) and not raw[pos : pos + 1].isspace():
            pos += 1
        token = raw[start:pos]
        if not token:
            raise PpmParseError("PPM 头部不完整")
        return token

    magic = _next_token()
    if magic != b"P6":
        raise PpmParseError(f"仅支持 P6 二进制 PPM，实际魔数: {magic[:4]!r}")
    try:
        width = int(_next_token())
        height = int(_next_token())
        maxval = int(_next_token())
    except ValueError as exc:
        raise PpmParseError(f"PPM 头部数值非法: {exc}") from exc
    if width <= 0 or height <= 0 or width > 16384 or height > 16384:
        raise PpmParseError(f"PPM 尺寸异常: {width}x{height}")
    if not 0 < maxval < 256:
        raise PpmParseError(f"仅支持 maxval<256 的 PPM，实际: {maxval}")

    # 头部以单个空白字符结束。
    pos += 1
    expected = width * height * 3
    pixel_data = raw[pos:]
    if len(pixel_data) < expected:
        raise PpmParseError(f"PPM 像素数据不足: 期望 {expected}，实际 {len(pixel_data)}")
    return PpmImage(width=width, height=height, maxval=maxval, data=bytes(pixel_data[:expected]))


def compute_quality_metrics(image: PpmImage) -> dict[str, Any]:
    """计算确定性质量指标（按步长采样，指标随算法修订版本入库）。"""

    total_pixels = image.width * image.height
    stride = max(1, total_pixels // MAX_SAMPLED_PIXELS)
    data = image.data
    scale = 255.0 / float(image.maxval) if image.maxval != 255 else 1.0

    luma_values: list[int] = []
    edge_pixels = 0
    compared_pairs = 0

    for index in range(0, total_pixels, stride):
        offset = index * 3
        r = data[offset]
        g = data[offset + 1]
        b = data[offset + 2]
        # Rec.601 亮度；maxval 非 255 时线性归一。
        luma = int((299 * r + 587 * g + 114 * b) / 1000 * scale)
        luma_values.append(luma)
        # 边缘密度：与左侧相邻像素比较（跳过行首）。
        if index % image.width != 0:
            left = offset - 3
            left_luma = int((299 * data[left] + 587 * data[left + 1] + 114 * data[left + 2]) / 1000 * scale)
            compared_pairs += 1
            if abs(luma - left_luma) > EDGE_LUMA_DELTA:
                edge_pixels += 1

    sampled = len(luma_values)
    if sampled == 0:
        raise PpmParseError("PPM 无可采样像素")
    mean_luma = sum(luma_values) / sampled
    variance = sum((item - mean_luma) ** 2 for item in luma_values) / sampled
    non_black = sum(1 for item in luma_values if item > NON_BLACK_LUMA_THRESHOLD)
    # 单色比例：最常见亮度值的占比（亮度直方图近似）。
    histogram: dict[int, int] = {}
    for item in luma_values:
        histogram[item] = histogram.get(item, 0) + 1
    dominant_count = max(histogram.values())

    return {
        "algorithm_revision": NEAR_BLACK_ALGORITHM_REVISION,
        "width": image.width,
        "height": image.height,
        "pixel_count": total_pixels,
        "file_bytes": len(image.data),
        "sampled_pixels": sampled,
        "sample_stride": stride,
        "mean_luma": round(mean_luma, 4),
        "luma_std": round(math.sqrt(variance), 4),
        "non_black_ratio": round(non_black / sampled, 6),
        "dominant_ratio": round(dominant_count / sampled, 6),
        "edge_density": round(edge_pixels / compared_pairs, 6) if compared_pairs else 0.0,
        # OCR 能力在 P1 引入；P0 固定为 false。
        "ocr_available": False,
    }


def is_near_black(metrics: dict[str, Any]) -> bool:
    """按固定阈值判定"近黑/无有效画面"。阈值与算法版本必须一同入库。"""

    if metrics.get("algorithm_revision") != NEAR_BLACK_ALGORITHM_REVISION:
        # 不同算法修订的指标不可直接比较；fail-closed 不判定为近黑。
        return False
    return (
        float(metrics.get("mean_luma", 255.0)) < NEAR_BLACK_MEAN_LUMA_MAX
        and float(metrics.get("non_black_ratio", 1.0)) < NEAR_BLACK_NON_BLACK_RATIO_MAX
        and float(metrics.get("edge_density", 1.0)) < NEAR_BLACK_EDGE_DENSITY_MAX
    )


def analyze_ppm_near_black(raw: bytes) -> dict[str, Any]:
    """一步完成解析 + 指标 + 判定；解析失败同样返回结构化结果（fail-closed）。"""

    try:
        image = parse_ppm(raw)
    except PpmParseError as exc:
        return {
            "algorithm_revision": NEAR_BLACK_ALGORITHM_REVISION,
            "parse_ok": False,
            "parse_error": str(exc),
            "near_black": False,
            "metrics": {},
        }
    metrics = compute_quality_metrics(image)
    return {
        "algorithm_revision": NEAR_BLACK_ALGORITHM_REVISION,
        "parse_ok": True,
        "near_black": is_near_black(metrics),
        "metrics": metrics,
    }
