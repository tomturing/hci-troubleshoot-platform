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
import sys

from shared.models.tool_definition import ToolDefinitionORM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.acli.classifier import risk_to_policy
from app.tools.base_tool import ToolDefinition

logger = logging.getLogger(__name__)

# 全局工具注册表（启动时由 main.py lifespan 填充）
TOOL_REGISTRY: dict[str, ToolDefinition] = {}

# 单元测试环境下自动预填充，保障不连接数据库时单元测试也能成功运行
if "pytest" in sys.modules or "unittest" in sys.modules:
    _test_tools = [
        # 只读工具（risk_level=1）
        ("get_active_alerts", "scp", 1),
        ("get_failed_tasks", "scp", 1),
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
    result = await db.execute(
        select(ToolDefinitionORM).where(ToolDefinitionORM.is_active.is_(True))
    )
    registry: dict[str, ToolDefinition] = {}
    for row in result.scalars():
        registry[row.tool_name] = ToolDefinition(
            name=row.tool_name,
            description=row.description,
            parameters=row.parameters_schema,
            risk_level=row.risk_level,
            policy=risk_to_policy(row.risk_level),
            category=row.category,  # 执行路由: scp | acli | sop
        )
    logger.info(f"已加载工具注册表：{len(registry)} 个工具")
    return registry


def get_tools_for_llm(include_sop: bool = True) -> list[dict]:
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
        for tool in TOOL_REGISTRY.values()
        if tool.policy != "block"
        and (include_sop or tool.category != "sop")
    ]
