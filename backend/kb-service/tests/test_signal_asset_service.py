"""
backend/kb-service/tests/test_signal_asset_service.py
单元测试验证 SignalAssetService 的模板缓存、最佳实践 Few-Shot 过滤及非阻塞异常持久化。
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.signal_asset_service import _CACHE, SignalAssetService


@pytest.fixture(autouse=True)
def clear_cache():
    _CACHE.clear()
    yield
    _CACHE.clear()


@pytest.mark.asyncio
async def test_get_all_templates_caching():
    """验证 get_all_templates 首次查 DB，第二次命中内存缓存"""
    mock_session = AsyncMock()
    mock_template = MagicMock()
    mock_template.id = 1
    mock_template.tool_name = "qkv_task"
    mock_template.category = "frontend"
    mock_template.description = "任务查询"
    mock_template.acquire_schema = {"type": "object"}
    mock_template.allowed_matcher_types = []
    mock_template.variable_protocol = {"produces": ["HOST"]}
    mock_template.anti_patterns = ["no match"]

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_template]
    mock_session.execute.return_value = mock_result

    # 首次调用
    t1 = await SignalAssetService.get_all_templates(mock_session)
    assert "qkv_task" in t1
    assert len(t1) == 13
    assert t1["qkv_task"]["contract_source"] == "shared.schemas.acquirer_args"
    assert t1["qkv_task"]["acquire_schema"]["required"] == ["keyword"]
    assert "qkv_case_context" not in t1
    assert mock_session.execute.call_count == 1

    # 第二次调用命中缓存
    t2 = await SignalAssetService.get_all_templates(mock_session)
    assert t2 == t1
    assert mock_session.execute.call_count == 1  # 无额外 DB 查询


@pytest.mark.asyncio
async def test_get_best_practices_by_tool_caching():
    """验证 get_best_practices_by_tool 缓存与结构化返回"""
    mock_session = AsyncMock()
    mock_bp = MagicMock()
    mock_bp.id = 101
    mock_bp.tool_name = "qfk_log"
    mock_bp.pattern_category = "日志排查"
    mock_bp.support_id = "18906"
    mock_bp.source_revision = 8
    mock_bp.source_checksum = "a" * 64
    mock_bp.signal_id = "sig_1"
    mock_bp.raw_evidence = "检查 log"
    mock_bp.signal_json = {"id": "sig_1"}
    mock_bp.design_notes = "设计要点"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_bp]
    mock_session.execute.return_value = mock_result

    res1 = await SignalAssetService.get_best_practices_by_tool(mock_session, "qfk_log", limit=3)
    assert len(res1) == 1
    assert res1[0]["support_id"] == "18906"
    assert res1[0]["source_revision"] == 8
    assert res1[0]["source_checksum"] == "a" * 64
    assert res1[0]["signal_id"] == "sig_1"
    assert mock_session.execute.call_count == 1

    res2 = await SignalAssetService.get_best_practices_by_tool(mock_session, "qfk_log", limit=3)
    assert res2 == res1
    assert mock_session.execute.call_count == 1


@pytest.mark.asyncio
async def test_published_kbd_creates_revisioned_best_practices_and_skips_context():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    template_result = MagicMock()
    template_result.all.return_value = [(1, "qkv_task"), (2, "qfk_log")]
    mock_session.execute.side_effect = [MagicMock(), template_result]
    mock_session.scalar.return_value = None
    kbd = SimpleNamespace(
        id=42,
        support_id="KBD-42",
        title="任务失败样例",
        signals_json={
            "signals": [
                {
                    "id": "task",
                    "acquire": {"tool": "qkv_task", "args": {"keyword": "启动失败"}},
                    "orchestrate": {"produces": [{"name": "VM", "path": "vm"}]},
                },
                {
                    "id": "context",
                    "acquire": {"tool": "qkv_case_context", "args": {}},
                    "orchestrate": {"produces": []},
                },
            ]
        },
    )

    count = await SignalAssetService.sync_published_kbd_best_practices(
        mock_session,
        kbd=kbd,
        source_revision=7,
        source_checksum="a" * 64,
    )

    assert count == 1
    best_practice = mock_session.add.call_args.args[0]
    assert best_practice.signal_id == "task"
    assert best_practice.source_revision == 7
    assert best_practice.source_checksum == "a" * 64
    assert best_practice.is_active is True

@pytest.mark.asyncio
async def test_record_failure_with_db_manager_independent_commit():
    """验证 record_failure 优先使用 db_manager 开辟独立事务提交"""
    mock_db = MagicMock()
    mock_independent_session = MagicMock()
    mock_independent_session.commit = AsyncMock()

    @asynccontextmanager
    async def session_factory():
        yield mock_independent_session

    mock_db.async_session_factory = session_factory

    ret_id = await SignalAssetService.record_failure(
        session=None,
        db_manager=mock_db,
        kbd_id=999,
        stage="count",
        raw_content="测试原文",
        reason="UNCOUNTABLE",
        detail_payload={"msg": "error"},
    )
    mock_independent_session.add.assert_called_once()
    mock_independent_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_record_failure_dry_run_does_not_touch_database():
    """dry-run 失败记录必须完全禁写数据库。"""
    mock_db = MagicMock()
    result = await SignalAssetService.record_failure(
        db_manager=mock_db,
        kbd_id=1,
        stage="count",
        raw_content="x",
        reason="UNCOUNTABLE",
        persist=False,
    )
    assert result == -1
    mock_db.async_session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_record_failure_graceful_exception_handling():
    """验证即使数据库写入失败也优雅返回 -1，绝不阻塞抛出异常"""
    mock_session = MagicMock()
    mock_session.begin_nested.side_effect = RuntimeError("DB down")

    ret_id = await SignalAssetService.record_failure(
        session=mock_session, kbd_id=1000, stage="classify", raw_content="异常内容", reason="UNCLASSIFIED"
    )
    assert ret_id == -1
