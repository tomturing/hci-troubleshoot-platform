from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.kbd_entry import KbdEntry
from app.models.kbd_revision import KbdRevision
from app.routes import admin
from app.routes.admin import KbdApproveRequest, KbdUpdateRequest, _patch_maintenance_payload
from app.services.kbd_mutation_guard import PublishedKbdMutationError, require_mutable_kbd
from shared.schemas.signal_generation import current_tool_contract_revision
from shared.schemas.verification_contract import (
    expert_editor_issues,
    normalize_legacy_role_contract,
    reconcile_verification_contract,
)


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _MappingResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


def _payload() -> dict:
    return {
        "support_id": "37150",
        "title": "虚拟机任务失败",
        "problem_description": "任务中心显示失败 ![img:0]",
        "alert_info": "",
        "steps_text": "检查任务详情",
        "root_cause": "镜像被占用",
        "solution": "释放占用后重试",
        "operational_impact": "",
        "is_temporary": "",
        "recommendations": "",
        "signals_json": {
            "schema_version": 2,
            "signals": [
                {
                    "id": "task_failure",
                    "role": "must",
                    "acquire": {"tool": "qkv_task", "args": {"keyword": "虚拟机启动失败"}},
                    "match": None,
                    "orchestrate": {
                        "produces": [{"name": "HOST", "path": "host"}, {"name": "END", "path": "end"}],
                        "requires": [],
                    },
                    "provenance": {"category": "frontend"},
                },
                {
                    "id": "log_failure",
                    "role": "must",
                    "acquire": {
                        "tool": "qfk_log",
                        "args": {
                            "file": "task.log",
                            "time_window": "{{END}}",
                        },
                    },
                    "match": {
                        "type": "keyword",
                        "pattern": ["镜像被占用"],
                        "mode": "or",
                        "expected": True,
                        "extract": {
                            "type": "text",
                            "rows": {"mode": "all"},
                            "cardinality": "all",
                            "source": "stdout",
                            "value_mode": "string",
                        },
                    },
                    "orchestrate": {"produces": [], "requires": ["END"]},
                    "provenance": {"category": "backend"},
                }
            ],
            "generation_metadata": {
                "schema_version": 1,
                "status": "current",
                "source_fingerprint": "1" * 64,
                "prompt_revision": "2" * 64,
                "model_id": "glm-5",
                "tool_contract_revision": "3" * 64,
                "generation_fingerprint": "4" * 64,
            },
        },
        "images_json": [
            {
                "seq": 0,
                "section": "problem_description",
                "desc": "BACKGROUND: 白色\nTYPE: 弹框截图\nFULL_TEXT:\n- 启动失败\nDESCRIPTION:\n失败弹框",
                "evidence": {"quality": {"status": "success"}, "provenance": {"image_sha256": "abc"}},
            }
        ],
        "content_md": "## 问题描述\n\n任务中心显示失败",
        "content_raw": "任务中心显示失败",
        "category_id": "vm-001",
        "ai_category_id": "vm-001",
        "ai_category_conf": 0.9,
        "ai_category_reason": "任务失败",
        "metadata": {},
        "payload_schema_version": 1,
    }


def test_maintenance_patch_updates_image_evidence_without_touching_active_entry():
    original = _payload()
    changed_image = {
        **original["images_json"][0],
        "desc": "BACKGROUND: 白色\nTYPE: 任务截图\nFULL_TEXT:\n- 启动虚拟机失败\nDESCRIPTION:\n任务中心失败记录",
        "evidence": {
            **original["images_json"][0]["evidence"],
            "regions": [{"observed_facts": ["任务状态为失败"], "inferences": []}],
        },
    }

    patched = _patch_maintenance_payload(
        original,
        KbdUpdateRequest(images_json=[changed_image]),
    )

    assert original["images_json"][0]["evidence"]["quality"]["status"] == "success"
    assert patched["images_json"][0]["evidence"]["quality"] == {
        "status": "manual_reviewed",
        "needs_review": False,
        "inference_status": "expert_confirmed",
        "inference_needs_review": False,
    }
    assert patched["images_json"][0]["evidence"]["provenance"]["expert_edited"] is True
    assert "TYPE: 任务截图" in patched["content_md"]
    assert patched["signals_json"]["generation_metadata"]["status"] == "stale"


def test_maintenance_signal_edit_is_validated_and_marked_manual_reviewed():
    original = _payload()
    signals = original["signals_json"] | {
        "signals": [
            original["signals_json"]["signals"][0]
            | {"acquire": {"tool": "qkv_task", "args": {"keyword": "创建虚拟机失败", "is_failed": True}}}
        ]
    }

    patched = _patch_maintenance_payload(original, KbdUpdateRequest(signals_json=signals))

    assert patched["signals_json"]["generation_metadata"]["status"] == "manual_reviewed"
    assert patched["signals_json"]["signals"][0]["orchestrate"]["requires"] == []


def test_expert_delete_context_signal_removes_its_agent_contract_reference():
    """KBD30880：正常 GPU 主机示例不应留下可执行的幽灵 Signal。"""

    original = _payload()
    reference_signal = {
        "id": "sig_004",
        "role": "context",
        "acquire": {
            "tool": "qfk_system",
            "args": {
                "instruction": "参照正常 GPU 主机的配置文件格式",
                "host": "{{HOST}}",
                "container": "host",
                "command": "cat /sf/cfg/gpu_info.ini",
            },
        },
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
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": ["HOST"]},
        "provenance": {"category": "backend"},
    }
    original["signals_json"]["signals"].append(reference_signal)
    original["signals_json"]["verification_contract"] = {
        "schema_version": 1,
        "evidence_policy": {
            "must": ["task_failure", "log_failure"],
            "should": [],
            "exclude": [],
            "context": ["sig_004"],
            "minimum_should": 0,
            "on_missing_must": "inconclusive",
        },
    }

    edited = original["signals_json"] | {
        "signals": [signal for signal in original["signals_json"]["signals"] if signal["id"] != "sig_004"]
    }
    patched = _patch_maintenance_payload(original, KbdUpdateRequest(signals_json=edited))
    policy = patched["signals_json"]["verification_contract"]["evidence_policy"]

    assert [signal["id"] for signal in patched["signals_json"]["signals"]] == ["task_failure", "log_failure"]
    assert policy["context"] == []
    assert policy["must"] == ["task_failure", "log_failure"]
    assert "sig_004" not in {item for values in policy.values() if isinstance(values, list) for item in values}


def test_expert_can_save_working_draft_without_must_and_gets_actionable_issue():
    """工作稿允许逐步编辑；只有发布门禁要求必要证据。"""

    original = _payload()
    edited = original["signals_json"] | {
        "signals": [signal | {"role": "should"} for signal in original["signals_json"]["signals"]]
    }
    patched = _patch_maintenance_payload(original, KbdUpdateRequest(signals_json=edited))

    policy = patched["signals_json"]["verification_contract"]["evidence_policy"]
    assert policy["must"] == []
    assert policy["should"] == ["task_failure", "log_failure"]
    assert expert_editor_issues(patched["signals_json"]) == [
        {
            "code": "NO_MUST_SIGNAL",
            "severity": "error",
            "message": "发布前请至少保留一条“必要证据”。",
            "action": "将一条可执行关键信号的“证据作用”改为“必要证据”，或新增一条必要证据。",
        }
    ]


def test_reconcile_contract_moves_signal_when_expert_changes_role():
    document = {
        "schema_version": 2,
        "signals": [
            {"id": "task", "role": "must", "acquire": {"tool": "qkv_task", "args": {}}},
            {"id": "check", "role": "context", "acquire": {"tool": "qfk_system", "args": {}}},
        ],
        "verification_contract": {
            "schema_version": 1,
            "scope": {"products": ["HCI"]},
            "evidence_policy": {"must": ["task"], "context": ["check"], "minimum_should": 0},
        },
    }
    document["signals"][1]["role"] = "must"
    canonical, impact = reconcile_verification_contract(document)

    policy = canonical["verification_contract"]["evidence_policy"]
    assert policy["must"] == ["task", "check"]
    assert policy["context"] == []
    assert canonical["verification_contract"]["scope"] == {"products": ["HCI"]}
    assert impact["after"] == {"must": 2, "should": 0, "exclude": 0, "context": 0}


def test_legacy_role_contract_conflict_uses_existing_runtime_contract_on_read():
    """KBD40061 类旧数据不能因编辑说明字段而把 should 静默改成 must。"""

    document = {
        "schema_version": 2,
        "signals": [
            {"id": "sig_001", "role": "must", "acquire": {"tool": "qkv_alert", "args": {}}},
            {"id": "sig_002", "role": "must", "acquire": {"tool": "qkv_task", "args": {}}},
        ],
        "verification_contract": {
            "schema_version": 1,
            "evidence_policy": {
                "must": ["sig_001"],
                "should": ["sig_002"],
                "minimum_should": 1,
            },
        },
    }

    normalized, impact = normalize_legacy_role_contract(document)

    assert normalized["signals"][1]["role"] == "should"
    assert normalized["verification_contract"]["evidence_policy"]["should"] == ["sig_002"]
    assert normalized["verification_contract"]["evidence_policy"]["minimum_should"] == 1
    assert impact == {"normalized_signal_ids": ["sig_002"], "count": 1}
    assert document["signals"][1]["role"] == "must"


@pytest.mark.asyncio
async def test_published_mutation_guard_rejects_final_write_race():
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(id=9, status="published")
    session.execute.return_value = result

    with pytest.raises(PublishedKbdMutationError):
        await require_mutable_kbd(session, 9, for_update=True)

    statement = session.execute.await_args.args[0]
    assert "FOR UPDATE" in str(statement.compile())


@pytest.mark.asyncio
async def test_draft_mutation_guard_returns_entry():
    session = AsyncMock()
    entry = KbdEntry(id=9, support_id="37150", title="草稿", status="draft")
    result = MagicMock()
    result.scalar_one_or_none.return_value = entry
    session.execute.return_value = result

    assert await require_mutable_kbd(session, 9) is entry


def _published_entry() -> KbdEntry:
    payload = _payload()
    entry = KbdEntry(id=9, status="published", lock_version=4, working_revision_id=21)
    for field, value in payload.items():
        if field == "metadata":
            entry.entry_metadata = value
        elif hasattr(entry, field):
            setattr(entry, field, value)
    return entry


@pytest.mark.asyncio
async def test_maintenance_save_updates_only_revision_payload_and_working_pointer():
    """保存维护稿不得覆盖 Agent 当前使用的 kbd_entry 内容。"""

    entry = _published_entry()
    active_title = entry.title
    working = KbdRevision(
        id=21,
        kbd_entry_id=9,
        revision_no=2,
        revision_type="expert",
        payload_json=_payload(),
        checksum="a" * 64,
        generation_metadata={"origin": "admin_maintenance"},
    )
    saved_payload = _payload() | {"title": "专家维护后的标题"}
    saved = SimpleNamespace(
        id=22,
        revision_no=3,
        revision_type="expert",
        parent_revision_id=21,
        payload_json=saved_payload,
        checksum="b" * 64,
        generation_metadata={"origin": "admin_maintenance"},
        actor_id=None,
        actor_type="expert",
        validation_summary={"status": "saved"},
        created_at=None,
    )
    session = AsyncMock()
    session.get.side_effect = [entry, working]
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))

    with (
        patch.object(admin, "_check_auth"),
        patch.object(admin, "_db_manager", db),
        patch.object(admin, "ensure_kbd_revision_payload", AsyncMock(return_value=saved)) as ensure,
        patch.object(admin, "_publish_kbd_revision", AsyncMock()) as publish,
    ):
        response = await admin.update_kbd_maintenance_working(
            MagicMock(),
            9,
            KbdUpdateRequest(title="专家维护后的标题", lock_version=4),
        )

    assert entry.title == active_title
    assert entry.working_revision_id == 21
    assert ensure.await_args.kwargs["payload"]["title"] == "专家维护后的标题"
    assert response["payload"]["title"] == "专家维护后的标题"
    assert response["agent_active_unchanged"] is True
    publish.assert_not_awaited()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_published_detail_overlays_maintenance_payload_without_changing_status():
    """专家详情读维护稿，状态与 Agent active 仍保持 published。"""

    row = {
        "id": 9,
        "support_id": "37150",
        "title": "Agent 当前标题",
        "problem_description": "任务失败",
        "alert_info": "",
        "steps_text": "旧步骤",
        "root_cause": "旧根因",
        "solution": "旧方案",
        "operational_impact": "",
        "is_temporary": "",
        "recommendations": "",
        "signals_json": _payload()["signals_json"],
        "content_md": "旧内容",
        "content_raw": "旧内容",
        "images_json": [],
        "metadata": {},
        "category_id": "vm-001",
        "ai_category_id": "vm-001",
        "ai_category_conf": 0.9,
        "ai_category_reason": "任务失败",
        "status": "published",
        "reviewer_id": 1,
        "review_note": "已发布",
        "latest_proposal_revision_id": 1,
        "working_revision_id": 21,
        "lock_version": 4,
        "created_at": None,
        "updated_at": None,
        "published_at": None,
    }
    working = SimpleNamespace(
        payload_json=_payload() | {"title": "维护工作稿标题", "steps_text": "新步骤"},
        generation_metadata={"origin": "admin_maintenance"},
    )
    session = AsyncMock()
    session.execute.return_value = _MappingResult(row)
    session.get.return_value = working
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))

    with patch.object(admin, "_check_auth"), patch.object(admin, "_db_manager", db):
        response = await admin.get_kbd_entry_detail(MagicMock(), 9)

    assert response["title"] == "维护工作稿标题"
    assert response["steps_text"] == "新步骤"
    assert response["status"] == "published"
    assert response["maintenance_working"] is True
    assert response["review_view"] == "maintenance_working"


@pytest.mark.asyncio
async def test_maintenance_publish_applies_payload_before_atomic_runtime_switch():
    """显式发布时先应用通过校验的 payload，再在同一事务切换 runtime active。"""

    entry = _published_entry()
    working_payload = _payload() | {"title": "已复核维护版", "content_md": "## 问题描述\n任务失败"}
    working = SimpleNamespace(
        id=21,
        payload_json=working_payload,
        generation_metadata={"origin": "admin_maintenance"},
    )
    read_session = AsyncMock()
    read_session.get.side_effect = [entry, working]
    write_session = AsyncMock()
    write_session.get.side_effect = [entry, working]
    sessions = iter([read_session, write_session])
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(next(sessions)))
    approved = SimpleNamespace(
        id=23,
        revision_no=4,
        revision_type="expert",
        parent_revision_id=21,
        checksum="c" * 64,
        actor_id=None,
        actor_type="expert",
        validation_summary={"status": "passed"},
        created_at=None,
    )

    async def publish_after_payload_applied(session, kbd_id, trace_id):
        assert session is write_session
        assert kbd_id == 9
        assert entry.title == "已复核维护版"
        assert entry.working_revision_id == 23
        validation = entry.signals_json["publish_validation"]
        assert validation["status"] == "passed"
        assert validation["tool_contract_revision"] == current_tool_contract_revision()
        assert entry.signals_json["generation_metadata"]["tool_contract_revision"] == "3" * 64
        return {"revision": 8, "checksum": "d" * 64}

    async def freeze_approved_revision(*args, **kwargs):
        entry.working_revision_id = approved.id
        return approved

    with (
        patch.object(admin, "_check_auth"),
        patch.object(admin, "_db_manager", db),
        patch.object(admin, "_embedding_service", None),
        patch.object(admin, "segment", return_value="任务 失败"),
        patch.object(admin, "ensure_kbd_revision_payload", AsyncMock(side_effect=freeze_approved_revision)),
        patch.object(admin, "_publish_kbd_revision", AsyncMock(side_effect=publish_after_payload_applied)) as publish,
    ):
        response = await admin.publish_kbd_maintenance_working(
            MagicMock(),
            9,
            KbdApproveRequest(reviewer_id=7, review_note="专家确认", lock_version=4),
        )

    assert response.success is True
    assert response.resource_revision["revision"] == 8
    assert entry.title == "已复核维护版"
    assert entry.working_revision_id is None
    publish.assert_awaited_once()
    write_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_maintenance_publish_lock_conflict_does_not_switch_runtime_active():
    """工作稿版本冲突必须在发布器调用前失败，旧 active 保持不变。"""

    entry = _published_entry()
    working = SimpleNamespace(
        id=21,
        payload_json=_payload(),
        generation_metadata={"origin": "admin_maintenance"},
    )
    session = AsyncMock()
    session.get.side_effect = [entry, working]
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))

    with (
        patch.object(admin, "_check_auth"),
        patch.object(admin, "_db_manager", db),
        patch.object(admin, "_publish_kbd_revision", AsyncMock()) as publish,
    ):
        with pytest.raises(admin.HTTPException) as exc_info:
            await admin.publish_kbd_maintenance_working(
                MagicMock(),
                9,
                KbdApproveRequest(reviewer_id=7, lock_version=3),
            )

    assert exc_info.value.status_code == 409
    assert entry.title == "虚拟机任务失败"
    assert entry.working_revision_id == 21
    publish.assert_not_awaited()
