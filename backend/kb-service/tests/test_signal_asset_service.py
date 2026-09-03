"""
backend/kb-service/tests/test_signal_asset_service.py
单元测试验证 SignalAssetService 的模板缓存、最佳实践 Few-Shot 过滤及非阻塞异常持久化。
"""

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
    mock_bp.raw_evidence = "检查 log"
    mock_bp.signal_json = {"id": "sig_1"}
    mock_bp.design_notes = "设计要点"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_bp]
    mock_session.execute.return_value = mock_result

    res1 = await SignalAssetService.get_best_practices_by_tool(mock_session, "qfk_log", limit=3)
    assert len(res1) == 1
    assert res1[0]["support_id"] == "18906"
    assert mock_session.execute.call_count == 1

    res2 = await SignalAssetService.get_best_practices_by_tool(mock_session, "qfk_log", limit=3)
    assert res2 == res1
    assert mock_session.execute.call_count == 1


@pytest.mark.asyncio
async def test_record_failure_with_db_manager_independent_commit():
    """验证 record_failure 优先使用 db_manager 开辟独立事务提交"""
    mock_db = MagicMock()
    mock_independent_session = AsyncMock()
    mock_db.async_session_factory.return_value.__aenter__.return_value = mock_independent_session

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
async def test_record_failure_graceful_exception_handling():
    """验证即使数据库写入失败也优雅返回 -1，绝不阻塞抛出异常"""
    mock_session = AsyncMock()
    mock_session.begin_nested.side_effect = RuntimeError("DB down")

    ret_id = await SignalAssetService.record_failure(
        session=mock_session, kbd_id=1000, stage="classify", raw_content="异常内容", reason="UNCLASSIFIED"
    )
    assert ret_id == -1
