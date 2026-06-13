from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_tool_registry_manager_refresh_updates_global_registry():
    from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY, ToolRegistryManager

    row = SimpleNamespace(
        tool_name="dynamic_tool",
        description="动态工具",
        parameters_schema={"type": "object", "properties": {}, "required": []},
        risk_level=1,
        category="acli",
        usage_template="acli --formatter json vm list",
    )
    scalars = MagicMock()
    scalars.return_value = [row]
    result = MagicMock()
    result.scalars = scalars
    session = AsyncMock()
    session.execute.return_value = result
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    def session_factory():
        return session

    old_registry = dict(TOOL_REGISTRY)
    try:
        manager = ToolRegistryManager(db_session_factory=session_factory, ttl_seconds=30.0)
        registry = await manager.refresh()

        assert registry is TOOL_REGISTRY
        assert list(TOOL_REGISTRY.keys()) == ["dynamic_tool"]
        assert TOOL_REGISTRY["dynamic_tool"].usage_template == "acli --formatter json vm list"
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(old_registry)
