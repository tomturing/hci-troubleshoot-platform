"""
工具注册表——从数据库加载所有激活工具的元数据

v2.0 变更：
  - 从约 430 行硬编码 ToolDefinition 对象 → 约 50 行 DB 加载器
  - 数据库表 tool_definition 是 LLM 接口的唯一来源（SSOT）
  - 代码持有执行逻辑（classifier + executor），两者完全分离

风险等级说明：
  1 = 只读操作，自动执行
  2 = 写操作，需用户确认后执行
  3 = 高危操作，直接 block 拒绝执行
"""

import logging
import string
import sys
import time
from typing import Any

from shared.models.tool_definition import ToolDefinitionORM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.acli.classifier import risk_to_policy
from app.tools.base_tool import ToolDefinition

logger = logging.getLogger(__name__)


def verify_tool_contract(tool: ToolDefinition) -> None:
    """静态校验工具参数 Schema 与模板占位符契约是否完备"""
    if not tool.usage_template:
        return
    try:
        from app.tools.acli.executor import TemplateInterpolator

        template = TemplateInterpolator._OPTIONAL_SEGMENT_RE.sub(lambda m: m.group(1), tool.usage_template)
        formatter = string.Formatter()
        placeholders = {f for _, f, _, _ in formatter.parse(template) if f is not None}
    except Exception as exc:
        raise ValueError(f"工具 '{tool.name}' 的使用模板解析失败: {exc}") from exc

    schema_properties = tool.parameters.get("properties", {}) if tool.parameters else {}
    for p in placeholders:
        if p not in schema_properties:
            raise ValueError(
                f"工具契约损坏: {tool.name} 的命令模板中包含占位符 '{p}'，但在 Schema 参数定义中未找到对应属性。"
            )


# 全局工具注册表（启动时由 main.py lifespan 填充）
TOOL_REGISTRY: dict[str, ToolDefinition] = {}

# 单元测试环境下自动预填充，保障不连接数据库时单元测试也能成功运行
if "pytest" in sys.modules or "unittest" in sys.modules:
    _test_tools = [
        # 只读工具（risk_level=1）
        ("get_active_alerts", "scp", 1),
        ("get_failed_tasks", "acli", 1),
        ("get_vm_list", "scp", 1),
        ("get_cluster_detail", "scp", 1),
        ("acli_system_top", "acli", 1),
        ("acli_vm_list", "acli", 1),
        ("acli_vm_config", "acli", 1),
        ("acli_vm_disk_check", "acli", 1),
        ("acli_platform_node_list", "acli", 1),
        ("acli_storage_disk_list", "acli", 1),
        ("acli_network_nic_list", "acli", 1),
        ("acli_log_get", "acli", 1),
        ("acli_run", "acli", 1),
        ("get_sop_node", "sop", 1),
        ("sop_advance", "sop", 1),
        # 写操作工具（risk_level=2）
        ("acli_service_restart", "acli", 2),
        ("acli_network_nic_up", "acli", 2),
        ("acli_netdoctor", "acli", 2),
    ]
    for name, cat, risk in _test_tools:
        TOOL_REGISTRY[name] = ToolDefinition(
            name=name,
            description=f"Test tool {name}",
            parameters={},
            risk_level=risk,
            policy=risk_to_policy(risk),
            category=cat,
        )


async def load_tool_registry(db: AsyncSession) -> dict[str, ToolDefinition]:
    """
    启动时从 tool_definition 表加载所有激活工具。

    Args:
        db: 数据库会话

    Returns:
        工具名称到 ToolDefinition 的映射字典
    """
    result = await db.execute(select(ToolDefinitionORM).where(ToolDefinitionORM.is_active.is_(True)))
    registry: dict[str, ToolDefinition] = {}
    for row in result.scalars():
        tool = ToolDefinition(
            name=row.tool_name,
            description=row.description,
            parameters=row.parameters_schema,
            risk_level=row.risk_level,
            policy=risk_to_policy(row.risk_level),
            category=row.category,  # 执行路由: scp | acli | sop
            usage_template=row.usage_template,
        )
        # 静态契约校验，如有不一致立刻 Fail-Fast 阻断启动
        verify_tool_contract(tool)
        registry[row.tool_name] = tool
    logger.info(f"已加载工具注册表：{len(registry)} 个工具")
    return registry


class ToolRegistryManager:
    """工具注册表运行时管理器，支持短 TTL 热刷新。"""

    def __init__(self, *, db_session_factory: Any, ttl_seconds: float = 30.0) -> None:
        self._db_session_factory = db_session_factory
        self._ttl_seconds = ttl_seconds
        self._last_refresh_monotonic = 0.0

    async def refresh(self) -> dict[str, ToolDefinition]:
        """强制从数据库刷新 active 工具定义。"""
        async with self._db_session_factory() as session:
            loaded = await load_tool_registry(session)
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(loaded)
        self._last_refresh_monotonic = time.monotonic()
        logger.info("工具注册表已刷新：%s 个工具", len(TOOL_REGISTRY))
        return TOOL_REGISTRY

    async def refresh_if_needed(self) -> dict[str, ToolDefinition]:
        """TTL 到期后刷新，未到期直接返回当前快照。"""
        if time.monotonic() - self._last_refresh_monotonic >= self._ttl_seconds:
            return await self.refresh()
        return TOOL_REGISTRY


TOOL_REGISTRY_MANAGER: ToolRegistryManager | None = None


def set_tool_registry_manager(manager: ToolRegistryManager) -> None:
    """由应用启动时注入工具注册表管理器。"""
    global TOOL_REGISTRY_MANAGER
    TOOL_REGISTRY_MANAGER = manager


async def refresh_tool_registry_if_needed() -> dict[str, ToolDefinition]:
    """刷新工具注册表；测试或未初始化时返回当前快照。"""
    if TOOL_REGISTRY_MANAGER is None:
        return TOOL_REGISTRY
    return await TOOL_REGISTRY_MANAGER.refresh_if_needed()


def get_tools_for_llm_from_registry(
    registry: dict[str, ToolDefinition],
    include_sop: bool = True,
) -> list[dict]:
    """
    返回 OpenAI function calling 格式的工具列表（排除高危工具）

    Args:
        include_sop: 是否包含 SOP 导航工具（category="sop"）。
                     SOP 模式传 True（默认），非 SOP 模式传 False 减少 token 消耗（DC-01）。

    Returns:
        OpenAI function calling 格式的工具定义列表
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in registry.values()
        if tool.policy != "block" and (include_sop or tool.category != "sop")
    ]


def get_tools_for_llm(include_sop: bool = True) -> list[dict]:
    """兼容同步调用：基于当前内存快照返回工具列表。"""
    return get_tools_for_llm_from_registry(TOOL_REGISTRY, include_sop=include_sop)
