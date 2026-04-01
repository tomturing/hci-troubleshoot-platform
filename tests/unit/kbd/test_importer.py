"""
tests/unit/kbd/test_importer.py — kbd/importer.py 单元测试

覆盖：
  - import_entry：幂等逻辑（draft 更新、非 draft 跳过、新建）
  - import_entry：converter 返回 None 时输出 "error"
  - import_batch：批量统计计数
  - get_pending_review_cases：构造期望的 SQL 查询路径（mock asyncpg）
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_scripts_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
)
if _scripts_root not in sys.path:
    sys.path.insert(0, _scripts_root)


def _make_pool(
    fetchrow_return=None,
    execute_return=None,
    fetch_return=None,
):
    """构造一个 asyncpg Pool mock"""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.execute = AsyncMock(return_value=execute_return)
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    return pool


# ─── import_entry ────────────────────────────────────────────────────────────

class TestImportEntry:
    """测试 import_entry 幂等与状态逻辑"""

    @pytest.mark.asyncio
    async def test_creates_new_entry(self, tmp_path, minimal_rows):
        """新案例应调用 INSERT 并返回 'created'"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(minimal_rows), encoding="utf-8"
        )
        pool = _make_pool(fetchrow_return=None)  # 无已有记录

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.importer import import_entry
            result = await import_entry("36156", pool)

        assert result == "created"
        pool.execute.assert_awaited_once()
        sql = pool.execute.call_args[0][0]
        assert "INSERT INTO kbd_entry" in sql

    @pytest.mark.asyncio
    async def test_updates_existing_draft(self, tmp_path, minimal_rows):
        """已有 draft 记录时应调用 UPDATE 并返回 'updated'"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(minimal_rows), encoding="utf-8"
        )
        existing = {"id": 1, "status": "draft"}
        pool = _make_pool(fetchrow_return=existing)

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.importer import import_entry
            result = await import_entry("36156", pool)

        assert result == "updated"
        sql = pool.execute.call_args[0][0]
        assert "UPDATE kbd_entry" in sql

    @pytest.mark.asyncio
    async def test_skips_non_draft_without_force(self, tmp_path):
        """非 draft 状态（published）默认跳过，不调用 converter"""
        existing = {"id": 2, "status": "published"}
        pool = _make_pool(fetchrow_return=existing)

        with patch("kbd.converter.convert_case_with_meta") as mock_conv:
            from kbd.importer import import_entry
            result = await import_entry("36156", pool)

        assert result == "skipped"
        mock_conv.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_draft_overrides_published(self, tmp_path, minimal_rows):
        """force_draft=True 时即使已 published 也应更新"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(minimal_rows), encoding="utf-8"
        )
        existing = {"id": 2, "status": "published"}
        pool = _make_pool(fetchrow_return=existing)

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.importer import import_entry
            result = await import_entry("36156", pool, force_draft=True)

        assert result == "updated"

    @pytest.mark.asyncio
    async def test_converter_none_returns_error(self, tmp_path, missing_mandatory_rows):
        """converter 返回 None（必填缺失）时应返回 'error'"""
        case_dir = tmp_path / "missing"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(missing_mandatory_rows), encoding="utf-8"
        )
        pool = _make_pool(fetchrow_return=None)

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.importer import import_entry
            result = await import_entry("missing", pool)

        assert result == "error"
        pool.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_insert_sql_has_required_fields(self, tmp_path, minimal_rows):
        """INSERT SQL 必须包含所有必要列"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(minimal_rows), encoding="utf-8"
        )
        pool = _make_pool(fetchrow_return=None)

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.importer import import_entry
            await import_entry("36156", pool)

        sql = pool.execute.call_args[0][0]
        for col in ["support_id", "support_url", "title", "content_md", "metadata"]:
            assert col in sql, f"INSERT SQL 缺少列: {col}"


# ─── import_batch ─────────────────────────────────────────────────────────────

class TestImportBatch:
    """测试 import_batch 批量统计"""

    @pytest.mark.asyncio
    async def test_counts_correctly(self, tmp_path, minimal_rows):
        """3 个 ID：2 created + 1 error"""
        # 准备两个有效案例
        for sid in ["1", "2"]:
            case_dir = tmp_path / sid
            case_dir.mkdir(parents=True)
            r = dict(minimal_rows)
            r["id"] = int(sid)
            (case_dir / "raw.json").write_text(json.dumps(r), encoding="utf-8")

        # 第三个不存在（converter 返回 None → error）
        pool = _make_pool(fetchrow_return=None)

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.importer import import_batch
            stats = await import_batch(["1", "2", "nonexist"], pool)

        assert stats["created"] == 2
        assert stats["error"] == 1
        assert stats.get("skipped", 0) == 0

    @pytest.mark.asyncio
    async def test_empty_list_returns_zero_stats(self, tmp_path):
        pool = _make_pool()
        with patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"):
            from kbd.importer import import_batch
            stats = await import_batch([], pool)

        assert stats == {"created": 0, "updated": 0, "skipped": 0, "error": 0}


# ─── get_pending_review_cases ─────────────────────────────────────────────────

class TestGetPendingReviewCases:
    """测试等待审核案例查询"""

    @pytest.mark.asyncio
    async def test_limit_passed_to_query(self):
        pool = _make_pool(fetch_return=[])
        from kbd.importer import get_pending_review_cases
        result = await get_pending_review_cases(pool, limit=10)
        # 确认调用了 fetch 并传入了 limit 参数
        pool.fetch.assert_awaited_once()
        args = pool.fetch.call_args[0]
        assert 10 in args  # limit 以位置参数传入

    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(self):
        fake_row = MagicMock()
        fake_row.keys = MagicMock(return_value=["support_id", "title"])
        fake_row.__iter__ = MagicMock(return_value=iter({"support_id": "36156", "title": "测试"}.items()))
        # asyncpg Record 是类 dict，直接 dict(r) 可用
        fake_row = {"support_id": "36156", "title": "测试"}
        pool = _make_pool(fetch_return=[fake_row])

        from kbd.importer import get_pending_review_cases
        # asyncpg 的 fetch 返回 asyncpg.Record 列表，dict(r) 会调用 __iter__
        # 这里 mock 直接返回 dict，importer 中 dict(r) 仍可工作
        result = await get_pending_review_cases(pool, limit=5)
        assert isinstance(result, list)
