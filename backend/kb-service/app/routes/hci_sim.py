"""hci-sim 控制面读取 API：只解析已发布快照，不触发 KBD 发布。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from shared.database.postgres import DatabaseManager

from app.config import settings
from app.services.hci_sim_resolver import HciSimKbdResolver

router = APIRouter(prefix="/api/kb/hci-sim", tags=["hci-sim-control-plane"])

_db_manager: DatabaseManager | None = None
_resolver = HciSimKbdResolver()


def set_dependencies(db: DatabaseManager) -> None:
    """由应用生命周期注入只读解析所需的数据库会话工厂。"""

    global _db_manager
    _db_manager = db


def _check_internal_auth(request: Request) -> None:
    """Compiler 仅能通过内部服务身份读取控制面快照。"""

    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.INTERNAL_API_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="内部身份认证失败")


@router.get("/capabilities")
async def list_hci_sim_capabilities(request: Request) -> dict:
    """批量验证全部 KBD 的 C 阶段解析前置条件，不执行编译或写数据库。"""

    _check_internal_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="数据库依赖未初始化")
    async with _db_manager.async_session_factory() as session:
        return (await _resolver.resolve_all(session)).to_dict()


@router.get("/capabilities/{support_id}")
async def get_hci_sim_capability(support_id: str, request: Request) -> dict:
    """按 support_id 解析单条 KBD 的不可变输入或 capability gap。"""

    _check_internal_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="数据库依赖未初始化")
    async with _db_manager.async_session_factory() as session:
        return (await _resolver.resolve_support_id(session, support_id)).to_dict()
