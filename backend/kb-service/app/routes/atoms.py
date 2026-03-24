"""
KB Service — 知识原子搜索路由

POST /api/v1/atoms/search
  - 双路检索：精确关键字匹配优先 → 语义向量检索兜底
  - 调用方：Conversation Service（内部服务调用）
  - 无需鉴权，仅在 K8s NetworkPolicy 层做隔离
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger

from app.services.atom_search_service import AtomSearchService

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager
    from app.services.embedding import EmbeddingService

logger = get_logger("kb-service-atoms")
router = APIRouter(prefix="/api/v1/atoms", tags=["atoms"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None
_embedding_service: EmbeddingService | None = None


def set_dependencies(
    db: DatabaseManager,
    embedding: EmbeddingService,
) -> None:
    global _db_manager, _embedding_service
    _db_manager = db
    _embedding_service = embedding


# ─────────────────────────────────────────────────────────────────────────────
# 请求/响应模型
# ─────────────────────────────────────────────────────────────────────────────


class AtomSearchRequest(BaseModel):
    """知识原子搜索请求"""

    query: str = Field(..., min_length=1, max_length=500, description="用户查询文本")
    category_id: str | None = Field(None, description="分类 ID 过滤（None=不过滤），如 虚拟机-003")
    knowledge_domain: str | None = Field(None, description="知识领域：sop|case|inferred")
    stage: str | None = Field(None, description="诊断阶段过滤：S0-S4")
    hci_version: str | None = Field(None, description="HCI 版本，用于版本范围过滤，如 6.8.0")
    top_k: int = Field(5, ge=1, le=20, description="返回知识原子数量上限")
    task_error_keywords: list[str] = Field(
        default_factory=list,
        description="从任务失败描述中提取的关键字列表（触发精确检索优先路由）",
    )


class AtomSearchResponse(BaseModel):
    """知识原子搜索响应"""

    atoms: list[dict] = Field(description="知识原子结果列表")
    total: int = Field(description="命中数量")
    matched_by: str = Field(description="检索路径：exact（精确匹配） | semantic（语义检索）")
    query: str = Field(description="原始查询文本")


# ─────────────────────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/search", response_model=AtomSearchResponse)
async def search_atoms(request: Request, body: AtomSearchRequest):
    """知识原子双路检索

    检索逻辑：
    1. 若 task_error_keywords 非空，优先执行 JSONB 精确关键字匹配
    2. 精确匹配无结果时，执行 pgvector 余弦相似度语义检索（fallback）

    附加过滤条件（均为可选）：
      - category_id    : 分类树过滤
      - knowledge_domain: sop/case/inferred
      - stage          : 诊断阶段
      - hci_version    : HCI 版本范围（宽松策略：无版本字段的原子视为全版本可用）

    响应体示例：
    ```json
    {
      "atoms": [
        {
          "id": "ka-abc123xyz456",
          "type": "diagnostic_step",
          "category_id": "虚拟机-003",
          "trigger": {"stage": "S2", "task_error_keywords": ["CPU不足"]},
          "content": {"description": "检查 CPU 使用率", "commands": ["acli system top"]},
          "confidence": 0.95,
          "verified": true,
          "score": 1.0,
          "matched_by": "exact"
        }
      ],
      "total": 1,
      "matched_by": "exact",
      "query": "CPU不足开机失败"
    }
    ```
    """
    if _db_manager is None or _embedding_service is None:
        raise HTTPException(status_code=503, detail="知识原子搜索服务未就绪")

    logger.info(
        event="atom_search_request",
        query=body.query[:50],
        top_k=body.top_k,
        keywords=body.task_error_keywords[:3] if body.task_error_keywords else [],
    )

    service = AtomSearchService(
        db_manager=_db_manager,
        embedding_service=_embedding_service,
    )

    results, matched_by = await service.search(
        query=body.query,
        category_id=body.category_id,
        knowledge_domain=body.knowledge_domain,
        stage=body.stage,
        hci_version=body.hci_version,
        top_k=body.top_k,
        task_error_keywords=body.task_error_keywords or None,
    )

    logger.info(
        event="atom_search_response",
        total=len(results),
        matched_by=matched_by,
        query=body.query[:50],
    )

    return AtomSearchResponse(
        atoms=[r.to_dict() for r in results],
        total=len(results),
        matched_by=matched_by,
        query=body.query,
    )
