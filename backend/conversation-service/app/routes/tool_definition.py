"""
工具定义管理路由 — 提供对 tool_definition 表的增删改查接口
"""

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from shared.database.postgres import DatabaseManager
from shared.dynamic_resource.adapters import tool_resource_payload
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.observability.logger import get_logger
from sqlalchemy import case, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_definition import ToolDefinition

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/tools", tags=["tools"])

# 工具管理页中 qkv/qfk 两类工具的展示顺序（其余分类保持 category, tool_name 不变）。
# 顺序依据：前端信号(QKV 生产者) → 后端信号(QFK 消费者) 的业务阅读顺序。
QKV_QFK_DISPLAY_ORDER = [
    "qkv_alert",
    "qkv_task",
    "qkv_dialog",
    "qfk_log",
    "qfk_service",
    "qfk_system",
    "qfk_vm",
    "qfk_network",
    "qfk_storage",
    "qfk_hardware",
    "qfk_platform",
]
_QKV_QFK_RANK = {name: i for i, name in enumerate(QKV_QFK_DISPLAY_ORDER)}

# 工具命名规范（与 DB CHECK 约束 chk_tool_definition_tool_name_format、前端表单校验保持一致）：
# 首字符小写字母，仅允许小写字母/数字/下划线，长度 1–64，禁止点号(.)与大写字母。
# 依据：tool_name 首要身份是 LLM function-calling 的 name 字段，须满足 OpenAI/Anthropic/Gemini 字符集约束。
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MUSTACHE_CONDITION_PATTERN = re.compile(r"\{\{#if\s+([A-Za-z0-9_.]+)\}\}|\{\{/if\}\}")
MUSTACHE_FIELD_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

# 由 main.py 注入数据库管理器
database_manager: DatabaseManager | None = None


def set_tool_database_manager(db: DatabaseManager) -> None:
    """依赖注入"""
    global database_manager
    database_manager = db


async def reconcile_tool_resource_revisions(db_manager: DatabaseManager) -> int:
    """将首次部署的 Tool 事实表幂等对账为不可变修订。"""

    published_count = 0
    async for db in db_manager.get_session():
        tools = (await db.execute(select(ToolDefinition).order_by(ToolDefinition.tool_name))).scalars().all()
        publisher = DynamicResourcePublisher(db)
        for tool in tools:
            await publisher.ensure_published(
                **tool_resource_payload(tool),
                trace_id="tool-registry-startup-reconcile",
            )
            published_count += 1
    return published_count


def _make_issue(level: str, location: str, message: str, code: str) -> dict[str, str]:
    return {"level": level, "location": location, "message": message, "code": code}


def _schema_has_path(schema: dict[str, Any], field_path: str) -> bool:
    """判断 Tool 参数模式是否声明了占位符路径。"""

    current = schema
    for segment in field_path.split("."):
        properties = current.get("properties") if isinstance(current, dict) else None
        if not isinstance(properties, dict) or segment not in properties:
            return False
        current = properties[segment]
    return True


def _template_placeholder_paths(template: str) -> set[str]:
    """同时解析 Mustache（双花括号）与 Formatter（单花括号）占位符。"""

    import string

    placeholders = set(MUSTACHE_FIELD_PATTERN.findall(template))
    placeholders.update(match.group(1) for match in MUSTACHE_CONDITION_PATTERN.finditer(template) if match.group(1))
    normalized = MUSTACHE_CONDITION_PATTERN.sub("", template)
    normalized = MUSTACHE_FIELD_PATTERN.sub("", normalized)
    placeholders.update(
        field_name
        for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(normalized)
        if field_name
    )
    return placeholders


def validate_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验工具定义契约，供保存前和独立校验接口复用。"""
    issues: list[dict[str, str]] = []
    tool_name = payload.get("tool_name")
    category = payload.get("category")
    schema = payload.get("parameters_schema") or {}
    usage_template = payload.get("usage_template") or ""
    risk_level = int(payload.get("risk_level", 1))

    # 命名规范校验（治本：固化 snake_case 规则，禁止点号/大写，防止约定漂移）
    if tool_name is not None and (not isinstance(tool_name, str) or not TOOL_NAME_PATTERN.fullmatch(tool_name)):
        issues.append(
            _make_issue(
                "error",
                "tool_name",
                "tool_name 必须以小写字母开头，仅含小写字母、数字、下划线，长度 1-64，且禁止点号(.)与大写字母",
                "TOOL_NAME_INVALID_FORMAT",
            )
        )

    if category in {"qkv", "qfk"} and risk_level != 1:
        issues.append(
            _make_issue(
                "error",
                "risk_level",
                "QKV/QFK 工具会被在线与离线采集复用，必须保持只读风险等级 1",
                "SIGNAL_TOOL_MUST_BE_READ_ONLY",
            )
        )

    if not isinstance(schema, dict):
        issues.append(
            _make_issue("error", "parameters_schema", "parameters_schema 必须是 JSON Object", "SCHEMA_NOT_OBJECT")
        )
        schema = {}

    properties = schema.get("properties") if isinstance(schema, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []
    if properties is not None and not isinstance(properties, dict):
        issues.append(
            _make_issue("error", "parameters_schema.properties", "properties 必须是对象", "SCHEMA_PROPERTIES_INVALID")
        )
        properties = {}
    if required is not None and not isinstance(required, list):
        issues.append(
            _make_issue("error", "parameters_schema.required", "required 必须是数组", "SCHEMA_REQUIRED_INVALID")
        )
        required = []

    if tool_name == "bash_exec":
        container = (properties or {}).get("container")
        if not container:
            issues.append(
                _make_issue(
                    "error",
                    "parameters_schema.properties.container",
                    "bash_exec 必须定义 container 参数",
                    "BASH_CONTAINER_MISSING",
                )
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
                _make_issue(
                    "error",
                    "parameters_schema.required",
                    "bash_exec 必须要求 container 为必填字段",
                    "BASH_CONTAINER_REQUIRED",
                )
            )

    if tool_name == "acli_exec":
        for field_name in ("command", "reason"):
            if field_name not in (required or []):
                issues.append(
                    _make_issue(
                        "error",
                        "parameters_schema.required",
                        f"acli_exec 必须要求 {field_name} 为必填字段",
                        "ACLI_REQUIRED_FIELD_MISSING",
                    )
                )

    if usage_template:
        try:
            placeholders = _template_placeholder_paths(usage_template)
            for placeholder in placeholders:
                if not _schema_has_path(schema, placeholder):
                    issues.append(
                        _make_issue(
                            "error",
                            "usage_template",
                            f"usage_template 占位符 {placeholder} 在 parameters_schema.properties 中不存在",
                            "USAGE_TEMPLATE_PLACEHOLDER_MISSING",
                        )
                    )
        except Exception as exc:
            issues.append(
                _make_issue("error", "usage_template", f"usage_template 解析失败：{exc}", "USAGE_TEMPLATE_PARSE_FAILED")
            )

    status = "error" if any(issue["level"] == "error" for issue in issues) else "warning" if issues else "ok"
    return {"status": status, "validation_issues": issues}


def _raise_if_invalid_tool_payload(payload: dict[str, Any]) -> None:
    """保存工具定义前强制执行契约校验。"""
    validation = validate_tool_payload(payload)
    if validation["status"] == "error":
        raise HTTPException(status_code=400, detail=validation)


async def _publish_tool_resource(db: AsyncSession, tool: ToolDefinition) -> dict[str, Any]:
    """将工具定义同步为动态资源 revision。"""
    payload = tool_resource_payload(tool)
    snapshot = await DynamicResourcePublisher(db).ensure_published(**payload)
    return {
        "resource_type": snapshot.resource_type,
        "resource_name": snapshot.resource_name,
        "revision": snapshot.revision,
        "checksum": snapshot.checksum,
    }


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
            "risk_level": t.risk_level,
        }
    )


@router.get("", summary="获取工具定义列表")
async def list_tools(
    category: str | None = Query(None, description="按工具执行分类过滤 (scp/acli/sop/qkv/qfk)"),
    is_active: bool | None = Query(None, description="按是否启用状态过滤"),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """查询 ReAct 工具定义库列表"""
    # 展示顺序：qkv/qfk 两类按 QKV_QFK_DISPLAY_ORDER 固定顺序置顶，其余分类保持 category, tool_name 不变。
    _is_qkv_qfk = ToolDefinition.category.in_(["qkv", "qfk"])
    _group_case = case((_is_qkv_qfk, 0), else_=1)
    _rank_case = case(
        *[(ToolDefinition.tool_name == name, rank) for name, rank in _QKV_QFK_RANK.items()],
        else_=9999,
    )
    _cat_case = case((_is_qkv_qfk, literal("")), else_=ToolDefinition.category)
    _name_case = case((_is_qkv_qfk, literal("")), else_=ToolDefinition.tool_name)
    stmt = select(ToolDefinition).order_by(_group_case, _rank_case, _cat_case, _name_case)
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
    await db.flush()
    await db.refresh(t)
    resource_revision = await _publish_tool_resource(db, t)
    await db.commit()

    logger.info(
        event="tool_created",
        tool_name=t.tool_name,
        tool_id=t.id,
        resource_revision=resource_revision,
        message="创建了新的工具定义",
    )
    return {"status": "success", "id": t.id, "resource_revision": resource_revision}


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
        "category": payload.get("category", t.category),
        "parameters_schema": payload.get("parameters_schema", t.parameters_schema),
        "usage_template": payload.get("usage_template", t.usage_template),
        "risk_level": payload.get("risk_level", t.risk_level),
    }
    _raise_if_invalid_tool_payload(merged_payload)

    tool_name = payload.get("tool_name")
    if tool_name and tool_name != t.tool_name:
        raise HTTPException(
            status_code=409,
            detail="tool_name 是 Tool 不可变身份，不能重命名；请新建工具并停用旧工具",
        )
    category = payload.get("category")
    if category and category != t.category:
        raise HTTPException(
            status_code=409,
            detail="category 决定 Tool 执行与同步边界，不能直接迁移；请新建工具并停用旧工具",
        )

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

    await db.flush()
    await db.refresh(t)
    resource_revision = await _publish_tool_resource(db, t)
    await db.commit()
    logger.info(
        event="tool_updated",
        tool_name=t.tool_name,
        tool_id=t.id,
        resource_revision=resource_revision,
        message="更新了工具定义",
    )
    return {"status": "success", "resource_revision": resource_revision}


@router.delete("/{tool_id}", summary="停用工具定义（保留历史修订）")
async def delete_tool(tool_id: int, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """停用工具并发布 disabled 修订，保留 KBD/Collector 依赖审计链。"""

    result = await db.execute(select(ToolDefinition).where(ToolDefinition.id == tool_id).with_for_update())
    tool = result.scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="工具定义不存在")
    tool.is_active = False
    await db.flush()
    resource_revision = await _publish_tool_resource(db, tool)
    await db.commit()

    logger.info(
        event="tool_disabled",
        tool_name=tool.tool_name,
        tool_id=tool_id,
        resource_revision=resource_revision,
        message="停用了工具定义并保留不可变修订",
    )
    return {"status": "success", "resource_revision": resource_revision}
