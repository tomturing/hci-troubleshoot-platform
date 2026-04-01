"""
scripts/kbd/classifier.py — AI 分类器

功能：
  对 kbd_entry 中 status='draft' 且 ai_category_id 为空的条目，
  调用 LLM，从 category_baseline.yaml 的 198 个分类中选择最匹配的。

输出：
  - ai_category_id: 分类 code（如 "虚拟机-003"）
  - ai_category_conf: 置信度（0-1）
  - ai_category_reason: 分类理由（供审核参考）

设计特点：
  - 将 198 个分类格式化为结构化列表放入 prompt
  - 要求 LLM 返回 JSON 格式（id + confidence + reason）
  - 低置信度（< MIN_CLASSIFY_CONFIDENCE）时标记，提示人工重新分类
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import asyncpg
import yaml
from openai import AsyncOpenAI

from .config import settings

logger = logging.getLogger("kbd.classifier")


# ─── Category Baseline 加载 ──────────────────────────────────────────────────

def _load_categories(yaml_path: Path | None = None) -> list[dict]:
    """加载 category_baseline.yaml，返回分类列表"""
    path = yaml_path or settings.CATEGORY_BASELINE
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("categories", [])


def _format_category_list(categories: list[dict]) -> str:
    """
    将分类列表格式化为 LLM prompt 中的分类参考文本。
    示例：
      虚拟机-001 | 虚拟机>虚拟机创建 | 虚拟机创建失败
    """
    lines: list[str] = []
    for cat in categories:
        code = cat.get("id") or cat.get("code") or ""
        label = cat.get("label") or ""
        path_parts = cat.get("path") or [cat.get("domain", ""), label]
        path_str = " > ".join(str(p) for p in path_parts if p)
        lines.append(f"{code} | {path_str} | {label}")
    return "\n".join(lines)


# ─── 分类 Prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是深信服 HCI 超融合平台的技术支持专家，熟悉所有产品故障分类体系。
你的任务是根据故障案例的标题和问题描述，从给定的故障分类列表中选择最匹配的分类。

故障分类列表（格式：分类ID | 路径 | 标签）：
{category_list}

要求：
1. 只能从以上分类中选择一个，不得创造新分类
2. 选择最精确的叶节点分类，而非宽泛的顶层分类
3. 置信度（confidence）：如果标题或描述与分类非常吻合，给 0.8-1.0；有一定歧义给 0.5-0.8；极不确定给 0.5 以下
4. 返回 JSON 格式，字段：category_id（分类ID字符串）、confidence（浮点数）、reason（中文理由，30字以内）
"""

_USER_TEMPLATE = """\
案例标题：{title}

问题描述：
{problem_desc}
"""


async def classify_case(
    case_id: str,
    title: str,
    problem_desc: str,
    client: AsyncOpenAI,
    categories: list[dict],
) -> dict[str, object]:
    """
    对单个案例调用 LLM 分类。

    Returns:
        {"category_id": "...", "confidence": 0.85, "reason": "..."}
    """
    category_list_text = _format_category_list(categories)
    system_msg = _SYSTEM_PROMPT.format(category_list=category_list_text)
    # 问题描述截断（避免 token 过长）
    desc_truncated = (problem_desc or "")[:800]

    try:
        response = await client.chat.completions.create(
            model=settings.CLASSIFY_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": _USER_TEMPLATE.format(
                    title=title,
                    problem_desc=desc_truncated,
                )},
            ],
            response_format={"type": "json_object"},
            max_tokens=200,
            temperature=0.1,
            timeout=settings.LLM_TIMEOUT,
        )
        raw = response.choices[0].message.content or "{}"
        result = json.loads(raw)

        # 校验 category_id 是否在合法列表中
        valid_ids = {cat.get("id") or cat.get("code") for cat in categories}
        cat_id = result.get("category_id", "")
        if cat_id not in valid_ids:
            logger.warning("LLM 返回无效分类 ID: %s，case_id=%s", cat_id, case_id)
            result["category_id"] = None
            result["confidence"] = 0.0
            result["reason"] = f"LLM 返回无效分类 ID: {cat_id}"

        return {
            "category_id": result.get("category_id"),
            "confidence": float(result.get("confidence", 0.0)),
            "reason": str(result.get("reason") or ""),
        }

    except json.JSONDecodeError as exc:
        logger.error("分类结果 JSON 解析失败 case_id=%s 原因=%s", case_id, exc)
        return {"category_id": None, "confidence": 0.0, "reason": "JSON解析失败"}
    except Exception as exc:
        logger.error("分类 LLM 调用失败 case_id=%s 原因=%s", case_id, exc)
        return {"category_id": None, "confidence": 0.0, "reason": f"API调用失败: {exc}"}


async def classify_batch(
    case_ids: list[str],
    pool: asyncpg.Pool,
) -> dict[str, int]:
    """
    批量对未分类的 kbd_entry 进行 AI 分类。

    Returns:
        {"done": N, "failed": N, "low_confidence": N}
    """
    categories = _load_categories()
    if not categories:
        raise RuntimeError(f"category_baseline.yaml 加载失败或为空：{settings.CATEGORY_BASELINE}")

    client = AsyncOpenAI(
        api_key=settings.ZAI_API_KEY,
        base_url=settings.ZAI_BASE_URL,
        timeout=settings.LLM_TIMEOUT,
    )

    stats = {"done": 0, "failed": 0, "low_confidence": 0}

    for case_id in case_ids:
        row = await pool.fetchrow(
            """SELECT title, problem_desc FROM kbd_entry
               WHERE case_id = $1 AND (ai_category_id IS NULL OR ai_category_id = '')""",
            case_id,
        )
        if not row:
            continue

        result = await classify_case(
            case_id=case_id,
            title=row["title"] or "",
            problem_desc=row["problem_desc"] or "",
            client=client,
            categories=categories,
        )

        conf = result["confidence"]
        is_low = conf < settings.MIN_CLASSIFY_CONFIDENCE

        await pool.execute(
            """UPDATE kbd_entry
               SET ai_category_id=$1, ai_category_conf=$2, ai_category_reason=$3, updated_at=NOW()
               WHERE case_id=$4""",
            result["category_id"],
            conf,
            result["reason"],
            case_id,
        )

        if result["category_id"]:
            stats["done"] += 1
            if is_low:
                stats["low_confidence"] += 1
        else:
            stats["failed"] += 1

        logger.debug(
            "分类完成 case_id=%s category=%s conf=%.2f",
            case_id, result["category_id"], conf,
        )

    logger.info(
        "批量分类完成 done=%d failed=%d low_conf=%d",
        stats["done"], stats["failed"], stats["low_confidence"],
    )
    return stats
