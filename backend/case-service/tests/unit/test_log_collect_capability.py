"""日志收集场景能力门禁的单元测试。

规则只有一条：只有 acli_help + node_modules 才是「节点支持什么」的事实。
能力未知时不得拦截；能力明确时必须在建任务之前拒绝不支持项。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.domain.log_collect_capability import (
    capability_rejection_message,
    supported_modules_from_probe,
    unsupported_modules,
)
from app.routes.log_collect import create_task
from app.schemas.log_collect import LogCollectTaskCreate
from fastapi import HTTPException


def make_payload(*, modules: list[str], probe: dict | None) -> LogCollectTaskCreate:
    return LogCollectTaskCreate(
        case_id="Q2026112600025",
        origin_node_ip="10.6.0.30",
        modules=modules,
        date_from=date(2026, 11, 25),
        date_to=date(2026, 11, 26),
        probe=probe,
        trace_id="lc-test-capability",
    )


def test_only_authoritative_help_is_a_capability_fact():
    """ability 事实只认 acli_help + 非空 node_modules。"""
    assert supported_modules_from_probe(
        {"catalog_source": "acli_help", "node_modules": ["host_offline", "core"]}
    ) == frozenset({"host_offline", "core"})
    # JSONB 被驱动当字符串返回时也要能解析
    assert supported_modules_from_probe(
        {"catalog_source": "acli_help", "node_modules": '["host_offline"]'}
    ) == frozenset({"host_offline"})
    # 以下都不是能力事实：不能据此删用户的选择
    assert supported_modules_from_probe(None) is None
    assert supported_modules_from_probe({}) is None
    assert supported_modules_from_probe(
        {"catalog_source": "platform_default", "node_modules": ["host_offline"]}
    ) is None
    assert supported_modules_from_probe(
        {"catalog_source": "acli_help", "node_modules": []}
    ) is None
    assert supported_modules_from_probe(
        {"catalog_source": "acli_help", "node_modules": "not-json"}
    ) is None


def test_unsupported_modules_keeps_selection_order_and_degrades_safely():
    assert unsupported_modules(["core", "host_offline"], {"host_offline"}) == ["core"]
    assert unsupported_modules(["core"], None) == []
    assert unsupported_modules(["core"], {"core"}) == []


def test_rejection_message_names_the_modules_and_next_action():
    message = capability_rejection_message(["core"], "1.0.0")
    assert "core" in message
    assert "1.0.0" in message
    assert "取消" in message and "升级" in message


@pytest.mark.asyncio
async def test_create_rejects_unsupported_module_before_creating_task():
    """节点 help 没有 core 时，平台必须在建任务前 422，且不写任何任务行。"""
    service = SimpleNamespace(
        repository=SimpleNamespace(session=AsyncMock()),
        create_task=AsyncMock(),
    )
    payload = make_payload(
        modules=["host_offline", "core"],
        probe={
            "catalog_source": "acli_help",
            "node_modules": ["host_offline", "slowdisk", "performance", "system"],
            "acli_version": "1.0.0",
        },
    )

    with patch("app.routes.log_collect.verify_case_exists", new=AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await create_task(payload, client_id="client-a", service=service)

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "LC-PLUGIN-016"
    assert "core" in exc.value.detail["detail"]
    # 拒绝发生在 create_task 之前：不能留下一条注定失败的记录
    service.create_task.assert_not_awaited()
