"""
KB Service — 分类管理路由

REST API：
- GET  /api/kb/categories           — 获取分类列表（按域分组）
- GET  /api/kb/categories/stats     — 获取统计信息
- PUT  /api/kb/categories/{code}    — 更新分类属性
- POST /api/kb/categories/{code}/hit — 增加 hit_count
- POST /api/kb/categories/import    — 导入 YAML（两阶段）

鉴权：
- 使用 INTERNAL_API_TOKEN（内部服务调用）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger

from app.services.category_service import CategoryService

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-categories")
router = APIRouter(prefix="/api/kb/categories", tags=["categories"])

# 由 main.py 的 set_categories_dependencies 注入
_db_manager: DatabaseManager | None = None
_category_service: CategoryService | None = None


def set_dependencies(db: DatabaseManager, embedding_service=None) -> None:
    """注入依赖"""
    global _db_manager, _category_service
    _db_manager = db
    _category_service = CategoryService(db)


def _check_auth(request: Request) -> None:
    """验证内部服务 Token"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Bearer Token",
        )
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 无效",
        )


# ---- 请求/响应模型 ----


class CategoryUpdateRequest(BaseModel):
    """分类更新请求"""

    name: str | None = Field(None, max_length=100, description="分类名称")
    is_active: bool | None = Field(None, description="是否活跃（软删除标记）")
    keywords: list[str] | None = Field(None, description="触发关键字列表")


class CategoryHitRequest(BaseModel):
    """命中计数请求（预留扩展）"""

    trace_id: str | None = Field(None, description="调用链 ID（用于溯源）")


# ---- 路由 ----


@router.get("")
async def list_categories(
    request: Request,
    grouped: bool = True,
    force_refresh: bool = False,
):
    """获取分类列表（含 KBD/SOP 统计）

    Args:
        grouped: True=按域分组返回，False=平铺列表
        force_refresh: 强制刷新缓存

    Returns:
        grouped=True: { domains: { domain: [category, ...] } }
        grouped=False: { categories: [category, ...] }
    """
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="list_categories_request",
        grouped=grouped,
        force_refresh=force_refresh,
    )

    if grouped:
        grouped_data = await _category_service.get_grouped_by_domain(
            force_refresh=force_refresh
        )
        return {
            "domains": {
                domain: [
                    {
                        **cat.to_dict(),
                        "id": cat.code,   # 覆盖 DB 整型主键，prompt_builder 期望业务编码如 '虚拟机-003'
                        "label": cat.name,  # 兼容 conversation-service prompt_builder 的期望字段
                    }
                    for cat in cats
                ]
                for domain, cats in grouped_data.items()
            },
            "total_domains": len(grouped_data),
        }
    else:
        categories = await _category_service.get_all_active(
            force_refresh=force_refresh
        )
        return {
            "categories": [cat.to_dict() for cat in categories],
            "total": len(categories),
        }


@router.get("/stats")
async def get_stats(request: Request):
    """获取分类统计信息

    Returns:
        {
            total, active, inactive, total_hits,
            domains: { domain: { count, total_hits } },
            cache_status: { valid, age_seconds, count }
        }
    """
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(event="get_stats_request")

    stats = await _category_service.get_stats()
    return stats


@router.put("/{code}")
async def update_category(
    request: Request,
    code: str,
    body: CategoryUpdateRequest,
):
    """更新分类属性

    Args:
        code: 分类业务键（如 "虚拟机-001"）

    Returns:
        更新后的分类详情
    """
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="update_category_request",
        code=code,
        name=body.name,
        is_active=body.is_active,
    )

    category = await _category_service.update(
        code=code,
        name=body.name,
        is_active=body.is_active,
        keywords=body.keywords,
    )

    if not category:
        raise HTTPException(
            status_code=404,
            detail=f"分类 {code} 不存在",
        )

    return {
        "success": True,
        "category": category.to_dict(),
    }


@router.post("/{code}/hit")
async def increment_hit(
    request: Request,
    code: str,
    body: CategoryHitRequest | None = None,
):
    """增加分类命中计数

    Args:
        code: 分类业务键

    Returns:
        { success: bool, code: str, hit_count: int }
    """
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="increment_hit_request",
        code=code,
        trace_id=body.trace_id if body else None,
    )

    new_hit_count = await _category_service.increment_hit_count(code)

    if new_hit_count < 0:
        raise HTTPException(
            status_code=404,
            detail=f"分类 {code} 不存在",
        )

    return {
        "success": True,
        "code": code,
        "hit_count": new_hit_count,
    }


@router.post("/import")
async def import_categories(
    request: Request,
    file: UploadFile = File(..., description="YAML 分类文件"),
    dry_run: bool = Query(default=False, description="仅验证不写入"),
):
    """导入 YAML 分类数据（两阶段）

    流程：
    1. dry_run=True：仅验证 YAML 格式和字段合法性，不写入数据库
    2. dry_run=False：验证通过后实际写入（upsert）

    请求格式：
    - Content-Type: multipart/form-data
    - file: YAML 文件
    - dry_run: 表单字段（可选，默认 false）

    Returns:
        {
            success: bool,
            dry_run: bool,
            total: int,
            created: int,
            updated: int,
            skipped: int,
            errors: list[str],
            details: list[dict]
        }
    """
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    # 读取上传的文件内容
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=400,
            detail="YAML 文件内容为空",
        )

    logger.info(
        event="import_categories_request",
        dry_run=dry_run,
        filename=file.filename,
        content_size=len(content),
    )

    result = await _category_service.import_from_yaml(
        content=content,
        dry_run=dry_run,
    )

    if not result.get("success"):
        # 验证失败返回 400
        raise HTTPException(
            status_code=400,
            detail={
                "message": "导入验证失败",
                "errors": result.get("errors", []),
            },
        )

    return result
