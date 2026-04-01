"""
KB Service — 分类树 API + LLM 分类路由

GET /api/kb/categories
  - 返回完整的分类树供意图识别服务使用
  - 调用方：Conversation Service（意图识别前调用）
  - 无需鉴权（Pod 内部调用）

POST /api/kb/classify
  - 基于 LLM 的知识分类接口（KBD 生产流水线使用）
  - 从 kb_category 表读取 198 个分类节点
  - 构建 Prompt 让 LLM 选择最匹配的 top3
  - 调用 ZAI API（OpenAI-compatible）
  - 低置信度（< 0.5）标记 needs_review=true
  - 调用方：KBD 生产流水线 Stage 4（AI 分类建议）
  - 请求参数：title + problem_desc

POST /api/kb/classify/intent
  - 意图识别接口（conversation-service 使用）
  - 基于 LLM 进行用户问题意图识别
  - 返回 top_n 个分类候选（默认 3）
  - 响应包含 category_id（数据库主键）用于后续调用 /api/kb/route
  - 调用方：conversation-service 意图识别模块
  - 请求参数：query + top_n

响应字段：
  - id: 分类节点 ID（数据库主键）
  - code: 分类编码（如 "虚拟机-001"，对应 category_baseline.yaml）
  - name: 分类名称（中文）
  - domain: 一级技术域（如 "虚拟机"）
  - level: 层级（1=L1, 2=L2, 3=L3, 4=L4）
  - path_labels: 从顶层到叶节点的完整路径（如 ["虚拟机", "虚拟机创建"]）
  - parent_id: 父节点 ID（NULL 表示 L1 根节点）
  - embedding: 语义向量（可选，默认不返回，因为 1536 维向量较大）
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from shared.database.postgres import Base
from shared.utils.logger import get_logger
from sqlalchemy import Column, Integer, SmallInteger, String, Text, select, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-classify")
router = APIRouter(prefix="/api/kb", tags=["classify"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None

# LLM 配置（从环境变量读取）
ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "")
ZAI_BASE_URL = os.environ.get("ZAI_BASE_URL", "http://host.docker.internal:18790")
LLM_MODEL = os.environ.get("ZAI_LLM_MODEL", "gpt-4o-mini")

# 分类置信度阈值
CONFIDENCE_THRESHOLD = 0.5


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖"""
    global _db_manager
    _db_manager = db


# ─────────────────────────────────────────────────────────────────────────────
# 响应模型
# ─────────────────────────────────────────────────────────────────────────────


class CategoryItem(BaseModel):
    """分类节点"""

    id: int = Field(..., description="分类节点 ID（数据库主键）")
    code: str | None = Field(None, description="分类编码（如 '虚拟机-001'）")
    name: str = Field(..., description="分类名称（中文）")
    domain: str | None = Field(None, description="一级技术域（如 '虚拟机'）")
    level: int = Field(..., description="层级（1=L1, 2=L2, 3=L3, 4=L4）")
    path_labels: list[str] = Field(default_factory=list, description="从顶层到叶节点的完整路径")
    parent_id: int | None = Field(None, description="父节点 ID（NULL 表示 L1 根节点）")
    embedding: list[float] | None = Field(None, description="语义向量（1536 维，可选）")


class CategoriesResponse(BaseModel):
    """分类树响应"""

    categories: list[CategoryItem] = Field(..., description="分类节点列表（按 level 排序）")
    total: int = Field(..., description="总数量")


# ─────────────────────────────────────────────────────────────────────────────
# SQLAlchemy ORM 模型（用于查询 kb_category 表）
# ─────────────────────────────────────────────────────────────────────────────


class KbCategory(Base):
    """kb_category 表 ORM 映射"""

    __tablename__ = "kb_category"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_id = Column(Integer, nullable=True)  # 自引用外键，查询时不需关联
    name = Column(String(100), nullable=False)
    level = Column(SmallInteger, nullable=False)
    keywords = Column(ARRAY(Text), nullable=True)
    source = Column(String(20), default="manual")
    version = Column(String(20), default="1.0")
    created_at = Column(String(50), nullable=True)  # TIMESTAMPTZ 映射为字符串，查询时不使用

    # 扩展字段（来自 20260401001_kbd_pipeline.sql）
    code = Column(String(32), unique=True, nullable=True)
    domain = Column(String(50), nullable=True)
    path_labels = Column(JSONB, nullable=True)
    embedding = Column(String, nullable=True)  # vector(1536) 映射为字符串，需特殊处理


# ─────────────────────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/categories", response_model=CategoriesResponse)
async def get_categories(
    request: Request,
    include_embedding: bool = Query(False, description="是否返回 embedding 向量（默认 false）"),
):
    """获取完整分类树

    返回 kb_category 表所有记录，按 level 排序（确保父节点在前）。

    响应体示例：
    ```json
    {
      "categories": [
        {
          "id": 1,
          "code": "虚拟机-001",
          "name": "虚拟机创建",
          "domain": "虚拟机",
          "level": 3,
          "path_labels": ["虚拟机", "虚拟机创建"],
          "parent_id": 10,
          "embedding": null
        }
      ],
      "total": 198
    }
    ```

    用途：
    - 意图识别服务加载分类树，用于匹配用户问题到具体分类
    - 前端展示分类树选择器
    """
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="categories_request",
        include_embedding=include_embedding,
    )

    async with _db_manager.async_session_factory() as session:
        # 查询所有分类节点，按 level 升序排序（父节点在前）
        query = select(
            KbCategory.id,
            KbCategory.code,
            KbCategory.name,
            KbCategory.domain,
            KbCategory.level,
            KbCategory.path_labels,
            KbCategory.parent_id,
        ).order_by(KbCategory.level.asc(), KbCategory.id.asc())

        # 若需要 embedding，添加到查询列
        if include_embedding:
            # 使用 raw SQL 查询 embedding（因为 SQLAlchemy 不支持 pgvector 类型）
            # 直接查询并手动处理
            result = await session.execute(
                text(
                    """
                    SELECT id, code, name, domain, level, path_labels, parent_id, embedding
                    FROM kb_category
                    ORDER BY level ASC, id ASC
                    """
                )
            )
            rows = result.fetchall()

            categories = []
            for row in rows:
                embedding_data = None
                if row.embedding is not None:
                    # pgvector 返回的是字符串格式 "[0.1,0.2,...]" 或向量对象
                    # 需转换为 Python list
                    try:
                        if isinstance(row.embedding, str):
                            # 去除方括号并分割
                            embedding_data = [
                                float(x) for x in row.embedding.replace("[", "").replace("]", "").split(",")
                            ]
                        elif hasattr(row.embedding, "__iter__"):
                            embedding_data = list(row.embedding)
                    except Exception as e:
                        logger.warning(
                            event="embedding_parse_failed",
                            category_id=row.id,
                            error=str(e),
                        )

                categories.append(
                    CategoryItem(
                        id=row.id,
                        code=row.code,
                        name=row.name,
                        domain=row.domain,
                        level=row.level,
                        path_labels=row.path_labels if row.path_labels else [],
                        parent_id=row.parent_id,
                        embedding=embedding_data,
                    )
                )

            total = len(categories)
        else:
            # 不需要 embedding，使用 ORM 查询
            result = await session.execute(query)
            rows = result.fetchall()

            categories = [
                CategoryItem(
                    id=r.id,
                    code=r.code,
                    name=r.name,
                    domain=r.domain,
                    level=r.level,
                    path_labels=r.path_labels if r.path_labels else [],
                    parent_id=r.parent_id,
                    embedding=None,
                )
                for r in rows
            ]
            total = len(categories)

    logger.info(
        event="categories_response",
        total=total,
        include_embedding=include_embedding,
    )

    return CategoriesResponse(categories=categories, total=total)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/kb/classify/intent — 意图识别接口（供 conversation-service 使用）
# ─────────────────────────────────────────────────────────────────────────────


class IntentClassifyRequest(BaseModel):
    """意图识别请求（conversation-service 调用）"""

    query: str = Field(..., min_length=1, max_length=500, description="用户问题")
    top_n: int = Field(3, ge=1, le=10, description="返回分类数量（默认 3）")


class IntentCategoryItem(BaseModel):
    """意图识别分类项"""

    category_id: int = Field(..., description="分类节点 ID（数据库主键）")
    code: str | None = Field(None, description="分类编码（如 '虚拟机-001'）")
    name: str = Field(..., description="分类名称（中文）")
    domain: str | None = Field(None, description="一级技术域")
    path_labels: list[str] = Field(default_factory=list, description="完整路径")
    score: float = Field(..., ge=0.0, le=1.0, description="置信度分数")


class IntentClassifyResponse(BaseModel):
    """意图识别响应"""

    categories: list[IntentCategoryItem] = Field(..., description="分类候选列表")
    needs_review: bool = Field(False, description="是否需要人工确认（最高置信度 < 0.5）")


# 意图识别 Prompt 模板（用于对话场景的用户问题分类）
INTENT_CLASSIFY_PROMPT_TEMPLATE = """你是 HCI 超融合平台的故障分类专家。

根据用户的问题描述，从以下分类列表中选择最匹配的分类。
返回 JSON 格式，包含 top{top_n} 分类候选。

## 分类列表（共 {count} 个）

{categories_text}

## 用户问题

{query}

## 输出要求

返回 JSON 格式：
```json
{
  "top3": [
    {"category_id": "<分类编码>", "label": "<分类标签>", "score": <置信度0-1>, "reason": "<匹配理由>"},
    {"category_id": "<分类编码>", "label": "<分类标签>", "score": <置信度0-1>, "reason": "<匹配理由>"},
    {"category_id": "<分类编码>", "label": "<分类标签>", "score": <置信度0-1>, "reason": "<匹配理由>"}
  ]
}
```

要求：
1. category_id 必须是上述分类列表中的合法编码
2. score 从高到低排列，最高为推荐分类
3. reason 简洁说明匹配依据（30字以内）
4. 如果问题不属于任何分类，第一项 score 设为 0.1
"""


@router.post("/classify/intent", response_model=IntentClassifyResponse)
async def classify_intent(request: Request, body: IntentClassifyRequest) -> IntentClassifyResponse:
    """意图识别接口（供 conversation-service 使用）

    流程：
    1. 从 kb_category 表读取所有分类节点
    2. 构建 Prompt 包含所有分类选项
    3. 调用 ZAI LLM API 进行意图识别
    4. 返回 top_n 个分类候选
    5. 低置信度标记 needs_review=true

    响应体示例：
    ```json
    {
      "categories": [
        {
          "category_id": 123,
          "code": "虚拟机-001",
          "name": "虚拟机创建失败",
          "domain": "虚拟机",
          "path_labels": ["虚拟机", "虚拟机创建"],
          "score": 0.85
        }
      ],
      "needs_review": false
    }
    ```

    用途：
    - conversation-service 在对话开始时调用此接口进行意图识别
    - 返回的 category_id 用于后续调用 GET /api/kb/route 进行知识检索
    """
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="intent_classify_request",
        query=body.query[:50],
        top_n=body.top_n,
    )

    # 1. 读取分类列表
    categories = await fetch_categories_for_classify(_db_manager)
    if not categories:
        raise HTTPException(status_code=503, detail="kb_category 表无分类数据")

    valid_codes = {cat["code"] for cat in categories}
    # 构建 code -> category info 映射（用于后续构建响应）
    code_to_category: dict[str, dict] = {cat["code"]: cat for cat in categories}
    categories_text = build_categories_text(categories)

    # 2. 构建 Prompt（根据 top_n 调整输出要求）
    prompt = INTENT_CLASSIFY_PROMPT_TEMPLATE.format(
        top_n=body.top_n,
        count=len(categories),
        categories_text=categories_text,
        query=body.query,
    )

    # 3. 调用 LLM
    try:
        llm_result = await call_llm(prompt)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(event="intent_classify_llm_error", error=str(e))
        raise HTTPException(status_code=503, detail=f"LLM 调用失败: {e}")

    # 4. 解析响应
    top_results = llm_result.get("top3", [])
    if not top_results:
        logger.warning(event="intent_classify_empty_result")
        raise HTTPException(status_code=500, detail="LLM 未返回分类结果")

    # 构建 IntentCategoryItem 列表
    intent_items: list[IntentCategoryItem] = []
    needs_review = True  # 默认需要审核

    for item in top_results[:body.top_n]:
        category_code = item.get("category_id", "")
        if category_code in valid_codes:
            cat_info = code_to_category.get(category_code, {})
            score = min(1.0, max(0.0, item.get("score", 0.0)))

            # 需要查询数据库获取 category_id（主键）
            async with _db_manager.async_session_factory() as session:
                result = await session.execute(
                    text("SELECT id FROM kb_category WHERE code = :code"),
                    {"code": category_code},
                )
                row = result.fetchone()
                db_id = row.id if row else 0

            intent_items.append(
                IntentCategoryItem(
                    category_id=db_id,
                    code=category_code,
                    name=cat_info.get("name", item.get("label", "")),
                    domain=cat_info.get("domain"),
                    path_labels=cat_info.get("path", []),
                    score=score,
                )
            )

            # 最高置信度 >= 0.5 则不需要审核
            if score >= CONFIDENCE_THRESHOLD and intent_items:
                needs_review = False

    # 如果所有分类都被过滤，返回默认响应
    if not intent_items:
        intent_items = [
            IntentCategoryItem(
                category_id=0,
                code=None,
                name="未分类",
                domain=None,
                path_labels=[],
                score=0.1,
            )
        ]
        needs_review = True

    logger.info(
        event="intent_classify_result",
        top_category=intent_items[0].code if intent_items else None,
        top_score=intent_items[0].score if intent_items else 0.0,
        needs_review=needs_review,
    )

    return IntentClassifyResponse(categories=intent_items, needs_review=needs_review)


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


# 分类 Prompt 模板
CLASSIFY_PROMPT_TEMPLATE = """你是 HCI 超融合平台的故障分类专家。

根据案例标题和问题描述，从以下分类列表中选择最匹配的分类。
返回 JSON 格式，包含 top3 分类候选。

## 分类列表（共 {count} 个）

{categories_text}

## 输入案例

**标题**: {title}

**问题描述**:
{problem_desc}

## 输出要求

返回 JSON 格式：
```json
{
  "top3": [
    {"category_id": "<分类编码>", "label": "<分类标签>", "score": <置信度0-1>, "reason": "<匹配理由>"},
    {"category_id": "<分类编码>", "label": "<分类标签>", "score": <置信度0-1>, "reason": "<匹配理由>"},
    {"category_id": "<分类编码>", "label": "<分类标签>", "score": <置信度0-1>, "reason": "<匹配理由>"}
  ]
}
```

要求：
1. category_id 必须是上述分类列表中的合法编码
2. score 从高到低排列，最高为推荐分类
3. reason 简洁说明匹配依据（50字以内）
4. 如果案例不属于任何分类，top3 第一项 score 设为 0.1
"""


async def fetch_categories_for_classify(db_manager: DatabaseManager) -> list[dict]:
    """从 kb_category 表读取所有分类节点（用于 LLM 分类）"""
    async with db_manager.async_session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT code, name, domain, path_labels
                FROM kb_category
                WHERE code IS NOT NULL
                ORDER BY domain, code
                """
            )
        )
        rows = result.fetchall()

        categories = []
        for row in rows:
            path_labels = json.loads(row.path_labels) if row.path_labels else []
            categories.append(
                {
                    "code": row.code,
                    "name": row.name,
                    "domain": row.domain,
                    "path": path_labels,
                }
            )

        logger.info(f"从 kb_category 读取 {len(categories)} 个分类节点")
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
    """调用 ZAI LLM API"""
    from openai import AsyncOpenAI

    if not ZAI_API_KEY:
        raise HTTPException(status_code=503, detail="ZAI_API_KEY 未配置")

    client = AsyncOpenAI(
        api_key=ZAI_API_KEY,
        base_url=ZAI_BASE_URL,
    )

    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是 HCI 超融合平台的故障分类专家，输出严格遵循 JSON 格式。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=500,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        logger.debug(f"LLM 响应: {content}")

        return json.loads(content)

    except json.JSONDecodeError as e:
        logger.error(f"LLM 响应 JSON 解析失败: {e}")
        raise HTTPException(status_code=500, detail="LLM 响应格式错误")

    except Exception as e:
        logger.error(f"LLM API 调用失败: {e}")
        raise HTTPException(status_code=503, detail=f"LLM API 调用失败: {e}")


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

    # 1. 读取分类列表
    categories = await fetch_categories_for_classify(_db_manager)
    if not categories:
        raise HTTPException(status_code=503, detail="kb_category 表无分类数据")

    valid_codes = {cat["code"] for cat in categories}
    categories_text = build_categories_text(categories)

    # 2. 构建 Prompt
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(
        count=len(categories),
        categories_text=categories_text,
        title=body.title,
        problem_desc=body.problem_desc,
    )

    # 3. 调用 LLM
    llm_result = await call_llm(prompt)

    # 4. 解析响应
    response = parse_llm_response(llm_result, valid_codes)

    logger.info(
        event="classify_result",
        category_id=response.category_id,
        confidence=response.confidence,
        needs_review=response.needs_review,
    )

    return response
