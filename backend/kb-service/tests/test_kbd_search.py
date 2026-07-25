"""KBD 候选检索正确性测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import kbd_search
from shared.dynamic_resource.models import UsageStatus


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


@pytest.mark.asyncio
async def test_vector_candidates_enforces_model_and_similarity_gate():
    session = SimpleNamespace(execute=AsyncMock(return_value=_RowsResult([(11, 0.82)])))

    ids, scores = await kbd_search._vector_candidates(session, "vm-001", [0.1, 0.2], 5)

    statement, params = session.execute.await_args.args
    sql = str(statement)
    assert "embedding_model = :embedding_model" in sql
    assert ">= :min_similarity" in sql
    assert "CAST(:query_vector AS vector)" in sql
    assert params["embedding_model"] == kbd_search.settings.LLM_EMBEDDING_MODEL
    assert params["min_similarity"] == kbd_search.settings.KBD_MIN_SIMILARITY
    assert ids == [11]
    assert scores == {11: 0.82}


@pytest.mark.asyncio
async def test_fts_candidates_uses_segmented_query_once_when_empty():
    session = SimpleNamespace(execute=AsyncMock(return_value=_RowsResult([])))

    with patch.object(kbd_search, "segment", return_value="虚拟机 镜像 异常") as mock_segment:
        ids, scores = await kbd_search._fts_candidates(session, "vm-001", "虚拟机镜像异常", 5)

    mock_segment.assert_called_once_with("虚拟机镜像异常")
    session.execute.assert_awaited_once()
    statement, params = session.execute.await_args.args
    assert "plainto_tsquery('simple', :query)" in str(statement)
    assert "published_at" not in str(statement)
    assert params["query"] == "虚拟机 镜像 异常"
    assert ids == []
    assert scores == {}


@pytest.mark.asyncio
async def test_audit_marks_candidates_as_retrieved_with_context():
    entry = SimpleNamespace(
        id=7,
        title="虚拟机启动失败",
        category_id="vm-001",
        problem_description="无法启动",
        alert_info="启动异常",
        steps_text="检查任务",
        root_cause="镜像损坏",
        solution="替换镜像",
        operational_impact="虚拟机不可用",
        is_temporary=False,
        recommendations="检查存储",
        signals_json=[{"name": "task_failed"}],
    )
    snapshot = SimpleNamespace()
    publisher = MagicMock()
    publisher.ensure_published = AsyncMock(return_value=snapshot)
    loader = MagicMock()
    loader.audit_usage = AsyncMock()

    with (
        patch.object(kbd_search, "DynamicResourcePublisher", return_value=publisher),
        patch.object(kbd_search, "DynamicResourceLoader", return_value=loader),
        patch.object(kbd_search, "kbd_resource_payload", return_value={}),
        patch.object(kbd_search, "snapshot_revision_metadata", return_value={"revision": 3}),
        patch.object(kbd_search, "get_current_trace_id", return_value="trace-1"),
    ):
        cases = await kbd_search._audit_and_serialize(
            MagicMock(),
            [entry],
            {7: 0.75},
            category_id="vm-001",
            query="虚拟机启动失败",
            top_k=5,
            search_path="vector",
            conversation_id="conv-1",
            case_id="case-1",
        )

    usage = loader.audit_usage.await_args.args[1]
    assert usage.status is UsageStatus.RETRIEVED
    assert usage.conversation_id == "conv-1"
    assert usage.case_id == "case-1"
    assert usage.output_payload == {"rank": 1, "score": 0.75}
    assert usage.metadata["search_path"] == "vector"
    assert cases[0]["resource_revision"] == {"revision": 3}


@pytest.mark.asyncio
async def test_search_returns_empty_when_vector_and_fts_have_no_candidates():
    session = AsyncMock()
    session.commit = AsyncMock()

    class _SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    db = SimpleNamespace(async_session_factory=lambda: _SessionContext())
    embedding = SimpleNamespace(embed_for_search=AsyncMock(return_value=[0.1, 0.2]))

    with (
        patch.object(kbd_search, "_db_manager", db),
        patch.object(kbd_search, "_embedding_service", embedding),
        patch.object(kbd_search, "_vector_candidates", AsyncMock(return_value=([], {}))) as vector_candidates,
        patch.object(kbd_search, "_fts_candidates", AsyncMock(return_value=([], {}))) as fts_candidates,
        patch.object(kbd_search, "_load_entries_in_order", AsyncMock(return_value=[])),
        patch.object(kbd_search, "_audit_and_serialize", AsyncMock(return_value=[])),
    ):
        response = await kbd_search.search_kbds(
            request=MagicMock(),
            category_id="vm-001",
            query="完全无关的问题",
            top_k=5,
            conversation_id="conv-1",
            case_id="case-1",
        )

    assert response == {"cases": []}
    vector_candidates.assert_awaited_once()
    fts_candidates.assert_awaited_once()
    session.commit.assert_awaited_once()
