"""
scripts/kbd/ocr.py — PaddleOCR 图片文字提取封装

功能：
  - 接收图片路径，调用 PaddleOCR 提取所有可见文字
  - 按阅读顺序（Y 坐标从上到下）排序文字块
  - 同一行内多个文字块（X 方向相近）合并为一行
  - 返回过滤后的有意义文字行列表
  - Model 文件首次运行时自动下载（约 300MB，后续缓存）

依赖安装：
  uv pip install -e ".[ocr]"
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("kbd.ocr")

# PaddleOCR 实例（懒加载，避免进程启动时等待模型加载）
_ocr_instance: "PaddleOCR | None" = None  # type: ignore[name-defined]


def _get_ocr() -> "PaddleOCR":  # type: ignore[name-defined]
    """获取或初始化 PaddleOCR 单例（线程不安全，仅用于单进程 CLI 场景）。"""
    import os

    global _ocr_instance  # noqa: PLW0603
    if _ocr_instance is None:
        # 跳过模型来源连通性检查（默认会阻塞等待远端响应）
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        try:
            from paddleocr import PaddleOCR  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "PaddleOCR 未安装，请运行：uv pip install -e '.[ocr]'"
            ) from exc
        logger.info("正在初始化 PaddleOCR（首次运行会下载模型，约 300MB）...")
        _ocr_instance = PaddleOCR(
            use_textline_orientation=True,  # 自动检测文字方向
            lang="ch",                       # 支持中英混合
            # PaddleOCR 3.x 已移除 show_log 参数
        )
        logger.info("PaddleOCR 初始化完成")
    return _ocr_instance


def _merge_same_row(blocks: list[tuple[float, float, str]], row_gap: float = 0.6) -> list[str]:
    """
    将同一行内的多个文字块合并为一行文字（按 X 坐标排序）。

    Args:
        blocks: [(y中心, x左边, 文字), ...] 列表，已按 y 排序
        row_gap: 行高的比例阈值，y 差 < row_gap * 平均行高则判定为同行

    Returns:
        合并后的文字行列表，保持从上到下的阅读顺序
    """
    if not blocks:
        return []

    # 估算行高（用所有 y 坐标中 10-90 分位的差值）
    ys = [b[0] for b in blocks]
    if len(ys) >= 2:
        sorted_ys = sorted(ys)
        # 相邻 y 间距中位数作为行高估算
        gaps = [sorted_ys[i + 1] - sorted_ys[i] for i in range(len(sorted_ys) - 1) if sorted_ys[i + 1] - sorted_ys[i] > 0]
        line_height = sorted(gaps)[len(gaps) // 2] if gaps else 20.0
    else:
        line_height = 20.0

    threshold = line_height * row_gap

    rows: list[list[tuple[float, float, str]]] = []
    current_row: list[tuple[float, float, str]] = [blocks[0]]

    for block in blocks[1:]:
        y_diff = abs(block[0] - current_row[-1][0])
        if y_diff <= threshold:
            # 同行
            current_row.append(block)
        else:
            rows.append(current_row)
            current_row = [block]
    rows.append(current_row)

    # 每行内按 X 坐标排序，拼接文字（空格分隔）
    lines: list[str] = []
    for row in rows:
        row_sorted = sorted(row, key=lambda b: b[1])  # 按 x 排序
        text = " ".join(item[2] for item in row_sorted).strip()
        if text:
            lines.append(text)
    return lines


def extract_text(image_path: Path) -> list[str]:
    """
    从图片中提取所有可见文字，返回按阅读顺序排列的文字行列表。

    Args:
        image_path: 图片文件路径（支持 jpg/png/bmp 等）

    Returns:
        文字行列表（已过滤空行和低置信度结果）；
        提取失败或无文字时返回空列表
    """
    ocr = _get_ocr()
    img_str = str(image_path)

    try:
        results = ocr.predict(img_str)
    except Exception as exc:
        logger.error("PaddleOCR 提取失败 path=%s 原因=%s", image_path.name, exc)
        return []

    if not results:
        return []

    # results 结构：[{"rec_texts": [...], "rec_scores": [...], "rec_polys": [...]}]
    # rec_polys 每个元素是 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]] 四边形顶点
    blocks: list[tuple[float, float, str]] = []
    for page in results:
        texts = page.get("rec_texts", []) or []
        scores = page.get("rec_scores", []) or []
        polys = page.get("rec_polys", []) or []

        for text, score, poly in zip(texts, scores, polys):
            text = (text or "").strip()
            # 过滤空字符串和低置信度（< 0.5）结果
            if not text or (score is not None and float(score) < 0.5):
                continue
            # 计算文字块中心坐标
            if poly and len(poly) >= 2:
                ys = [pt[1] for pt in poly]
                xs = [pt[0] for pt in poly]
                y_center = sum(ys) / len(ys)
                x_left = min(xs)
            else:
                y_center, x_left = 0.0, 0.0
            blocks.append((y_center, x_left, text))

    if not blocks:
        return []

    # 按 Y 坐标排序（从上到下）
    blocks.sort(key=lambda b: b[0])

    return _merge_same_row(blocks)
