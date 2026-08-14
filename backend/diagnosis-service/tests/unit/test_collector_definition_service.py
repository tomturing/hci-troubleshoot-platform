"""Collector Definition（采集器定义）服务测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.auth import ActorContext
from app.services.collector_definition_service import CollectorDefinitionService


def collector(collector_id: str, *, review_status: str, is_enabled: bool) -> SimpleNamespace:
    """构造最小 Collector ORM 替身。"""

    return SimpleNamespace(
        collector_id=collector_id,
        display_name=collector_id,
        description="只读采集器",
        platform="linux",
        executor="shell",
        command_template="/usr/bin/true",
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk_level="read_only",
        timeout_seconds=30,
        max_output_mb=1,
        supported_product_versions=["*"],
        output_contract={
            "schema_id": f"{collector_id}_v1",
            "media_type": "text/plain",
            "output_path": f"commands/{collector_id}.txt",
        },
        semantic_version="1.0.0",
        review_status=review_status,
        is_enabled=is_enabled,
        approved_by="admin-a" if review_status == "approved" else None,
        approved_at=None,
        rejection_reason=None,
        lock_version=1,
    )


@pytest.mark.asyncio
async def test_list_collectors_applies_filters_and_keeps_stable_response_order():
    """列表查询必须应用筛选条件并返回当前生效修订信息。"""

    database = AsyncMock()
    query_result = MagicMock()
    database.execute.return_value = query_result
    query_result.scalars.return_value.all.return_value = [
        collector("collector.alpha", review_status="approved", is_enabled=True),
        collector("collector.beta", review_status="approved", is_enabled=True),
    ]
    service = CollectorDefinitionService(database)
    service._active_snapshot = AsyncMock(
        side_effect=[
            SimpleNamespace(revision=2, checksum="a" * 64),
            SimpleNamespace(revision=3, checksum="b" * 64),
        ]
    )
    actor = ActorContext(
        tenant_id="tenant-a",
        user_id="admin-a",
        roles=frozenset({"platform_admin"}),
    )

    result = await service.list(actor=actor, review_status="approved", is_enabled=True)

    assert [item.collector_id for item in result] == ["collector.alpha", "collector.beta"]
    assert [item.active_revision for item in result] == [2, 3]
    statement = database.execute.await_args.args[0]
    assert "collector_definition.review_status" in str(statement)
    assert "collector_definition.is_enabled" in str(statement)
    assert "ORDER BY collector_definition.collector_id" in str(statement)
