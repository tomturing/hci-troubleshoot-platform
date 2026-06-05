"""
技能定义管理路由 — 提供对 skill_definition 表的增删改查接口

遵循 Agent Skills Open Standard (https://agentskills.io)
Skill 是"过程性知识包"，核心字段是 instructions_md（Markdown 正文），
而非函数接口（Tool 才有 parameters_schema / output_schema）。

渐进式加载支持（Progressive Disclosure）：
  GET /api/v1/skills              — 列表（含 description，不含 instructions_md）
  GET /api/v1/skills/{id}         — 完整详情（含 instructions_md）
  GET /api/v1/skills/{id}/discovery — 仅 name + description（Agent 发现阶段专用）
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


def _serialize_skill_list(s: SkillDefinition) -> dict[str, Any]:
    """列表视图序列化（不含 instructions_md，减少传输量）"""
    return {
        "id": s.id,
        "skill_name": s.skill_name,
        "display_name": s.display_name,
        "description": s.description,
        "compatibility": s.compatibility,
        "license": s.license,
        "allowed_tools": s.allowed_tools,
        "metadata_json": s.metadata_json or {},
        "is_active": s.is_active,
        "assets_json": s.assets_json or [],
        "references_json": s.references_json or [],
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_skill_detail(s: SkillDefinition) -> dict[str, Any]:
    """详情视图序列化（含 instructions_md 完整正文）"""
    result = _serialize_skill_list(s)
    result["instructions_md"] = s.instructions_md or ""
    return result


@router.get("", summary="获取技能定义列表")
async def list_skills(
    is_active: bool | None = Query(None, description="按启用状态过滤"),
    category: str | None = Query(None, description="按 metadata_json.category 过滤"),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """
    查询 Skill 列表。

    注意：列表响应不包含 instructions_md（正文），减少传输量。
    获取完整内容请调用 GET /api/v1/skills/{id}。
    """
    stmt = select(SkillDefinition).order_by(SkillDefinition.skill_name)
    if is_active is not None:
        stmt = stmt.where(SkillDefinition.is_active == is_active)
    if category:
        stmt = stmt.where(SkillDefinition.metadata_json["category"].astext == category)

    result = await db.execute(stmt)
    skills = result.scalars().all()
    return [_serialize_skill_list(s) for s in skills]


@router.get("/{skill_id}/discovery", summary="Agent 发现接口（仅返回 name + description）")
async def get_skill_discovery(skill_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    Agent Skills 渐进式加载——发现阶段专用接口。
    仅返回 skill_name + description，约 100 tokens，供 Agent 判断是否激活此 Skill。
    """
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
        "is_active": s.is_active,
    }


@router.get("/{skill_id}", summary="获取技能完整详情")
async def get_skill(skill_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    获取单个 Skill 完整内容（含 instructions_md 正文）。
    对应 Agent Skills 渐进式加载的"激活阶段"。
    """
    stmt = select(SkillDefinition).where(SkillDefinition.id == skill_id)
    result = await db.execute(stmt)
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="技能定义不存在")
    return _serialize_skill_detail(s)


@router.post("", summary="创建新技能定义", status_code=201)
async def create_skill(payload: dict[str, Any], db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    创建一条新的技能定义记录。

    skill_name 必须符合 kebab-case 规范：
    - 1-64 字符
    - 只含小写字母、数字、连字符
    - 不以连字符开头或结尾
    - 不含连续连字符
    """
    skill_name = payload.get("skill_name", "").strip()
    if not skill_name:
        raise HTTPException(status_code=400, detail="技能标识名 (skill_name) 必填")

    # 基础格式校验（kebab-case）
    import re
    if not re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$', skill_name) or '--' in skill_name:
        raise HTTPException(
            status_code=400,
            detail="skill_name 必须为 kebab-case：小写字母/数字/单连字符，不以连字符开头或结尾"
        )
    if len(skill_name) > 64:
        raise HTTPException(status_code=400, detail="skill_name 最长 64 字符")

    description = payload.get("description", "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description 必填，且须描述触发条件")
    if len(description) > 1024:
        raise HTTPException(status_code=400, detail="description 最长 1024 字符")

    # 重名检查
    stmt = select(SkillDefinition).where(SkillDefinition.skill_name == skill_name)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"技能名 '{skill_name}' 已存在")

    s = SkillDefinition(
        skill_name=skill_name,
        description=description,
        instructions_md=payload.get("instructions_md") or "",
        compatibility=payload.get("compatibility"),
        license=payload.get("license"),
        allowed_tools=payload.get("allowed_tools"),
        metadata_json=payload.get("metadata_json") or {},
        display_name=payload.get("display_name"),
        is_active=bool(payload.get("is_active", True)),
        assets_json=payload.get("assets_json") or [],
        references_json=payload.get("references_json") or [],
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

    # skill_name 编辑时不允许修改（作为唯一标识）
    if "description" in payload:
        desc = str(payload["description"]).strip()
        if len(desc) > 1024:
            raise HTTPException(status_code=400, detail="description 最长 1024 字符")
        s.description = desc
    if "instructions_md" in payload:
        s.instructions_md = payload["instructions_md"] or ""
    if "compatibility" in payload:
        s.compatibility = payload["compatibility"]
    if "license" in payload:
        s.license = payload["license"]
    if "allowed_tools" in payload:
        s.allowed_tools = payload["allowed_tools"]
    if "metadata_json" in payload:
        s.metadata_json = payload["metadata_json"] or {}
    if "display_name" in payload:
        s.display_name = payload["display_name"]
    if "is_active" in payload:
        s.is_active = bool(payload["is_active"])
    if "assets_json" in payload:
        s.assets_json = payload["assets_json"] or []
    if "references_json" in payload:
        s.references_json = payload["references_json"] or []

    await db.commit()
    logger.info(f"更新了技能定义: skill_name={s.skill_name}, id={s.id}")
    return {"status": "success"}


@router.put("/{skill_id}/toggle", summary="快速切换启用状态")
async def toggle_skill_status(skill_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """快速切换 Skill 启用/禁用状态"""
    stmt = select(SkillDefinition).where(SkillDefinition.id == skill_id)
    result = await db.execute(stmt)
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="技能定义不存在")

    s.is_active = not s.is_active
    await db.commit()
    logger.info(f"切换技能状态: skill_name={s.skill_name}, is_active={s.is_active}")
    return {"status": "success", "is_active": s.is_active}


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
