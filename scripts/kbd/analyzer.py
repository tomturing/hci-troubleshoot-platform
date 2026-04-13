"""
scripts/kbd/analyzer.py — 截图内容 LLM 智能分析

功能：
  接收 PaddleOCR 提取的完整文字 + 背景颜色，调用 DashScope LLM（文本模式，无图片）
  返回结构化分析结果：截图类型 / 关键内容 / 排障建议。

设计原则：
  - 纯文本调用，LLM 不看图，只分析 OCR 文字
  - TYPE 字段是后端权威，前端只读不重新判断
  - Prompt 版本化（常量 _ANALYSIS_PROMPT_VERSION），便于追踪

截图类型定义（与前端保持一致）：
  终端截图 — 黑色背景 + 含 $ 或 # Shell 提示符
  日志截图 — 黑色背景 + 含时间戳格式日志行（INFO/WARN/ERROR/debug）
  告警截图 — 白色背景 + 含紧急/普通/未处理关键词
  任务截图 — 白色背景 + 含完成/失败/操作人关键词
  其他截图 — 不满足以上条件
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger("kbd.analyzer")

# Prompt 版本号——变更 prompt 时同步更新，便于追溯历史分析结果质量
_ANALYSIS_PROMPT_VERSION = "v1.0"

_ANALYSIS_PROMPT = """\
你是 HCI 超融合平台故障排查助手。以下是从截图中用 OCR 提取的完整文字内容。

背景颜色：{background}

【截图文字（完整，共 {line_count} 行）】
{full_text_block}

请严格按以下格式输出分析结果，禁止输出任何其他内容：

TYPE: [从以下五种中选一种，原文照录]
  终端截图（黑色背景 + 含 $ 或 # Shell 提示符）
  日志截图（黑色背景 + 含时间戳格式日志行 INFO/WARN/ERROR/debug）
  告警截图（白色背景 + 含紧急/普通/未处理等告警级别词）
  任务截图（白色背景 + 含完成/失败/操作人等任务状态词）
  其他截图（不满足以上条件）

KEY:
[根据 TYPE 提取对应内容，每项以 "- " 开头，最多 8 条，无内容写 "- 无"]
  终端截图 → 最后 3-5 条命令及完整输出（原文照录）
  日志截图 → 所有 ERROR/WARN/error/warn 级别行（原文照录）
  告警截图 → 所有告警条目（级别+名称+描述）
  任务截图 → 所有"失败"状态任务（任务名+开始时间，无则写 "- 无"）
  其他截图 → 最重要的 3 条内容

TIPS:
[2-3 条排障建议，基于截图内容具体说明，每项以 "- " 开头]
"""

# 用于解析 LLM 输出的正则
_RE_TYPE = re.compile(r"^TYPE:\s*(.+)$", re.MULTILINE)
_RE_KEY_SECTION = re.compile(r"^KEY:\s*\n((?:^-\s.+\n?)+)", re.MULTILINE)
_RE_TIPS_SECTION = re.compile(r"^TIPS:\s*\n((?:^-\s.+\n?)+)", re.MULTILINE)
_RE_BULLET = re.compile(r"^-\s+(.+)$", re.MULTILINE)


@dataclass
class AnalysisResult:
    """LLM 分析结果"""
    type: str = "其他截图"
    key: list[str] = field(default_factory=list)
    tips: list[str] = field(default_factory=list)


def _parse_analysis(text: str) -> AnalysisResult:
    """解析 LLM 输出文本为结构化结果。"""
    result = AnalysisResult()

    # 提取 TYPE
    type_match = _RE_TYPE.search(text)
    if type_match:
        raw_type = type_match.group(1).strip()
        # 规范化到标准类型名称
        for standard in ("终端截图", "日志截图", "告警截图", "任务截图", "其他截图"):
            if standard in raw_type:
                result.type = standard
                break

    # 提取 KEY 部分
    key_match = _RE_KEY_SECTION.search(text)
    if key_match:
        items = _RE_BULLET.findall(key_match.group(1))
        result.key = [item.strip() for item in items if item.strip() and item.strip() != "无"]
    else:
        # 备用：在 KEY: 之后 TIPS: 之前找 bullet
        key_start = text.find("KEY:")
        tips_start = text.find("TIPS:")
        if key_start != -1:
            end = tips_start if tips_start > key_start else len(text)
            key_block = text[key_start:end]
            items = _RE_BULLET.findall(key_block)
            result.key = [item.strip() for item in items if item.strip() and item.strip() != "无"]

    # 提取 TIPS 部分
    tips_match = _RE_TIPS_SECTION.search(text)
    if tips_match:
        items = _RE_BULLET.findall(tips_match.group(1))
        result.tips = [item.strip() for item in items if item.strip() and item.strip() != "无"]
    else:
        tips_start = text.find("TIPS:")
        if tips_start != -1:
            tips_block = text[tips_start:]
            items = _RE_BULLET.findall(tips_block)
            result.tips = [item.strip() for item in items if item.strip() and item.strip() != "无"]

    return result


async def analyze_screenshot(
    background: str,
    full_text: list[str],
    client: "AsyncOpenAI",
    model: str,
    timeout: float = 60.0,
) -> AnalysisResult:
    """
    调用 DashScope LLM 分析截图内容（纯文本，无图片）。

    Args:
        background:  背景颜色（"黑色" / "白色" / "其他"）
        full_text:   OCR 提取的完整文字行列表
        client:      AsyncOpenAI 客户端（指向 DashScope）
        model:       分析模型名称（如 qwen3-max-2026-01-23）
        timeout:     请求超时（秒）

    Returns:
        AnalysisResult(type, key, tips)
    """
    if not full_text:
        logger.warning("LLM 分析：full_text 为空，返回默认结果")
        return AnalysisResult()

    full_text_block = "\n".join(f"  {line}" for line in full_text)
    prompt = _ANALYSIS_PROMPT.format(
        background=background,
        line_count=len(full_text),
        full_text_block=full_text_block,
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0.1,
            timeout=timeout,
        )
    except Exception as exc:
        logger.error("LLM 分析调用失败 model=%s 原因=%s", model, exc)
        return AnalysisResult()

    raw = (response.choices[0].message.content or "").strip()
    logger.debug("LLM 分析原始输出（前200字）：%s", raw[:200])

    result = _parse_analysis(raw)
    tokens = response.usage.total_tokens if response.usage else 0
    logger.debug(
        "LLM 分析完成 type=%s key_count=%d tips_count=%d tokens=%d",
        result.type, len(result.key), len(result.tips), tokens,
    )
    return result
