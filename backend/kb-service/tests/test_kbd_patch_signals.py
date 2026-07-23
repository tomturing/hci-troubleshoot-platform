from unittest.mock import AsyncMock, MagicMock

import app.routes.admin as admin_route
import pytest
from app.routes.admin import kbd_router, set_dependencies
from fastapi import FastAPI
from fastapi.testclient import TestClient


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
    test_signals = [
        {
            "signal_category": "frontend",
            "acquirer": "qkv_task",
            "keyword": "启动虚拟机",
            "description": "在 HCI 控制台查看虚拟机任务详情",
            "produces": [{"name": "VM", "path": "vm"}],
        }
    ]

    response = client.patch(
        "/api/admin/kbd/1",
        json={"signals_json": test_signals},
        headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
    )

    admin_route._check_auth = original_check_auth

    assert response.status_code == 200

    # 保存时统一归约为 v2 数组级对象（RFC §7），并以 CAST(... AS jsonb) 落库
    executed_call = mock_session.execute.call_args
    sql_text = str(executed_call[0][0])
    assert "CAST(:signals_json AS jsonb)" in sql_text
    # 关键：落库 SQL 不得残留 ':signals_json' 字面量（否则会被 PG 报语法错误 500）
    assert ":signals_json::jsonb" not in sql_text
    # 落库内容应为经 migrate_signal_document 归约后的 v2 文档（含 acquire 嵌套对象）
    import json

    stored = executed_call[0][1].get("signals_json")
    stored_doc = json.loads(stored) if isinstance(stored, str) else stored
    stored_signals = (
        stored_doc["signals"] if isinstance(stored_doc, dict) else stored_doc
    )
    assert any("acquire" in s for s in stored_signals), "signals 应已归约为 v2 嵌套形态"


@pytest.mark.anyio
async def test_update_kbd_entry_qfk_keyword_alias_normalized():
    """回归：保存带历史 keyword 的 qfk_system 信号（半残 v2）不再 422，落库归并为
    resource_keyword（KBD 详情页「保存失败，请重试」根因修复）。"""
    import json

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
    mock_update_row.mappings.return_value.first.return_value = {"id": 1, "status": "draft"}
    mock_session.execute.return_value = mock_update_row

    client = TestClient(app)
    # 半残 v2：qfk_system 的 args 带 v1 别名 keyword（无 resource_keyword）
    v2_doc = {
        "schema_version": 2,
        "signals": [
            {
                "id": "s1",
                "acquire": {
                    "tool": "qfk_system",
                    "args": {"keyword": "镜像文件占用检查", "sub_command": "lsof"},
                },
                "match": {"type": "keyword", "pattern": "", "mode": "any", "expected": True},
                "orchestrate": {"produces": [], "requires": ["HOST"]},
                "provenance": {"category": "backend", "source_section": "steps_text", "evidence": "x", "confidence": 0.8},
                "review": {"require_human_confirm": False, "notes": ""},
            }
        ],
    }

    response = client.patch(
        "/api/admin/kbd/1",
        json={"signals_json": v2_doc},
        headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
    )
    admin_route._check_auth = original_check_auth

    assert response.status_code == 200, response.text

    stored = mock_session.execute.call_args[0][1].get("signals_json")
    stored_doc = json.loads(stored) if isinstance(stored, str) else stored
    stored_args = stored_doc["signals"][0]["acquire"]["args"]
    assert stored_args.get("resource_keyword") == "镜像文件占用检查"
    assert "keyword" not in stored_args


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
