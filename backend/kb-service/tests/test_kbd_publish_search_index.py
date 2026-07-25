"""KBD 发布时检索索引更新测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import admin


class _MappingResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_approve_clears_stale_embedding_when_provider_fails():
    source_row = {
        "id": 7,
        "title": "虚拟机镜像异常",
        "content_md": "## 问题描述\n虚拟机无法启动",
        "content_raw": "旧内容",
        "problem_description": "虚拟机无法启动",
        "alert_info": "镜像异常",
        "root_cause": "镜像损坏",
        "status": "draft",
        "published_at": None,
        "embedding": "[0.1,0.2]",
        "signals_json": {
            "schema_version": "2.0",
            "signals": [{"acquire": {"tool": "qfk_task"}, "provenance": {"category": "backend"}}],
        },
        "category_id": "vm-001",
        "ai_category_id": None,
    }
    published_at = datetime.now(UTC)
    updated_row = {"id": 7, "status": "published", "embedding": None, "published_at": published_at}

    read_session = SimpleNamespace(execute=AsyncMock(return_value=_MappingResult(source_row)))
    write_session = SimpleNamespace(
        execute=AsyncMock(return_value=_MappingResult(updated_row)),
        commit=AsyncMock(),
    )
    sessions = iter([read_session, write_session])
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(next(sessions)))
    embedding = SimpleNamespace(embed_single=AsyncMock(side_effect=RuntimeError("provider unavailable")))

    with (
        patch.object(admin, "_check_auth"),
        patch.object(admin, "_db_manager", db),
        patch.object(admin, "_embedding_service", embedding),
        patch.object(admin, "segment", return_value="虚拟机 镜像 异常"),
        patch.object(admin, "_publish_kbd_revision", AsyncMock(return_value={"revision": 1})),
    ):
        response = await admin.approve_kbd_entry(
            request=MagicMock(),
            kbd_id=7,
            body=admin.KbdApproveRequest(reviewer_id=1),
        )

    statement, params = write_session.execute.await_args.args
    sql = str(statement)
    assert "embedding = NULL" in sql
    assert "embedding_model = NULL" in sql
    assert "embedding_content_hash = NULL" in sql
    assert "embedding_updated_at = NULL" in sql
    assert "to_tsvector('simple', :tsv_text)" in sql
    assert params["tsv_text"] == "虚拟机 镜像 异常"
    assert response.embedding_generated is False
    write_session.commit.assert_awaited_once()
