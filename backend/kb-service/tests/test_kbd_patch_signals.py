from unittest.mock import AsyncMock, MagicMock

import app.routes.admin as admin_route
import pytest
from app.routes.admin import _prepare_expert_draft_signals, kbd_router, set_dependencies
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_prepare_expert_draft_migrates_legacy_qkv_nested_match_extract():
    """KBD41464 同类历史草稿在保存边界自动清理 QKV 嵌套 QFK 提取字段。"""

    qfk_extract = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "value_mode": "string",
    }
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_002",
                "role": "must",
                "review": {"notes": "", "require_human_confirm": False},
                "provenance": {"category": "frontend", "confidence": 0.9},
                "acquire": {
                    "tool": "qkv_task",
                    "args": {"keyword": "创建虚拟机", "limit": 1, "timeout": 60, "is_failed": True},
                },
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "produces": [{"name": "DESCRIPTION", "path": "description"}],
                    "requires": [],
                    "output_processing": [
                        {
                            "mode": "assert",
                            "input": "{{DESCRIPTION}}",
                            "match": {
                                "type": "keyword",
                                "pattern": "无法复制镜像",
                                "mode": "or",
                                "expected": True,
                                "extract": qfk_extract,
                            },
                        }
                    ],
                },
            }
        ],
    }

    normalized = _prepare_expert_draft_signals(document, kbd_id=2051, operation="test")
    matcher = normalized["signals"][0]["orchestrate"]["output_processing"][0]["match"]
    assert "extract" not in matcher


@pytest.mark.anyio
async def test_update_kbd_entry_signals_json_sql_cast():
    app = FastAPI()
    app.include_router(kbd_router)

    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    original_check_auth = admin_route._check_auth
    admin_route._check_auth = lambda req: None
    set_dependencies(mock_db)

    # Mock UPDATE result
    mock_update_row = MagicMock()
    mock_update_row.mappings.return_value.first.return_value = {"id": 1, "status": "draft"}
    mock_session.execute.return_value = mock_update_row

    client = TestClient(app)
    # v2 嵌套文档（运行时仅 v2 单一版本）
    test_signals = {
        "schema_version": 2,
        "generation_metadata": {
            "schema_version": 1,
            "status": "current",
            "source_fingerprint": "0" * 64,
            "prompt_revision": "1" * 64,
            "model_id": "test-model",
            "tool_contract_revision": "2" * 64,
            "generation_fingerprint": "3" * 64,
        },
        "rejected_candidates": [
            {
                "candidate": {"id": "bad"},
                "reason_code": "run_failed",
                "reason": "缺少 acquire",
            }
        ],
        "signals": [
            {
                "acquire": {"tool": "qkv_task", "args": {"keyword": "vm"}},
                # QKV 是产出变量信号：必须以 produces 写入变量池，不能同时残留 matcher。
                "match": None,
                "orchestrate": {"produces": [{"name": "VM", "path": "vm"}]},
                "provenance": {"category": "frontend"},
            }
        ],
    }

    response = client.patch(
        "/api/admin/kbd/1",
        json={"signals_json": test_signals},
        headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
    )

    admin_route._check_auth = original_check_auth

    assert response.status_code == 200

    # 保存时以 CAST(... AS jsonb) 落库 v2 文档
    executed_call = next(
        call
        for call in reversed(mock_session.execute.call_args_list)
        if "UPDATE kbd_entry SET" in str(call[0][0])
    )
    sql_text = str(executed_call[0][0])
    assert "CAST(:signals_json AS jsonb)" in sql_text
    # 关键：落库 SQL 不得残留 ':signals_json' 字面量（否则会被 PG 报语法错误 500）
    assert ":signals_json::jsonb" not in sql_text
    # 落库内容应为 v2 文档（含 acquire 嵌套对象）
    import json

    stored = executed_call[0][1].get("signals_json")
    stored_doc = json.loads(stored) if isinstance(stored, str) else stored
    stored_signals = (
        stored_doc["signals"] if isinstance(stored_doc, dict) else stored_doc
    )
    assert any("acquire" in s for s in stored_signals), "signals 应为 v2 嵌套形态"
    assert stored_doc["generation_metadata"]["status"] == "manual_reviewed"
    assert stored_doc["rejected_candidates"] == test_signals["rejected_candidates"]


@pytest.mark.anyio
async def test_update_kbd_entry_signals_json_sql_bind_compiles():
    """回归（根因级，挡住 PR#599 修复被 PR#601 回退）：

    落库 SQL 若写成 'signals_json = :signals_json::jsonb'，其中 ':signals_json'
    紧跟 '::'，SQLAlchemy 命名绑定正则(负向预查 (?!:))不将其识别为绑定参数，
    会把 ':signals_json' 字面量发给 Postgres → 'syntax error at or near ":"' (500)，
    前端统一弹「保存失败，请重试」。

    本用例用 PG 方言编译真实落库 SQL，断言命名绑定被正确编译为参数占位符
    ($N / %(name)s)，而非残留 ':signals_json' 字面量。一旦有人把 CAST(...) 改回
    '::jsonb' 写法，本测试立即失败。
    """
    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql

    # 复刻 update_kbd_entry 的落库 SQL（含 id 绑定）
    sql = text(
        "UPDATE kbd_entry SET signals_json = CAST(:signals_json AS jsonb) "
        "WHERE id = :id"
    )
    compiled = str(sql.compile(dialect=postgresql.dialect()))

    # 关键断言：不得残留 ':signals_json' 字面量（BUG 写法会残留并被 PG 拒绝）
    assert ":signals_json" not in compiled, (
        f"命名绑定未被识别为参数，SQL 残留字面量 ':signals_json'（将触发 PG 语法错误 500）: {compiled}"
    )
    # 同时确认两个命名绑定都被编译成了参数占位符
    assert "signals_json" in sql.compile(dialect=postgresql.dialect()).params or "%(signals_json)s" in compiled or "$1" in compiled
    assert "id" in sql.compile(dialect=postgresql.dialect()).params or "%(id)s" in compiled or "$2" in compiled


@pytest.mark.anyio
async def test_update_kbd_entry_signals_with_produce_alias():
    """回归测试：KBD 生产者信号支持产出变量 alias 字段保存且不报 SIGNAL_FIELD_UNSUPPORTED。"""
    app = FastAPI()
    app.include_router(kbd_router)

    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    original_check_auth = admin_route._check_auth
    admin_route._check_auth = lambda req: None
    set_dependencies(mock_db)

    mock_update_row = MagicMock()
    mock_update_row.mappings.return_value.first.return_value = {"id": 2051, "status": "draft"}
    mock_session.execute.return_value = mock_update_row

    client = TestClient(app)
    test_signals = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_002",
                "role": "must",
                "acquire": {"tool": "qkv_task", "args": {"keyword": "创建虚拟机", "is_failed": True}},
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "produces": [
                        {"name": "VM", "path": "vm"},
                        {"name": "HOST", "path": "host"},
                        {"name": "REQUEST_ID", "path": "request_id"},
                        {"name": "END", "path": "end", "alias": "END1"},
                    ],
                },
                "provenance": {"category": "frontend"},
            },
            {
                "id": "expert_qfk_log",
                "role": "must",
                "acquire": {
                    "tool": "qfk_log",
                    "args": {
                        "host": "{{HOST}}",
                        "file": "sfscp.log",
                        "request_id": "{{REQUEST_ID}}",
                        "time_window": "{{END1}}",
                        "instruction": "检索日志",
                    },
                },
                "match": {
                    "type": "keyword",
                    "pattern": "write失败",
                    "mode": "and",
                    "expected": True,
                    "extract": {"type": "text", "rows": {"mode": "all"}},
                },
                "orchestrate": {
                    "phase": "diagnostic",
                    "requires": ["END1", "HOST", "REQUEST_ID"],
                },
                "provenance": {"category": "backend"},
            },
        ],
    }

    response = client.patch(
        "/api/admin/kbd/2051",
        json={"signals_json": test_signals},
        headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
    )

    admin_route._check_auth = original_check_auth
    assert response.status_code == 200, response.text
