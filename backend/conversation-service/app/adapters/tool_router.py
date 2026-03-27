"""
工具路由——根据工具定义中的 category 字段将调用路由到正确的适配器

支持的类别：
  scp   → SCPAdapter（深信服 HCI 平台 REST API）
  acli  → AcliAdapter（SSH 执行节点级 acli 命令）
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.tool_registry import TOOL_REGISTRY
from .acli_adapter import AcliAdapter
from .scp_adapter import SCPAdapter

logger = logging.getLogger(__name__)


class ToolRouter:
    """工具路由：根据 TOOL_REGISTRY 中的 category 字段分发到各适配器"""

    def __init__(self, scp: SCPAdapter, acli: AcliAdapter) -> None:
        self._adapters: dict[str, Any] = {
            "scp": scp,
            "acli": acli,
        }

    async def execute(self, tool_name: str, args: dict) -> Any:
        """
        统一工具执行入口，ReactExecutor 通过此方法调用工具。

        路由逻辑：
          1. 从 TOOL_REGISTRY 查找工具定义，获取 category
          2. 根据 category 选择对应适配器
          3. 调用适配器的 execute()
        """
        tool_def = TOOL_REGISTRY.get(tool_name)
        if not tool_def:
            logger.warning(f"ToolRouter: 工具 {tool_name!r} 不在注册表中")
            return {"error": f"未知工具: {tool_name}"}

        adapter = self._adapters.get(tool_def.category)
        if adapter is None:
            logger.warning(f"ToolRouter: 工具类别 {tool_def.category!r} 无对应适配器")
            return {"error": f"工具类别 {tool_def.category} 无对应适配器"}

        logger.debug(
            f"ToolRouter: {tool_name} → {tool_def.category} adapter, args={args}"
        )
        return await adapter.execute(tool_name, args)
