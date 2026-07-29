from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.kbd_entry import KbdEntry
from app.routes.admin import kbd_router, set_dependencies
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.anyio
async def test_kbd_entry_sync_sections_from_content_md():
    # Test unstructured to structured parsing
    kbd = KbdEntry()
    kbd.content_md = """
## 问题描述
这描述了虚拟机的开机故障。

## 告警信息
> **【截图说明】**
> TYPE: ERROR
> BACKGROUND: BLUE
> 故障告警。

## 有效排查步骤
1. 检查 network。

## 解决方案
重启服务。
"""
    kbd.images_json = [
        {"seq": 0, "desc": "TYPE: ERROR\nBACKGROUND: BLUE\n故障告警。"}
    ]

    kbd.sync_sections_from_content_md()

    assert kbd.problem_description == "这描述了虚拟机的开机故障。"
    assert kbd.alert_info == "![img:0]"
    assert kbd.steps_text == "1. 检查 network。"
    assert kbd.solution == "重启服务。"
    # Check other empty fields
    assert kbd.root_cause == ""
    assert kbd.recommendations == ""


def test_rebuild_content_md_excludes_unverified_image_inference_from_agent_view():
    kbd = KbdEntry()
    kbd.problem_description = "故障现象如下：![img:0]"
    kbd.images_json = [{
        "seq": 0,
        "section": "problem_description",
        "desc": (
            "TYPE: 告警截图\nBACKGROUND: 白色\nFULL_TEXT:\n"
            "- 数据通信口(vxlan)告警\nDESCRIPTION:\n"
            "网口掉线是导致后续内存错误的根本原因。"
        ),
        "evidence": {
            "quality": {
                "status": "success",
                "needs_review": False,
                "inference_status": "needs_review",
                "inference_needs_review": True,
                "inference_issues": ["unsupported_causal_claim"],
            }
        },
    }]

    content_md = kbd.rebuild_content_md()

    assert "数据通信口(vxlan)告警" in content_md
    assert "INFERENCE_STATUS: needs_review" in content_md
    assert "模型语义描述未进入 Agent 文档" in content_md
    assert "根本原因" not in content_md

    kbd.content_md = content_md
    kbd.sync_sections_from_content_md()
    assert kbd.problem_description.replace("\n", "") == "故障现象如下：![img:0]"

@pytest.mark.anyio
async def test_update_kbd_entry_api_sync_sections():
    # Test patch api route
    app = FastAPI()
    app.include_router(kbd_router)

    # We mock _db_manager
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    # Mock authentication to bypass check
    import app.routes.admin as admin_route
    original_check_auth = admin_route._check_auth
    admin_route._check_auth = lambda req: None

    set_dependencies(mock_db)

    status_row = MagicMock()
    status_row.scalar_one_or_none.return_value = "draft"

    # Mock SELECT for update route to return images_json
    image_row = MagicMock()
    image_row.mappings.return_value.first.return_value = {
        "images_json": [{"seq": 0, "desc": "TYPE: ERR\nBG: BLU\nTest image."}]
    }

    kbd = SimpleNamespace(
        id=123,
        latest_proposal_revision_id=None,
        working_revision_id=None,
        lock_version=0,
        status="draft",
        signals_json={},
        content_md="",
    )
    kbd_row = MagicMock()
    kbd_row.scalar_one_or_none.return_value = kbd

    # Mock UPDATE to return id, status
    mock_update_row = MagicMock()
    mock_update_row.mappings.return_value.first.return_value = {
        "id": 123,
        "status": "draft",
        "lock_version": 1,
    }

    mock_session.execute.side_effect = [status_row, image_row, kbd_row, mock_update_row]

    client = TestClient(app)

    new_content_md = """
## 问题描述
新的问题描述。

## 告警信息
> **【截图说明】**
> TYPE: ERR
> BG: BLU
> Test image.
"""

    revision = SimpleNamespace(
        id=1,
        revision_no=1,
        revision_type="proposal",
        parent_revision_id=None,
        checksum="a" * 64,
        actor_id=None,
        actor_type="migration",
        validation_summary={},
        created_at=None,
    )
    with patch.object(admin_route, "ensure_kbd_revision", AsyncMock(return_value=revision)):
        response = client.patch(
            "/api/admin/kbd/123",
            json={"content_md": new_content_md},
            headers={"Authorization": "Bearer any_token"}
        )

    assert response.status_code == 200

    # Restore authentication mock
    admin_route._check_auth = original_check_auth

    # Let's inspect the call to UPDATE to verify it contains the parsed fields
    assert mock_session.execute.call_count == 4
    update_call = mock_session.execute.call_args_list[3]
    sql_text = str(update_call[0][0])
    params = update_call[0][1]

    assert "problem_description = :problem_description" in sql_text
    assert "alert_info = :alert_info" in sql_text
    assert "content_md = :content_md" in sql_text

    assert params["problem_description"] == "新的问题描述。"
    assert params["alert_info"] == "![img:0]"
