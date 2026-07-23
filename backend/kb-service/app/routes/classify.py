"""
KB Service — LLM 分类路由

POST /api/kb/classify
  - 基于 LLM 的知识分类接口（KBD 生产流水线使用）
  - 从 kb_category 表读取 198 个分类节点
  - 构建 Prompt 让 LLM 选择最匹配的 top3
  - 调用 LLM API（OpenAI-compatible）
  - 低置信度（< 0.5）标记 needs_review=true
  - 调用方：KBD 生产流水线 Stage 4（AI 分类建议）
  - 请求参数：title + problem_desc

"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from shared.observability.logger import get_logger
from shared.utils.prompt_loader import StrictPromptLoader
from sqlalchemy import text

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager


logger = get_logger("kb-service-classify")
router = APIRouter(prefix="/api/kb", tags=["classify"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None

# LLM 配置（从环境变量读取，统一使用 LLM_* 命名，与 ConfigMap hci-common-config 保持一致）
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# 优先读取 CLASSIFY_MODEL，若未配置，则回退到已验证可用的 kimi-k2.5
LLM_MODEL = os.environ.get("CLASSIFY_MODEL", "kimi-k2.5")
# 是否启用思维链（与 vision_processor.py 统一由 LLM_ENABLE_THINKING 控制，默认关闭）
LLM_ENABLE_THINKING = os.environ.get("LLM_ENABLE_THINKING", "false").lower() in ("1", "true", "yes", "on")

# 分类置信度阈值
CONFIDENCE_THRESHOLD = 0.5


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖"""
    global _db_manager
    _db_manager = db




# ─────────────────────────────────────────────────────────────────────────────
# POST /api/kb/classify — LLM 分类路由（KBD 生产流水线使用）
# ─────────────────────────────────────────────────────────────────────────────


class ClassifyRequest(BaseModel):
    """LLM 分类请求"""

    title: str = Field(..., min_length=1, max_length=200, description="案例标题")
    problem_desc: str = Field(..., min_length=1, max_length=2000, description="问题描述")


class Top3Item(BaseModel):
    """Top3 分类项"""

    category_id: str = Field(..., description="分类编码（如 虚拟机-001）")
    label: str = Field(..., description="分类标签")
    score: float = Field(..., ge=0.0, le=1.0, description="置信度分数")


class ClassifyResponse(BaseModel):
    """LLM 分类响应"""

    category_id: str = Field(..., description="推荐分类编码")
    confidence: float = Field(..., ge=0.0, le=1.0, description="置信度")
    reason: str = Field(..., description="分类理由")
    top3: list[Top3Item] = Field(..., description="Top3 分类候选")
    needs_review: bool = Field(False, description="是否需要人工审核（置信度 < 0.5）")


# 分类 Prompt 名称（从 system_prompt 表热加载，支持 admin-ui 在线管理）
_KBD_CLASSIFY_PROMPT_NAME = "kbd_classify_v1"


async def fetch_categories_for_classify(db_manager: DatabaseManager) -> list[dict]:
    """从 kb_category 表读取所有活跃分类节点（用于 LLM 分类）"""
    async with db_manager.async_session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT code, name, domain, path_labels
                FROM kb_category
                WHERE code IS NOT NULL AND is_active = TRUE
                ORDER BY domain, code
                """
            )
        )
        rows = result.fetchall()

        categories = []
        for row in rows:
            raw = row.path_labels
            if isinstance(raw, list):
                path_labels = raw
            elif isinstance(raw, str):
                path_labels = json.loads(raw)
            else:
                path_labels = []
            categories.append(
                {
                    "code": row.code,
                    "name": row.name,
                    "domain": row.domain,
                    "path": path_labels,
                }
            )

        logger.info(
            event="fetch_categories_for_classify",
            table="kb_category",
            status="success",
            category_count=len(categories),
        )
        return categories


def build_categories_text(categories: list[dict]) -> str:
    """构建分类列表文本（用于 Prompt）"""
    lines = []
    for cat in categories:
        # 格式：编码 | 标签 | 路径
        path_str = " > ".join(cat["path"]) if cat["path"] else cat["name"]
        lines.append(f"- {cat['code']}: {cat['name']}（{path_str}）")

    return "\n".join(lines)


async def call_llm(prompt: str) -> dict:
    """调用 LLM API（使用统一的 LLM_* 配置）"""
    from openai import AsyncOpenAI

    if not LLM_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY 未配置")

    client = AsyncOpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
    )

    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是 HCI 超融合平台的故障分类专家，输出严格遵循 JSON 格式。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,  # 确保输出确定性
            max_tokens=8192,
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": LLM_ENABLE_THINKING},
        )
        content = (response.choices[0].message.content or "").strip()
        logger.debug(f"LLM 响应: {content}")
    except Exception as e:
        logger.error(f"LLM API 调用失败: {e}")
        raise HTTPException(status_code=503, detail=f"LLM API 调用失败: {e}")

    # 防御：推理模型（glm-5.2 / deepseek-v4-flash）开启思维链时会先耗尽 token 预算，
    # 导致 message.content 为空、json.loads("") 报「LLM 响应格式错误」。
    # 该分支在 try 之外，避免被上方 except Exception 吞掉后误报 503。
    if not content:
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        logger.error(
            "classify LLM 未返回 JSON 正文: finish_reason=%s model=%s",
            finish_reason,
            LLM_MODEL,
        )
        raise HTTPException(
            status_code=502,
            detail=f"LLM 未返回有效 JSON 内容（finish_reason={finish_reason}，可能因思维链耗尽 token 预算）",
        )

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"LLM 响应 JSON 解析失败: {e}")
        raise HTTPException(status_code=500, detail="LLM 响应格式错误")


def validate_category_id(category_id: str, valid_codes: set[str]) -> bool:
    """校验 category_id 是否在合法列表中"""
    return category_id in valid_codes


def parse_llm_response(llm_result: dict, valid_codes: set[str]) -> ClassifyResponse:
    """解析 LLM 响应并构建 ClassifyResponse"""
    top3_raw = llm_result.get("top3", [])

    if not top3_raw:
        raise HTTPException(status_code=500, detail="LLM 未返回 top3 分类")

    # 校验并过滤非法分类
    top3_items = []
    for item in top3_raw:
        category_id = item.get("category_id", "")
        if validate_category_id(category_id, valid_codes):
            top3_items.append(
                Top3Item(
                    category_id=category_id,
                    label=item.get("label", ""),
                    score=min(1.0, max(0.0, item.get("score", 0.0))),
                )
            )
        else:
            logger.warning(f"LLM 返回非法分类编码: {category_id}")

    # 如果所有分类都被过滤，返回默认响应
    if not top3_items:
        top3_items = [Top3Item(category_id="未分类-000", label="未分类", score=0.1)]

    # 取最高置信度作为推荐分类
    top1 = top3_items[0]
    confidence = top1.score
    needs_review = confidence < CONFIDENCE_THRESHOLD

    # 合并所有理由（取第一项的理由）
    reason = top3_raw[0].get("reason", "") if top3_raw else ""

    return ClassifyResponse(
        category_id=top1.category_id,
        confidence=confidence,
        reason=reason,
        top3=top3_items,
        needs_review=needs_review,
    )


@router.post("/classify", response_model=ClassifyResponse)
async def classify(request: Request, body: ClassifyRequest) -> ClassifyResponse:
    """LLM 分类接口

    流程：
    1. 从 kb_category 表读取 198 个分类节点
    2. 构建 Prompt 包含所有分类选项
    3. 调用 ZAI LLM API
    4. 校验返回的 category_id 是否合法
    5. 低置信度标记 needs_review=true

    响应体示例：
    ```json
    {
      "category_id": "虚拟机-001",
      "confidence": 0.85,
      "reason": "问题描述中提到'虚拟机开机失败'和'CPU资源不足'，符合虚拟机创建类故障",
      "top3": [
        {"category_id": "虚拟机-001", "label": "虚拟机创建失败", "score": 0.85},
        {"category_id": "虚拟机-002", "label": "虚拟机状态异常", "score": 0.72},
        {"category_id": "虚拟机-003", "label": "虚拟机资源不足", "score": 0.68}
      ],
      "needs_review": false
    }
    ```
    """
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="classify_request",
        title=body.title[:50],
        problem_desc_len=len(body.problem_desc),
    )

    response = await classify_case(_db_manager, body.title, body.problem_desc)

    logger.info(
        event="classify_result",
        category_id=response.category_id,
        confidence=response.confidence,
        needs_review=response.needs_review,
    )

    return response


async def classify_case(
    db_manager: DatabaseManager,
    title: str,
    problem_desc: str,
) -> ClassifyResponse:
    """分类核心逻辑（可复用，供 admin.py 的 reclassify API 调用）

    流程：
      1. 读取分类列表
      2. 从数据库热加载分类 Prompt（kbd_classify_v1）
      3. 调用 LLM
      4. 解析响应并校验

    Args:
        db_manager: 数据库管理器
        title: 案例标题
        problem_desc: 问题描述

    Returns:
        ClassifyResponse
    """
    # 1. 读取分类列表
    categories = await fetch_categories_for_classify(db_manager)
    if not categories:
        raise HTTPException(status_code=503, detail="kb_category 表无分类数据")

    valid_codes = {cat["code"] for cat in categories}
    categories_text = build_categories_text(categories)

    # 2. 从数据库热加载分类 Prompt（支持 admin-ui 修改后立即生效）
    async with db_manager.async_session_factory() as session:
        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            _KBD_CLASSIFY_PROMPT_NAME,
            ["count", "categories_text", "title", "problem_desc"],
            consumer="kb-service.classify",
        )
    prompt = prompt_template.format(
        count=len(categories),
        categories_text=categories_text,
        title=title,
        problem_desc=problem_desc,
    )

    # 3. 调用 LLM
    llm_result = await call_llm(prompt)

    # 4. 解析响应
    return parse_llm_response(llm_result, valid_codes)
