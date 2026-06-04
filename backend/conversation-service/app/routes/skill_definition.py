"""
技能定义管理路由 — 提供对 skill_definition 表的增删改查接口
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from shared.database.postgres import DatabaseManager
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill_definition import SkillDefinition

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

# 由 main.py 注入数据库管理器
database_manager: DatabaseManager | None = None


def set_skill_database_manager(db: DatabaseManager) -> None:
    """依赖注入"""
    global database_manager
    database_manager = db


async def get_db() -> AsyncSession:
    """获取数据库会话"""
    if not database_manager:
        raise HTTPException(status_code=500, detail="数据库管理器未初始化")
    async for session in database_manager.get_session():
        yield session


@router.get("", summary="获取技能定义列表")
async def list_skills(
    is_active: bool | None = Query(None, description="按是否启用状态过滤"),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """查询 AI 技能定义库列表"""
    stmt = select(SkillDefinition).order_by(SkillDefinition.skill_name)
    if is_active is not None:
        stmt = stmt.where(SkillDefinition.is_active == is_active)

    result = await db.execute(stmt)
    skills = result.scalars().all()

    return [
        {
            "id": s.id,
            "skill_name": s.skill_name,
            "display_name": s.display_name,
            "description": s.description,
            "parameters_schema": s.parameters_schema,
            "output_schema": s.output_schema,
            "is_active": s.is_active,
            "version": s.version,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in skills
    ]


@router.get("/{skill_id}", summary="获取技能详情")
async def get_skill(skill_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """查询单个技能的完整详细定义"""
    stmt = select(SkillDefinition).where(SkillDefinition.id == skill_id)
    result = await db.execute(stmt)
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="技能定义不存在")

    return {
        "id": s.id,
        "skill_name": s.skill_name,
        "display_name": s.display_name,
        "description": s.description,
        "parameters_schema": s.parameters_schema,
        "output_schema": s.output_schema,
        "is_active": s.is_active,
        "version": s.version,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.post("", summary="创建新技能定义", status_code=210)
async def create_skill(payload: dict[str, Any], db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """创建一条新的技能定义记录。"""
    skill_name = payload.get("skill_name")
    if not skill_name:
        raise HTTPException(status_code=400, detail="技能标识名 (skill_name) 必填")

    # 检查是否重名
    stmt = select(SkillDefinition).where(SkillDefinition.skill_name == skill_name)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"技能名 '{skill_name}' 已存在")

    s = SkillDefinition(
        skill_name=skill_name,
        display_name=payload.get("display_name") or skill_name,
        description=payload.get("description") or "",
        parameters_schema=payload.get("parameters_schema") or {},
        output_schema=payload.get("output_schema") or {},
        is_active=bool(payload.get("is_active", True)),
        version=payload.get("version") or "1.0",
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)

    logger.info(f"创建了新的技能定义: skill_name={s.skill_name}, id={s.id}")
    return {"status": "success", "id": s.id}


@router.put("/{skill_id}", summary="修改技能定义")
async def update_skill(skill_id: int, payload: dict[str, Any], db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """更新一条已有的技能定义记录"""
    stmt = select(SkillDefinition).where(SkillDefinition.id == skill_id)
    result = await db.execute(stmt)
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="技能定义不存在")

    skill_name = payload.get("skill_name")
    if skill_name and skill_name != s.skill_name:
        # 重名校验
        check_stmt = select(SkillDefinition).where(SkillDefinition.skill_name == skill_name)
        check_res = await db.execute(check_stmt)
        if check_res.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"技能名 '{skill_name}' 已存在")
        s.skill_name = skill_name

    if "display_name" in payload:
        s.display_name = payload["display_name"]
    if "description" in payload:
        s.description = payload["description"]
    if "parameters_schema" in payload:
        s.parameters_schema = payload["parameters_schema"]
    if "output_schema" in payload:
        s.output_schema = payload["output_schema"]
    if "is_active" in payload:
        s.is_active = bool(payload["is_active"])
    if "version" in payload:
        s.version = payload["version"]

    await db.commit()
    logger.info(f"更新了技能定义: skill_name={s.skill_name}, id={s.id}")
    return {"status": "success"}


@router.delete("/{skill_id}", summary="删除技能定义")
async def delete_skill(skill_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """删除一条已有的技能定义记录"""
    stmt = delete(SkillDefinition).where(SkillDefinition.id == skill_id)
    result = await db.execute(stmt)
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="技能定义不存在")

    logger.info(f"删除了技能定义: id={skill_id}")
    return {"status": "success"}
