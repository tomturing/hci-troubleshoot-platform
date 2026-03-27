"""
KB Service — 知识原子审核路由 (T17 知识反馈闭环)

GET    /api/v1/atoms/pending      — 列出待审核知识原子（verified=false）
GET    /api/v1/atoms/{id}         — 获取单个知识原子详情
POST   /api/v1/atoms              — 写入新知识原子（由 conversation-service 调用）
PATCH  /api/v1/atoms/{id}/verify  — 审核通过，设置 verified=true
PATCH  /api/v1/atoms/{id}         — 编辑修正知识原子内容
DELETE /api/v1/atoms/{id}         — 拒绝并删除知识原子

鉴权：所有写操作（POST/PATCH/DELETE）需要 INTERNAL_API_TOKEN
      GET 操作无需鉴权（仅在 K8s NetworkPolicy 层做隔离）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-review")
router = APIRouter(prefix="/api/v1/atoms", tags=["review"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    global _db_manager
    _db_manager = db


def _check_auth(request: Request) -> None:
    """验证内部服务 Token（INTERNAL_API_TOKEN）"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")


# ─────────────────────────────────────────────────────────────────────────────
# 请求/响应模型
# ─────────────────────────────────────────────────────────────────────────────


class AtomCreateRequest(BaseModel):
    """创建知识原子请求（由 conversation-service 的 KnowledgeExtractor 调用）"""

    id: str = Field(..., description="原子 ID，格式 ka-{12位hex}")
    atom_type: str = Field(..., description="类型：diagnostic_step|fix_action|decision_gate")
    category_id: str = Field("", description="故障分类 ID")
    trigger_json: dict = Field(default_factory=dict, description="触发条件 JSON")
    content_json: dict = Field(default_factory=dict, description="内容 JSON")
    source_type: str = Field("session", description="来源类型")
    source_ref: str = Field("", description="来源会话 ID")
    verified: bool = Field(False, description="是否已验证（通常为 false）")
    confidence: float = Field(0.70, description="置信度 0.00-1.00")


class AtomUpdateRequest(BaseModel):
    """编辑知识原子请求"""

    atom_type: str | None = Field(None, description="修改类型")
    category_id: str | None = Field(None, description="修改分类")
    trigger_json: dict | None = Field(None, description="修改触发条件")
    content_json: dict | None = Field(None, description="修改内容")
    confidence: float | None = Field(None, ge=0.0, le=1.0, description="修改置信度")


class AtomVerifyRequest(BaseModel):
    """审核通过请求"""

    verified_by: str = Field(..., min_length=1, description="审核者 ID（如用户名）")


# ─────────────────────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/pending")
async def list_pending_atoms(
    category_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """列出待审核知识原子（verified=false），供管理后台展示"""
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    offset = (page - 1) * page_size
    async with _db_manager.async_session_factory() as session:
        from sqlalchemy import text

        # 动态拼接分类过滤（参数化查询，防 SQL 注入）
        where_clause = "WHERE verified = false"
        params: dict[str, Any] = {"limit": page_size, "offset": offset}
        if category_id:
            where_clause += " AND category_id = :category_id"
            params["category_id"] = category_id

        count_result = await session.execute(
            text(f"SELECT COUNT(*) FROM knowledge_atoms {where_clause}"),
            params,
        )
        total = count_result.scalar_one()

        rows = await session.execute(
            text(
                f"""
                SELECT id, atom_type, category_id, trigger_json, content_json,
                       source_type, source_ref, confidence, created_at
                FROM knowledge_atoms
                {where_clause}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        atoms = [dict(row._mapping) for row in rows]

    return {"atoms": atoms, "total": total, "page": page, "page_size": page_size}


@router.get("/{atom_id}")
async def get_atom(atom_id: str) -> dict[str, Any]:
    """获取知识原子详情"""
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        from sqlalchemy import text

        row = await session.execute(
            text("SELECT * FROM knowledge_atoms WHERE id = :id"),
            {"id": atom_id},
        )
        atom = row.mappings().first()

    if not atom:
        raise HTTPException(status_code=404, detail=f"知识原子 {atom_id} 不存在")
    return dict(atom)


@router.post("", status_code=201)
async def create_atom(request: Request, body: AtomCreateRequest) -> dict[str, Any]:
    """写入新知识原子（由 conversation-service 的 KnowledgeExtractor 调用）"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    import json

    async with _db_manager.async_session_factory() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                """
                INSERT INTO knowledge_atoms
                    (id, atom_type, category_id, trigger_json, content_json,
                     source_type, source_ref, verified, confidence)
                VALUES
                    (:id, :atom_type, :category_id, :trigger_json::jsonb, :content_json::jsonb,
                     :source_type, :source_ref, :verified, :confidence)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": body.id,
                "atom_type": body.atom_type,
                "category_id": body.category_id,
                "trigger_json": json.dumps(body.trigger_json, ensure_ascii=False),
                "content_json": json.dumps(body.content_json, ensure_ascii=False),
                "source_type": body.source_type,
                "source_ref": body.source_ref,
                "verified": body.verified,
                "confidence": body.confidence,
            },
        )
        await session.commit()

    logger.info(
        event="atom_created",
        atom_id=body.id,
        atom_type=body.atom_type,
        source_ref=body.source_ref,
    )
    return {"id": body.id, "status": "created"}


@router.patch("/{atom_id}/verify")
async def verify_atom(request: Request, atom_id: str, body: AtomVerifyRequest) -> dict[str, Any]:
    """审核通过知识原子（设置 verified=true）"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        from sqlalchemy import text

        result = await session.execute(
            text(
                """
                UPDATE knowledge_atoms
                SET verified = true,
                    verified_at = NOW(),
                    verified_by = :verified_by
                WHERE id = :id
                RETURNING id
                """
            ),
            {"id": atom_id, "verified_by": body.verified_by},
        )
        updated = result.fetchone()
        if not updated:
            raise HTTPException(status_code=404, detail=f"知识原子 {atom_id} 不存在")
        await session.commit()

    logger.info(
        event="atom_verified",
        atom_id=atom_id,
        verified_by=body.verified_by,
    )
    return {"id": atom_id, "status": "verified"}


@router.patch("/{atom_id}")
async def update_atom(request: Request, atom_id: str, body: AtomUpdateRequest) -> dict[str, Any]:
    """编辑修正知识原子内容"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    import json

    # 构建动态 SET 子句（仅更新提供的字段）
    set_parts: list[str] = []
    params: dict[str, Any] = {"id": atom_id}
    if body.atom_type is not None:
        set_parts.append("atom_type = :atom_type")
        params["atom_type"] = body.atom_type
    if body.category_id is not None:
        set_parts.append("category_id = :category_id")
        params["category_id"] = body.category_id
    if body.trigger_json is not None:
        set_parts.append("trigger_json = :trigger_json::jsonb")
        params["trigger_json"] = json.dumps(body.trigger_json, ensure_ascii=False)
    if body.content_json is not None:
        set_parts.append("content_json = :content_json::jsonb")
        params["content_json"] = json.dumps(body.content_json, ensure_ascii=False)
    if body.confidence is not None:
        set_parts.append("confidence = :confidence")
        params["confidence"] = body.confidence

    if not set_parts:
        raise HTTPException(status_code=400, detail="请提供至少一个需要更新的字段")

    async with _db_manager.async_session_factory() as session:
        from sqlalchemy import text

        result = await session.execute(
            text(f"UPDATE knowledge_atoms SET {', '.join(set_parts)} WHERE id = :id RETURNING id"),
            params,
        )
        updated = result.fetchone()
        if not updated:
            raise HTTPException(status_code=404, detail=f"知识原子 {atom_id} 不存在")
        await session.commit()

    logger.info(event="atom_updated", atom_id=atom_id)
    return {"id": atom_id, "status": "updated"}


@router.delete("/{atom_id}", status_code=204, response_model=None)
async def delete_atom(request: Request, atom_id: str):
    """拒绝并删除知识原子"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        from sqlalchemy import text

        result = await session.execute(
            text("DELETE FROM knowledge_atoms WHERE id = :id RETURNING id"),
            {"id": atom_id},
        )
        deleted = result.fetchone()
        if not deleted:
            raise HTTPException(status_code=404, detail=f"知识原子 {atom_id} 不存在")
        await session.commit()

    logger.info(event="atom_deleted", atom_id=atom_id)
    return Response(status_code=204)
