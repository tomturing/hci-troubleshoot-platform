"""Pipeline 分类只读取 KBD 主键，生成与 revision 落库由 kb-service 原子完成。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kbd import classifier


@pytest.mark.asyncio
async def test_pipeline_classification_calls_server_persistence_and_never_updates_db_directly():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"id": 42})
    pool.execute = AsyncMock(side_effect=AssertionError("Pipeline 不得直接写分类结果"))
    api_result = {
        "category_id": "vm-001",
        "confidence": 0.91,
        "reason": "命中虚拟机启动失败",
        "needs_review": False,
        "proposal_revision_id": 17,
    }
    client = MagicMock()

    with patch.object(classifier, "_call_classify_api", AsyncMock(return_value=api_result)) as call:
        result = await classifier.classify_case("27123", pool, client)

    call.assert_awaited_once_with(42, client)
    pool.execute.assert_not_awaited()
    assert result == {
        "category_id": "vm-001",
        "confidence": 0.91,
        "reason": "命中虚拟机启动失败",
        "status": "done",
        "needs_review": False,
    }


@pytest.mark.asyncio
async def test_existing_classification_is_counted_as_done_without_api_call():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"id": 42, "ai_category_id": "vm-001"})
    client = MagicMock()

    with patch.object(classifier, "_call_classify_api", AsyncMock()) as call:
        result = await classifier.classify_case("27123", pool, client)

    call.assert_not_awaited()
    assert result["status"] == "done"
    assert result["category_id"] == "vm-001"
    assert result["already_classified"] is True
