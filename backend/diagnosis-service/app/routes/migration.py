"""Bundle 迁移 API 路由"""

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..dependencies import _require_database_manager
from ..services.bundle_migration import (
    CURRENT_BUNDLE_FACTORY_VERSION,
    batch_migrate_bundles,
    check_factory_version_health,
)

router = APIRouter(prefix="/api/v1/bundle-migration", tags=["bundle-migration"])


class MigrateRequest(BaseModel):
    """迁移请求"""

    kbd_ids: list[int] | None = None
    dry_run: bool = False


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为请求创建事务级数据库会话。"""
    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield session


@router.get("/health")
async def get_bundle_migration_health(
    session: AsyncSession = Depends(get_db_session),
):
    """获取 Bundle 迁移健康状态

    Returns:
        当前工厂版本和需要迁移的 Bundle 数量
    """
    return await check_factory_version_health(session)


@router.post("/migrate")
async def migrate_bundles(
    request: MigrateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """批量迁移 Bundle

    Args:
        request: 迁移请求参数

    Returns:
        迁移结果
    """
    result = await batch_migrate_bundles(
        session,
        kbd_ids=request.kbd_ids,
        dry_run=request.dry_run,
    )
    return result


@router.get("/version")
async def get_current_factory_version():
    """获取当前 Bundle 工厂版本

    Returns:
        当前版本信息
    """
    return {
        "current_version": CURRENT_BUNDLE_FACTORY_VERSION,
        "description": "Bundle 工厂当前版本，所有新编译的 Bundle 都会使用此版本",
    }
