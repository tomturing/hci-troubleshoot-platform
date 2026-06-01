"""
KB Service — 分类管理路由

REST API：
- GET  /api/kb/categories           — 获取分类列表（按域分组）
- GET  /api/kb/categories/stats     — 获取统计信息
- PUT  /api/kb/categories/{code}    — 更新分类属性
- POST /api/kb/categories/{code}/hit — 增加 hit_count
- POST /api/kb/categories/import    — 导入 YAML（两阶段）
- GET  /api/kb/categories/export    — 导出 YAML

鉴权：
- 使用 INTERNAL_API_TOKEN（内部服务调用）
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from shared.observability.logger import get_logger

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


class CategoryCreateRequest(BaseModel):
    """分类创建请求"""

    name: str = Field(..., max_length=100, description="分类名称")
    domain: str = Field(..., max_length=50, description="一级技术域")
    parent_id: int | None = Field(None, description="父分类 ID")
    code: str | None = Field(None, max_length=32, description="业务编码")
    keywords: list[str] | None = Field(None, description="触发关键字列表")


class CategoryParentUpdateRequest(BaseModel):
    """分类父节点及层级更新请求"""

    parent_id: int | None = Field(..., description="新父分类 ID，传入 None 表示作为根节点")


# ---- 路由 ----


@router.get("")
async def list_categories(
    request: Request,
    grouped: bool = True,
    force_refresh: bool = False,
    include_inactive: bool = False,
    leaf_only: bool = False,
):
    """获取分类列表（含 KBD/SOP 统计）

    Args:
        grouped: True=按域分组返回，False=平铺列表
        force_refresh: 强制刷新缓存（仅当 leaf_only=False 时有效）
        include_inactive: 是否包含禁用的分类
        leaf_only: True=仅返回叶子节点（无子分类的节点），用于 S0 意图识别

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
        include_inactive=include_inactive,
        leaf_only=leaf_only,
    )

    if grouped:
        grouped_data = await _category_service.get_grouped_by_domain(
            force_refresh=force_refresh,
            include_inactive=include_inactive,
            leaf_only=leaf_only,
        )
        return {
            "domains": {
                domain: [
                    {
                        **cat.to_dict(),
                        "id": cat.code,   # 覆盖 DB 整型主键，prompt_builder 期望业务编码如 '虚拟机-003'
                        "label": cat.name,  # 兼容 conversation-service prompt_builder 的期望字段
                        "id_in_db": cat.id,  # 保留 DB 整型主键供编辑器 parent_id 关联
                    }
                    for cat in cats
                ]
                for domain, cats in grouped_data.items()
            },
            "total_domains": len(grouped_data),
        }
    else:
        if include_inactive:
            categories = await _category_service.get_all(
                force_refresh=force_refresh
            )
        else:
            categories = await _category_service.get_all_active(
                force_refresh=force_refresh
            )
        # leaf_only 过滤（非 grouped 模式）
        if leaf_only:
            # 叶子节点过滤：无子分类的节点
            # 需要在服务层处理，这里简单过滤（效率较低，建议用 grouped=True）
            all_ids = {c.id for c in categories}
            parent_ids = {c.parent_id for c in categories if c.parent_id}
            leaf_ids = all_ids - parent_ids
            categories = [c for c in categories if c.id in leaf_ids]
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


@router.get("/export")
async def export_categories(request: Request):
    """导出分类数据为 YAML 文件

    Returns:
        StreamingResponse: YAML 文件流，文件名格式为 category_baseline_YYYY-MM-DD.yaml
    """
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(event="export_categories_request")

    # 获取所有分类（包含禁用的）
    categories = await _category_service.get_all()

    # 生成 YAML 内容
    yaml_content = _generate_export_yaml(categories)

    # 返回文件流
    filename = f"category_baseline_{datetime.now(UTC).strftime('%Y-%m-%d')}.yaml"
    return StreamingResponse(
        io.BytesIO(yaml_content.encode("utf-8")),
        media_type="application/yaml",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _generate_export_yaml(categories: list) -> str:
    """生成导出 YAML 内容

    Args:
        categories: KbCategory 列表

    Returns:
        YAML 字符串
    """
    import yaml  # noqa: PLC0415

    # 构建头部注释
    header_lines = [
        "# HCI 云平台故障分类基准",
        "# 来源: kb_category 表导出",
        f"# 导出日期: {datetime.now(UTC).strftime('%Y-%m-%d')}",
        "# 用途: 分类管理导出备份",
        "",
        "version: \"1.0\"",
        "source: kb_category_export",
        f"generated: \"{datetime.now(UTC).strftime('%Y-%m-%d')}\"",
        f"total: {len(categories)}",
        "",
        "categories:",
    ]

    # 构建分类列表
    category_items = []
    for cat in categories:
        if not cat.code:
            # 无业务编码的分类跳过（通常是 L1 域节点）
            continue
        item = {
            "id": cat.code,
            "domain": cat.domain or "未分类",
            "label": cat.name,
            "path": cat.path_labels or [cat.domain or "未分类", cat.name],
        }
        category_items.append(item)

    # 使用 yaml.dump 生成 YAML 内容（不包含 categories 键，因为我们已经手动添加了）
    yaml_body = yaml.dump(category_items, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # 组合完整 YAML
    full_yaml = "\n".join(header_lines) + "\n" + yaml_body
    return full_yaml


@router.post("")
async def create_category(
    request: Request,
    body: CategoryCreateRequest,
):
    """新增单个分类节点"""
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="create_category_route",
        name=body.name,
        domain=body.domain,
        parent_id=body.parent_id,
        code=body.code,
    )

    try:
        category = await _category_service.create(
            name=body.name,
            domain=body.domain,
            parent_id=body.parent_id,
            code=body.code,
            keywords=body.keywords,
        )
        return {
            "success": True,
            "category": category.to_dict(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(event="create_category_route_failed", error=str(e))
        raise HTTPException(status_code=500, detail="创建分类失败，请重试")


@router.delete("/{code}")
async def delete_category(
    request: Request,
    code: str,
):
    """删除分类节点"""
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(event="delete_category_route", code=code)

    try:
        success = await _category_service.delete(code)
        return {
            "success": success,
            "code": code,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(event="delete_category_route_failed", error=str(e))
        raise HTTPException(status_code=500, detail="删除分类失败，请重试")


@router.put("/{code}/parent")
async def update_category_parent(
    request: Request,
    code: str,
    body: CategoryParentUpdateRequest,
):
    """更新分类父子关系及层级（用于拖拽重组）"""
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="update_category_parent_route",
        code=code,
        new_parent_id=body.parent_id,
    )

    try:
        category = await _category_service.update_parent_recursive(
            code=code,
            new_parent_id=body.parent_id,
        )
        if not category:
            raise HTTPException(status_code=404, detail=f"分类 {code} 不存在")
        return {
            "success": True,
            "category": category.to_dict(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(event="update_category_parent_route_failed", error=str(e))
        raise HTTPException(status_code=500, detail="级联更新分类层次失败，请重试")
