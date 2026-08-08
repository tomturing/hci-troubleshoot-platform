import json
from unittest.mock import AsyncMock

import pytest
from kbd.pipeline import _db_failed_vision_ids, _db_vision_status, _vision_item_status


def test_legacy_nonempty_desc_remains_done():
    assert _vision_item_status({"desc": "TYPE: 任务截图"}) == "done"


def test_empty_desc_is_failed_even_if_metadata_exists():
    assert _vision_item_status({"desc": "", "evidence": {"quality": {"status": "success"}}}) == "failed"


def test_partial_or_low_quality_evidence_requires_review():
    for status in ("partial", "low_quality", "needs_review"):
        item = {"desc": "legacy view", "evidence": {"quality": {"status": status}}}
        assert _vision_item_status(item) == "needs_review"


def test_explicit_needs_review_wins_over_success_status():
    item = {
        "desc": "legacy view",
        "evidence": {"quality": {"status": "success", "needs_review": True}},
    }
    assert _vision_item_status(item) == "needs_review"


def test_success_evidence_is_done():
    item = {
        "desc": "legacy view",
        "evidence": {"quality": {"status": "success", "needs_review": False}},
    }
    assert _vision_item_status(item) == "done"


@pytest.mark.asyncio
async def test_db_vision_status_decodes_asyncpg_jsonb_string():
    """asyncpg 默认返回 JSON 字符串，不能把字符串字符误判为 legacy 图片项。"""
    images = [
        {
            "seq": seq,
            "desc": f"TYPE: 终端截图\nDESCRIPTION: image {seq}",
            "evidence": {"quality": {"status": "success", "needs_review": False}},
        }
        for seq in range(4)
    ]
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "kbd_id": 127,
        "images_json": json.dumps(images, ensure_ascii=False),
        "img_count": 4,
    }

    assert await _db_vision_status(pool, "23821") == "done"


@pytest.mark.asyncio
async def test_db_vision_status_rejects_invalid_jsonb_string():
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "kbd_id": 127,
        "images_json": "not-json",
        "img_count": 4,
    }

    assert await _db_vision_status(pool, "23821") == "failed"


@pytest.mark.asyncio
async def test_failed_only_does_not_retry_success_that_only_needs_human_review():
    pool = AsyncMock()
    pool.fetch.return_value = [{"support_id": "retryable"}]

    result = await _db_failed_vision_ids(["retryable", "manual-review"], pool)

    assert result == ["retryable"]
    sql = pool.fetch.await_args.args[0]
    assert "NOT (img ? 'evidence')" in sql
    assert "quality'->>'needs_review'" not in sql
