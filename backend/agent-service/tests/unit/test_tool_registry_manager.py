from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_tool_registry_manager_refresh_updates_global_registry(monkeypatch):
    from app.adapters.agents.htp import tool_registry
    from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY, ToolRegistryManager
    from shared.dynamic_resource.models import ResourceSnapshot

    row = SimpleNamespace(
        tool_name="dynamic_tool",
        display_name="动态工具",
        description="动态工具",
        parameters_schema={"type": "object", "properties": {}, "required": []},
        risk_level=1,
        category="acli",
        usage_template="acli --formatter json vm list",
        examples=[],
        is_active=True,
        version="1.0",
    )
    scalars = MagicMock()
    scalars.return_value = [row]
    result = MagicMock()
    result.scalars = scalars
    session = AsyncMock()
    session.execute.return_value = result
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.commit = AsyncMock()

    def session_factory():
        return session

    async def fake_ensure_published(self, **kwargs):
        return ResourceSnapshot(
            resource_type=kwargs["resource_type"],
            resource_name=kwargs["resource_name"],
            revision=4,
            version=kwargs["version"],
            status=kwargs["status"],
            content=kwargs["content"],
            contract=kwargs["contract"],
            dependencies=kwargs["dependencies"],
            checksum="tool-checksum",
        )

    monkeypatch.setattr(tool_registry.DynamicResourcePublisher, "ensure_published", fake_ensure_published)

    old_registry = dict(TOOL_REGISTRY)
    try:
        manager = ToolRegistryManager(db_session_factory=session_factory, ttl_seconds=30.0)
        registry = await manager.refresh()

        assert registry is TOOL_REGISTRY
        assert list(TOOL_REGISTRY.keys()) == ["dynamic_tool"]
        assert TOOL_REGISTRY["dynamic_tool"].usage_template == "acli --formatter json vm list"
        assert TOOL_REGISTRY["dynamic_tool"].resource_revision["revision"] == 4
        session.commit.assert_awaited_once()
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(old_registry)
