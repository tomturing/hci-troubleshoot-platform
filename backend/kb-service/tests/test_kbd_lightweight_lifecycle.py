from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.routes.admin as admin_route
import pytest
from app.services.kbd_revision_service import KBD_PAYLOAD_FIELDS


def _db_with_session(session: AsyncMock) -> MagicMock:
    db = MagicMock()
    session.__aenter__.return_value = session
    db.async_session_factory.return_value = session
    return db


@pytest.mark.asyncio
async def test_published_entry_patch_is_blocked_before_unreviewed_content_can_replace_active():
    session = AsyncMock()
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = "published"
    session.execute.return_value = status_result
    db = _db_with_session(session)

    with patch.object(admin_route, "_check_auth"), patch.object(admin_route, "_db_manager", db):
        with pytest.raises(admin_route.HTTPException) as exc_info:
            await admin_route.update_kbd_entry(
                MagicMock(),
                9,
                admin_route.KbdUpdateRequest(title="未经复核的新标题"),
            )

    assert exc_info.value.status_code == 409
    assert "不能直接覆盖编辑" in exc_info.value.detail
    assert all("UPDATE kbd_entry" not in str(call.args[0]) for call in session.execute.call_args_list)


@pytest.mark.asyncio
async def test_revert_to_draft_publishes_tombstone_in_same_transaction():
    session = AsyncMock()
    entry = SimpleNamespace(id=9, status="published", lock_version=3)
    session.get.return_value = entry
    db = _db_with_session(session)
    tombstone = AsyncMock(return_value={"revision": 2, "status": "disabled"})

    with (
        patch.object(admin_route, "_check_auth"),
        patch.object(admin_route, "_db_manager", db),
        patch.object(admin_route, "_publish_kbd_tombstone", tombstone),
    ):
        response = await admin_route.revert_kbd_to_draft(MagicMock(), 9)

    assert response["active_deactivated"] is True
    assert response["resource_revision"]["revision"] == 2
    assert entry.status == "draft"
    assert entry.lock_version == 4
    tombstone.assert_awaited_once()
    assert tombstone.await_args.kwargs["lifecycle_status"] == "draft"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_archive_published_kbd_publishes_disabled_tombstone():
    session = AsyncMock()
    entry = SimpleNamespace(id=9, status="published", lock_version=4)
    session.get.return_value = entry
    db = _db_with_session(session)
    tombstone = AsyncMock(return_value={"revision": 3, "status": "disabled"})

    with (
        patch.object(admin_route, "_check_auth"),
        patch.object(admin_route, "_db_manager", db),
        patch.object(admin_route, "_publish_kbd_tombstone", tombstone),
    ):
        response = await admin_route.archive_kbd_entry(MagicMock(), 9)

    assert response["status"] == "archived"
    assert entry.status == "archived"
    assert entry.lock_version == 5
    tombstone.assert_awaited_once()
    assert tombstone.await_args.kwargs["lifecycle_status"] == "archived"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_kbd_tombstone_is_append_only_and_removes_active_pointer():
    session = AsyncMock()
    entry = SimpleNamespace(
        id=9,
        support_id="37150",
        title="虚拟机启动失败",
        category_id="vm",
        problem_description="启动失败",
        alert_info="",
        steps_text="检查日志",
        signals_json={"schema_version": 2, "signals": [{"id": "s1"}]},
        content_md="正文",
        content_raw="正文",
        images_json=[],
        root_cause="服务异常",
        solution="恢复服务",
        operational_impact="",
        is_temporary="否",
        recommendations="",
        status="draft",
        published_at=None,
        entry_metadata={"offline_scenario": "vm_start_failed"},
    )
    snapshot = SimpleNamespace(
        resource_type="kbd",
        resource_name="9",
        revision=2,
        version="1.0",
        checksum="checksum-2",
    )
    publisher = MagicMock()
    publisher.ensure_published = AsyncMock(return_value=snapshot)

    with patch.object(admin_route, "DynamicResourcePublisher", return_value=publisher):
        result = await admin_route._publish_kbd_tombstone(
            session,
            entry,
            lifecycle_status="draft",
            trace_id="trace-tombstone",
        )

    assert result["revision"] == 2
    payload = publisher.ensure_published.await_args.kwargs
    assert payload["status"] == "disabled"
    assert payload["contract"]["lifecycle"] == {"state": "draft", "tombstone": True}
    assert payload["contract"]["metadata"]["offline_scenario"] == "vm_start_failed"
    delete_sql = str(session.execute.await_args.args[0])
    assert "DELETE FROM dynamic_resource_active" in delete_sql
    assert session.execute.await_args.args[1] == {"resource_name": "9"}


@pytest.mark.asyncio
async def test_explicit_republish_has_new_lifecycle_event_identity():
    """相同正文重新发布也必须越过 tombstone 后的增量 Watermark。"""

    session = AsyncMock()
    entry = SimpleNamespace(id=9)
    session.execute.return_value.scalar_one_or_none.return_value = entry
    snapshot = SimpleNamespace(
        resource_type="kbd",
        resource_name="9",
        revision=4,
        version="1.0",
        checksum="checksum-4",
    )
    publisher = MagicMock()
    publisher.ensure_published = AsyncMock(return_value=snapshot)

    with (
        patch.object(admin_route, "kbd_resource_payload", return_value={"contract": {"metadata": {}}}),
        patch.object(admin_route, "DynamicResourcePublisher", return_value=publisher),
    ):
        result = await admin_route._publish_kbd_revision(
            session,
            9,
            "trace-republish",
            lifecycle_event_id=42,
        )

    assert result["revision"] == 4
    assert publisher.ensure_published.await_args.kwargs["contract"]["lifecycle"] == {
        "state": "published",
        "event_id": 42,
    }


@pytest.mark.asyncio
async def test_signal_review_is_side_effect_free_and_separates_contract_from_runtime_proof():
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
    db = _db_with_session(session)

    with patch.object(admin_route, "_check_auth"), patch.object(admin_route, "_db_manager", db):
        body = await admin_route.review_kbd_signals(MagicMock(), 9)

    assert body["publishable"] is True
    assert body["runtime_verified"] is False
    assert body["warning_count"] == 0
    assert body["issues"] == []
    assert body["signal_review"]["feature"] == "expert"
    assert body["signal_review"]["status"] == "passed"
    assert body["platform_status"][0]["code"] == "CAPABILITY_RUNTIME_UNVERIFIED"
    assert body["platform_status"][0]["expert_action_required"] is False
    session.commit.assert_not_awaited()


async def test_approval_freezes_a_distinct_expert_revision_even_when_payload_is_unchanged():
    session = AsyncMock()
    # 审批冻结现在同时记录结构化 review metadata，因此测试替身也必须具备完整
    # 可审核 payload，而不是只提供版本指针。
    kbd_payload = {field: "" for field in KBD_PAYLOAD_FIELDS}
    kbd_payload.update({"signals_json": {"schema_version": 2, "signals": []}, "images_json": []})
    kbd = SimpleNamespace(
        id=9,
        latest_proposal_revision_id=1,
        working_revision_id=2,
        entry_metadata={},
        **kbd_payload,
    )
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
    assert ensure.await_args.kwargs["review_metadata"]["review_state"] == "approved"
