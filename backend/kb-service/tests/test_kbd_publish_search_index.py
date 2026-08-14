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
            "schema_version": 2,
            "signals": [
                {
                    "id": "sig_task_failure",
                    "role": "must",
                    "acquire": {
                        "tool": "qkv_task",
                        "args": {"keyword": "虚拟机启动失败"},
                    },
                    "match": None,
                    "orchestrate": {
                        "produces": [{"name": "VM", "path": "vm"}],
                        "requires": [],
                    },
                    "provenance": {"category": "frontend"},
                },
                {
                    "id": "sig_001",
                    "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
                    "match": {
                        "type": "exists",
                        "expected": True,
                        "extract": {
                            "type": "text",
                            "rows": {"mode": "all"},
                            "cardinality": "all",
                            "source": "stdout",
                            "value_mode": "string",
                        },
                    },
                    "provenance": {"category": "backend"},
                }
            ],
        },
        "category_id": "vm-001",
        "ai_category_id": None,
        "lock_version": 4,
    }
    published_at = datetime.now(UTC)
    updated_row = {"id": 7, "status": "published", "embedding": None, "published_at": published_at}
    published_entry = SimpleNamespace(working_revision_id=2)

    read_session = SimpleNamespace(execute=AsyncMock(return_value=_MappingResult(source_row)))
    write_session = SimpleNamespace(
        execute=AsyncMock(return_value=_MappingResult(updated_row)),
        get=AsyncMock(return_value=published_entry),
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
        patch.object(
            admin,
            "_freeze_approved_expert_revision",
            AsyncMock(return_value=SimpleNamespace(
                id=2,
                revision_no=2,
                revision_type="expert",
                parent_revision_id=1,
                checksum="a" * 64,
                actor_id=None,
                actor_type="expert",
                validation_summary={"status": "passed"},
                created_at=published_at,
            )),
        ),
        patch.object(admin, "_publish_kbd_revision", AsyncMock(return_value={"revision": 1})),
    ):
        response = await admin.approve_kbd_entry(
            request=MagicMock(),
            kbd_id=7,
            body=admin.KbdApproveRequest(reviewer_id=1, lock_version=4),
        )

    statement, params = write_session.execute.await_args.args
    sql = str(statement)
    assert "embedding = NULL" in sql
    assert "embedding_model = NULL" in sql
    assert "embedding_content_hash = NULL" in sql
    assert "embedding_updated_at = NULL" in sql
    assert "to_tsvector('simple', :tsv_text)" in sql
    assert "lock_version = :expected_lock_version" in sql
    assert params["expected_lock_version"] == 4
    assert params["tsv_text"] == "虚拟机 镜像 异常"
    assert response.embedding_generated is False
    assert published_entry.working_revision_id is None
    write_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_blocks_before_indexing_when_shared_runtime_blocks_signal():
    source_row = {
        "id": 8,
        "title": "命令不可执行",
        "content_md": "## 问题描述\n命令不可执行",
        "content_raw": "内容",
        "problem_description": "命令不可执行",
        "alert_info": "异常",
        "root_cause": "命令不存在",
        "status": "draft",
        "published_at": None,
        "embedding": None,
        "signals_json": {
            "schema_version": 2,
            "signals": [
                {
                    "id": "unknown-command",
                    "acquire": {
                        "tool": "qfk_system",
                        "args": {"command": "definitely_not_a_real_acli_command"},
                    },
                    "match": {
                        "type": "exists",
                        "expected": True,
                        "extract": {
                            "type": "text",
                            "rows": {"mode": "all"},
                            "cardinality": "all",
                            "source": "stdout",
                        },
                    },
                    "provenance": {"category": "backend"},
                }
            ],
        },
        "category_id": "vm-001",
        "ai_category_id": None,
        "lock_version": 1,
    }
    read_session = SimpleNamespace(execute=AsyncMock(return_value=_MappingResult(source_row)))
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(read_session))
    embedding = SimpleNamespace(embed_single=AsyncMock())

    with (
        patch.object(admin, "_check_auth"),
        patch.object(admin, "_db_manager", db),
        patch.object(admin, "_embedding_service", embedding),
    ):
        with pytest.raises(admin.HTTPException) as exc_info:
            await admin.approve_kbd_entry(
                request=MagicMock(),
                kbd_id=8,
                body=admin.KbdApproveRequest(reviewer_id=1, lock_version=1),
            )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "SIGNAL_REVIEW_BLOCKED"
    assert any(
        issue["code"] == "SYSTEM_COMMAND_UNKNOWN"
        for issue in exc_info.value.detail["review"]["issues"]
    )
    embedding.embed_single.assert_not_awaited()
    read_session.execute.assert_awaited_once()
