from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.routes.admin as admin_route
from app.routes.admin import kbd_router, set_dependencies
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client_with_session(session: AsyncMock) -> tuple[TestClient, MagicMock, object]:
    app = FastAPI()
    app.include_router(kbd_router)
    db = MagicMock()
    session.__aenter__.return_value = session
    db.async_session_factory.return_value = session
    original = admin_route._check_auth
    admin_route._check_auth = lambda request: None
    set_dependencies(db)
    return TestClient(app), db, original


def test_published_entry_patch_is_blocked_before_unreviewed_content_can_replace_active():
    session = AsyncMock()
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = "published"
    session.execute.return_value = status_result
    client, _, original_auth = _client_with_session(session)

    try:
        response = client.patch(
            "/api/admin/kbd/9",
            json={"title": "未经复核的新标题"},
            headers={"Authorization": "Bearer test"},
        )
    finally:
        admin_route._check_auth = original_auth

    assert response.status_code == 409
    assert "不能直接覆盖编辑" in response.json()["detail"]
    assert all("UPDATE kbd_entry" not in str(call.args[0]) for call in session.execute.call_args_list)


def test_revert_to_draft_deactivates_existing_runtime_pointer_in_same_transaction():
    session = AsyncMock()
    select_result = MagicMock()
    select_result.mappings.return_value.first.return_value = {"id": 9, "status": "published"}
    update_result = MagicMock()
    delete_result = MagicMock()
    delete_result.rowcount = 1
    session.execute.side_effect = [select_result, update_result, delete_result]
    client, _, original_auth = _client_with_session(session)

    try:
        response = client.post(
            "/api/admin/kbd/9/revert-to-draft",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        admin_route._check_auth = original_auth

    assert response.status_code == 200
    assert response.json()["active_deactivated"] is True
    delete_sql = str(session.execute.call_args_list[2].args[0])
    assert "DELETE FROM dynamic_resource_active" in delete_sql
    assert session.execute.call_args_list[2].args[1] == {"resource_name": "9"}
    session.commit.assert_awaited_once()


def test_candidate_validation_is_side_effect_free_and_separates_contract_from_runtime_proof():
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        id=9,
        title="虚拟机启动失败",
        problem_description="点击开机后任务失败",
        root_cause="镜像被占用",
        solution="释放占用后重试",
        category_id="vm-001",
        ai_category_id=None,
        working_revision_id=2,
        lock_version=3,
        signals_json={
            "schema_version": 2,
            "signals": [
                {
                    "id": "sig_001",
                    "acquire": {"tool": "qkv_task", "args": {"keyword": "虚拟机启动失败"}},
                    "match": None,
                    "orchestrate": {"produces": [{"name": "VM", "path": "vm"}]},
                    "provenance": {"category": "frontend"},
                }
            ],
        },
    )
    client, _, original_auth = _client_with_session(session)

    try:
        response = client.post(
            "/api/admin/kbd/9/validate",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        admin_route._check_auth = original_auth

    assert response.status_code == 200
    body = response.json()
    assert body["publishable"] is True
    assert body["runtime_verified"] is False
    assert body["warning_count"] == 1
    assert body["issues"][0]["code"] == "CAPABILITY_RUNTIME_UNVERIFIED"
    session.commit.assert_not_awaited()


async def test_approval_freezes_a_distinct_expert_revision_even_when_payload_is_unchanged():
    session = AsyncMock()
    kbd = SimpleNamespace(id=9, latest_proposal_revision_id=1, working_revision_id=2)
    kbd_result = MagicMock()
    kbd_result.scalar_one.return_value = kbd
    session.execute.return_value = kbd_result
    session.get.return_value = SimpleNamespace(id=1)
    approved = SimpleNamespace(id=3)

    with patch.object(admin_route, "ensure_kbd_revision", AsyncMock(return_value=approved)) as ensure:
        result = await admin_route._freeze_approved_expert_revision(
            session,
            kbd_id=9,
            reviewer_id=1,
            review_note="专家确认可发布",
            trace_id="trace-1",
        )

    assert result is approved
    assert ensure.await_args.kwargs["parent_revision_id"] == 2
    assert ensure.await_args.kwargs["reuse_existing"] is False
    assert ensure.await_args.kwargs["generation_metadata"]["review_note"] == "专家确认可发布"
