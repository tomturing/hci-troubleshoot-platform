"""
Prompt 模板管理路由 — 提供对 system_prompt 表的增删改查接口
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from shared.database.postgres import DatabaseManager
from shared.dynamic_resource.adapters import prompt_resource_payload
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.models.system_prompt import SystemPrompt
from shared.observability.logger import get_logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger("prompts-routes")
router = APIRouter(prefix="/api/v1/prompts", tags=["prompts"])

# 由 main.py 注入数据库管理器
database_manager: DatabaseManager | None = None


def set_prompt_database_manager(db: DatabaseManager) -> None:
    """依赖注入"""
    global database_manager
    database_manager = db


async def get_db() -> AsyncSession:
    """获取数据库会话"""
    if not database_manager:
        raise HTTPException(status_code=500, detail="数据库管理器未初始化")
    async for session in database_manager.get_session():
        yield session


async def _publish_prompt_resource(db: AsyncSession, prompt: SystemPrompt) -> dict[str, Any]:
    """将 Prompt 模板同步为动态资源 revision。"""
    payload = prompt_resource_payload(prompt)
    snapshot = await DynamicResourcePublisher(db).ensure_published(**payload)
    return {
        "resource_type": snapshot.resource_type,
        "resource_name": snapshot.resource_name,
        "revision": snapshot.revision,
        "checksum": snapshot.checksum,
    }


@router.get("", summary="获取 Prompt 模板列表")
async def list_prompts(
    stage: str | None = Query(None, description="按诊断阶段过滤 (S0/S1/S2/S3/S4/S5/BASE)"),
    is_active: bool | None = Query(None, description="按是否启用状态过滤"),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """查询 Prompt 模板列表"""
    stmt = select(SystemPrompt).order_by(SystemPrompt.stage, SystemPrompt.name)
    if stage:
        stmt = stmt.where(SystemPrompt.stage == stage)
    if is_active is not None:
        stmt = stmt.where(SystemPrompt.is_active == is_active)

    result = await db.execute(stmt)
    prompts = result.scalars().all()

    return [
        {
            "id": p.id,
            "stage": p.stage,
            "name": p.name,
            "description": p.description,
            "content_template": p.content_template,
            "version": p.version,
            "is_active": p.is_active,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in prompts
    ]


@router.get("/{prompt_id}", summary="获取 Prompt 模板详情")
async def get_prompt(prompt_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """查询单个 Prompt 模板"""
    stmt = select(SystemPrompt).where(SystemPrompt.id == prompt_id)
    result = await db.execute(stmt)
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Prompt 模板不存在")

    return {
        "id": p.id,
        "stage": p.stage,
        "name": p.name,
        "description": p.description,
        "content_template": p.content_template,
        "version": p.version,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.post("", summary="创建新 Prompt 模板", status_code=210)
async def create_prompt(payload: dict[str, Any], db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """创建一条新的 Prompt 模板记录。
    如果 is_active=true，同一 stage 的其他模板会被置为 false 状态以保持唯一激活属性。
    """
    name = payload.get("name")
    stage = payload.get("stage")
    if not name or not stage:
        raise HTTPException(status_code=400, detail="模板标识 (name) 和诊断阶段 (stage) 均必填")

    # 检查重名
    stmt = select(SystemPrompt).where(SystemPrompt.name == name)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Prompt 模板名 '{name}' 已存在")

    is_active = bool(payload.get("is_active", True))
    if is_active:
        # 将同阶段的其他 prompt 设为 inactive
        await db.execute(update(SystemPrompt).where(SystemPrompt.stage == stage).values(is_active=False))

    p = SystemPrompt(
        stage=stage,
        name=name,
        description=payload.get("description") or "",
        content_template=payload.get("content_template") or "",
        version=payload.get("version") or "1.0",
        is_active=is_active,
    )
    db.add(p)
    await db.flush()
    await db.refresh(p)
    resource_revision = await _publish_prompt_resource(db, p)
    await db.commit()

    logger.info(
        event="prompt_created",
        prompt_name=p.name,
        prompt_id=p.id,
        resource_revision=resource_revision,
        message="创建了新的 Prompt 模板",
    )
    return {"status": "success", "id": p.id, "resource_revision": resource_revision}


@router.put("/{prompt_id}", summary="修改 Prompt 模板")
async def update_prompt(prompt_id: int, payload: dict[str, Any], db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """更新已有的 Prompt 模板。
    如果 is_active 更新为 true，同一 stage 的其他模板会被置为 false。
    """
    stmt = select(SystemPrompt).where(SystemPrompt.id == prompt_id)
    result = await db.execute(stmt)
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Prompt 模板不存在")

    name = payload.get("name")
    if name and name != p.name:
        # 重名校验
        check_stmt = select(SystemPrompt).where(SystemPrompt.name == name)
        check_res = await db.execute(check_stmt)
        if check_res.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"Prompt 模板名 '{name}' 已存在")
        p.name = name

    stage = payload.get("stage", p.stage)
    p.stage = stage

    if "description" in payload:
        p.description = payload["description"]
    if "content_template" in payload:
        p.content_template = payload["content_template"]
    if "version" in payload:
        p.version = payload["version"]

    if "is_active" in payload:
        is_active = bool(payload["is_active"])
        if is_active and not p.is_active:
            # 由 False 转为 True：将同阶段的其他 prompt 设为 inactive
            await db.execute(update(SystemPrompt).where(SystemPrompt.stage == stage).values(is_active=False))
        p.is_active = is_active

    await db.flush()
    await db.refresh(p)
    resource_revision = await _publish_prompt_resource(db, p)
    await db.commit()
    logger.info(
        event="prompt_updated",
        prompt_name=p.name,
        prompt_id=p.id,
        resource_revision=resource_revision,
        message="更新了 Prompt 模板",
    )
    return {"status": "success", "resource_revision": resource_revision}


@router.delete("/{prompt_id}", summary="删除 Prompt 模板")
async def delete_prompt(prompt_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """删除已有的 Prompt 模板记录"""
    stmt = delete(SystemPrompt).where(SystemPrompt.id == prompt_id)
    result = await db.execute(stmt)
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Prompt 模板不存在")

    logger.info(event="prompt_deleted", prompt_id=prompt_id, message="删除了 Prompt 模板")
    return {"status": "success"}
