"""
工具定义管理路由 — 提供对 tool_definition 表的增删改查接口
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_definition import ToolDefinition

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/tools", tags=["tools"])

# 由 main.py 注入数据库管理器
database_manager: DatabaseManager | None = None


def set_tool_database_manager(db: DatabaseManager) -> None:
    """依赖注入"""
    global database_manager
    database_manager = db


def _make_issue(level: str, location: str, message: str, code: str) -> dict[str, str]:
    return {"level": level, "location": location, "message": message, "code": code}


def validate_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验工具定义契约，供保存前和独立校验接口复用。"""
    issues: list[dict[str, str]] = []
    tool_name = payload.get("tool_name")
    schema = payload.get("parameters_schema") or {}
    usage_template = payload.get("usage_template") or ""

    if not isinstance(schema, dict):
        issues.append(_make_issue("error", "parameters_schema", "parameters_schema 必须是 JSON Object", "SCHEMA_NOT_OBJECT"))
        schema = {}

    properties = schema.get("properties") if isinstance(schema, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []
    if properties is not None and not isinstance(properties, dict):
        issues.append(_make_issue("error", "parameters_schema.properties", "properties 必须是对象", "SCHEMA_PROPERTIES_INVALID"))
        properties = {}
    if required is not None and not isinstance(required, list):
        issues.append(_make_issue("error", "parameters_schema.required", "required 必须是数组", "SCHEMA_REQUIRED_INVALID"))
        required = []

    if tool_name == "bash_exec":
        container = (properties or {}).get("container")
        if not container:
            issues.append(
                _make_issue("error", "parameters_schema.properties.container", "bash_exec 必须定义 container 参数", "BASH_CONTAINER_MISSING")
            )
        else:
            enum_values = container.get("enum") if isinstance(container, dict) else None
            if not isinstance(enum_values, list) or not enum_values:
                issues.append(
                    _make_issue(
                        "error",
                        "parameters_schema.properties.container.enum",
                        "bash_exec container 必须通过 enum 声明允许值",
                        "BASH_CONTAINER_ENUM_MISSING",
                    )
                )
            elif "host" not in {str(item) for item in enum_values}:
                issues.append(
                    _make_issue(
                        "error",
                        "parameters_schema.properties.container.enum",
                        "bash_exec container enum 必须包含 host，作为物理机执行边界",
                        "BASH_CONTAINER_HOST_MISSING",
                    )
                )
        if "container" not in (required or []):
            issues.append(
                _make_issue("error", "parameters_schema.required", "bash_exec 必须要求 container 为必填字段", "BASH_CONTAINER_REQUIRED")
            )

    if tool_name == "acli_exec":
        for field_name in ("command", "reason"):
            if field_name not in (required or []):
                issues.append(
                    _make_issue("error", "parameters_schema.required", f"acli_exec 必须要求 {field_name} 为必填字段", "ACLI_REQUIRED_FIELD_MISSING")
                )

    if usage_template:
        import string

        try:
            placeholders = {f for _, f, _, _ in string.Formatter().parse(usage_template) if f is not None}
            for placeholder in placeholders:
                if placeholder not in (properties or {}):
                    issues.append(
                        _make_issue(
                            "error",
                            "usage_template",
                            f"usage_template 占位符 {placeholder} 在 parameters_schema.properties 中不存在",
                            "USAGE_TEMPLATE_PLACEHOLDER_MISSING",
                        )
                    )
        except Exception as exc:
            issues.append(_make_issue("error", "usage_template", f"usage_template 解析失败：{exc}", "USAGE_TEMPLATE_PARSE_FAILED"))

    status = "error" if any(issue["level"] == "error" for issue in issues) else "warning" if issues else "ok"
    return {"status": status, "validation_issues": issues}


def _raise_if_invalid_tool_payload(payload: dict[str, Any]) -> None:
    """保存工具定义前强制执行契约校验。"""
    validation = validate_tool_payload(payload)
    if validation["status"] == "error":
        raise HTTPException(status_code=400, detail=validation)


async def get_db() -> AsyncSession:
    """获取数据库会话"""
    if not database_manager:
        raise HTTPException(status_code=500, detail="数据库管理器未初始化")
    async for session in database_manager.get_session():
        yield session


@router.post("/validate", summary="校验工具定义")
async def validate_tool_definition(payload: dict[str, Any]) -> dict[str, Any]:
    """校验未保存的工具定义 payload。"""
    return validate_tool_payload(payload)


@router.post("/{tool_id}/validate", summary="校验已有工具定义")
async def validate_existing_tool(tool_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """校验数据库中的已有工具定义。"""
    stmt = select(ToolDefinition).where(ToolDefinition.id == tool_id)
    result = await db.execute(stmt)
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="工具定义不存在")
    return validate_tool_payload(
        {
            "tool_name": t.tool_name,
            "parameters_schema": t.parameters_schema,
            "usage_template": t.usage_template,
        }
    )


@router.get("", summary="获取工具定义列表")
async def list_tools(
    category: str | None = Query(None, description="按工具执行分类过滤 (scp/acli/sop)"),
    is_active: bool | None = Query(None, description="按是否启用状态过滤"),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """查询 ReAct 工具定义库列表"""
    stmt = select(ToolDefinition).order_by(ToolDefinition.category, ToolDefinition.tool_name)
    if category:
        stmt = stmt.where(ToolDefinition.category == category)
    if is_active is not None:
        stmt = stmt.where(ToolDefinition.is_active == is_active)

    result = await db.execute(stmt)
    tools = result.scalars().all()

    return [
        {
            "id": t.id,
            "tool_name": t.tool_name,
            "display_name": t.display_name,
            "category": t.category,
            "description": t.description,
            "usage_template": t.usage_template,
            "parameters_schema": t.parameters_schema,
            "examples": t.examples,
            "risk_level": t.risk_level,
            "is_active": t.is_active,
            "version": t.version,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        for t in tools
    ]


@router.get("/{tool_id}", summary="获取工具详情")
async def get_tool(tool_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """查询单个工具的完整详细定义"""
    stmt = select(ToolDefinition).where(ToolDefinition.id == tool_id)
    result = await db.execute(stmt)
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="工具定义不存在")

    return {
        "id": t.id,
        "tool_name": t.tool_name,
        "display_name": t.display_name,
        "category": t.category,
        "description": t.description,
        "usage_template": t.usage_template,
        "parameters_schema": t.parameters_schema,
        "examples": t.examples,
        "risk_level": t.risk_level,
        "is_active": t.is_active,
        "version": t.version,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.post("", summary="创建新工具定义", status_code=210)
async def create_tool(payload: dict[str, Any], db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """创建一条新的工具定义记录。"""
    tool_name = payload.get("tool_name")
    if not tool_name:
        raise HTTPException(status_code=400, detail="工具标识名 (tool_name) 必填")
    _raise_if_invalid_tool_payload(payload)

    # 检查是否重名
    stmt = select(ToolDefinition).where(ToolDefinition.tool_name == tool_name)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"工具名 '{tool_name}' 已存在")

    t = ToolDefinition(
        tool_name=tool_name,
        display_name=payload.get("display_name") or tool_name,
        category=payload.get("category") or "acli",
        description=payload.get("description") or "",
        usage_template=payload.get("usage_template"),
        parameters_schema=payload.get("parameters_schema") or {},
        examples=payload.get("examples") or [],
        risk_level=int(payload.get("risk_level", 1)),
        is_active=bool(payload.get("is_active", True)),
        version=payload.get("version") or "1.0",
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)

    logger.info(event="tool_created", tool_name=t.tool_name, tool_id=t.id, message="创建了新的工具定义")
    return {"status": "success", "id": t.id}


@router.put("/{tool_id}", summary="修改工具定义")
async def update_tool(tool_id: int, payload: dict[str, Any], db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """更新一条已有的工具定义记录"""
    stmt = select(ToolDefinition).where(ToolDefinition.id == tool_id)
    result = await db.execute(stmt)
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="工具定义不存在")

    merged_payload = {
        "tool_name": payload.get("tool_name", t.tool_name),
        "parameters_schema": payload.get("parameters_schema", t.parameters_schema),
        "usage_template": payload.get("usage_template", t.usage_template),
    }
    _raise_if_invalid_tool_payload(merged_payload)

    tool_name = payload.get("tool_name")
    if tool_name and tool_name != t.tool_name:
        # 重名校验
        check_stmt = select(ToolDefinition).where(ToolDefinition.tool_name == tool_name)
        check_res = await db.execute(check_stmt)
        if check_res.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"工具名 '{tool_name}' 已存在")
        t.tool_name = tool_name

    if "display_name" in payload:
        t.display_name = payload["display_name"]
    if "category" in payload:
        t.category = payload["category"]
    if "description" in payload:
        t.description = payload["description"]
    if "usage_template" in payload:
        t.usage_template = payload["usage_template"]
    if "parameters_schema" in payload:
        t.parameters_schema = payload["parameters_schema"]
    if "examples" in payload:
        t.examples = payload["examples"]
    if "risk_level" in payload:
        t.risk_level = int(payload["risk_level"])
    if "is_active" in payload:
        t.is_active = bool(payload["is_active"])
    if "version" in payload:
        t.version = payload["version"]

    await db.commit()
    logger.info(event="tool_updated", tool_name=t.tool_name, tool_id=t.id, message="更新了工具定义")
    return {"status": "success"}


@router.delete("/{tool_id}", summary="删除工具定义")
async def delete_tool(tool_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """删除一条已有的工具定义记录"""
    stmt = delete(ToolDefinition).where(ToolDefinition.id == tool_id)
    result = await db.execute(stmt)
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="工具定义不存在")

    logger.info(event="tool_deleted", tool_id=tool_id, message="删除了工具定义")
    return {"status": "success"}
