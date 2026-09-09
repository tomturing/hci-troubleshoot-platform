from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.routes.kbd_search import SemanticEntryResolveRequest, resolve_semantic_entry


@pytest.mark.asyncio
@pytest.mark.parametrize("category_id", [None, "", "   "])
async def test_admin_preview_requires_confirmed_category(monkeypatch, category_id):
    from app.routes import admin

    monkeypatch.setattr(admin, "_check_auth", lambda _: None)
    monkeypatch.setattr(admin, "get_kbd_entry_detail", AsyncMock(return_value={
        "id": 1, "category_id": category_id, "ai_category_id": "vm",
        "signals_json": {"schema_version": 2, "signals": []},
    }))
    resolver = AsyncMock()
    monkeypatch.setattr("shared.schemas.semantic_routing.resolve_candidates", resolver)
    with pytest.raises(HTTPException) as error:
        await admin.preview_semantic_profile(
            MagicMock(), 1, admin.SemanticPreviewRequest(profile={}, context={"description": "启动失败"})
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "SEMANTIC_PREVIEW_CATEGORY_REQUIRED"
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_preview_returns_guidance_and_literal_sources_without_saving(monkeypatch):
    import shared.dynamic_resource.loader as loader_module
    from app.routes import admin

    profile = {
        "schema_version": 1,
        "diagnosis_capability": "guidance_only",
        "canonical_symptoms": ["客户机蓝屏"],
        "positive_anchors": ["蓝屏"],
        "manual_evidence_request": ["请提供错误文字"],
    }
    detail = {
        "id": 1,
        "category_id": "vm",
        "problem_description": "客户机蓝屏",
        "signals_json": {"schema_version": 2, "signals": []},
    }
    monkeypatch.setattr(admin, "_check_auth", lambda _: None)
    monkeypatch.setattr(admin, "get_kbd_entry_detail", AsyncMock(return_value=detail))
    session = AsyncMock()
    session.__aenter__.return_value = session
    monkeypatch.setattr(admin, "_db_manager", SimpleNamespace(async_session_factory=lambda: session))
    monkeypatch.setattr(admin, "_embedding_service", None)
    monkeypatch.setattr(
        loader_module, "DynamicResourceLoader", lambda _: SimpleNamespace(list_active=AsyncMock(return_value=[]))
    )
    result = await admin.preview_semantic_profile(
        MagicMock(), 1, admin.SemanticPreviewRequest(profile=profile, context={"description": "客户机蓝屏"})
    )
    assert result["preview_only"] is True
    assert result["reason"] == "guidance_only"
    assert result["source_evidence"][0]["quote"] == "客户机蓝屏"
    assert detail["signals_json"]["signals"] == []
    session.commit.assert_not_called()


from fastapi import HTTPException


@pytest.mark.asyncio
async def test_semantic_resolver_refuses_fallback_when_strong_source_is_unavailable():
    response = await resolve_semantic_entry(
        SemanticEntryResolveRequest(
            category_id="存储-001",
            case_context="备份池空间不足",
            strong_producer_status="source_unavailable",
        )
    )
    assert response["decision"] == "strong_producer_first"
    assert response["reason"] == "strong_producer_unavailable"
    assert response["candidates"] == []
    assert response["reason_text"]


@pytest.mark.asyncio
async def test_resolver_uses_frozen_profile_and_audits_exact_revision(monkeypatch):
    from app.routes import kbd_search, playbooks

    document = {
        "signals": [{"acquire": {"tool": "qkv_case_context"}}],
        "semantic_entry_profile": {
            "schema_version": 1,
            "diagnosis_capability": "executable",
            "canonical_symptoms": ["镜像格式不支持"],
            "positive_anchors": ["镜像格式不支持"],
            "exclusion_anchors": [],
        },
    }
    frozen = SimpleNamespace(
        status="published",
        revision=8,
        content={"id": 1, "category_id": "vm", "status": "published", "signals_json": document},
        checksum="frozen",
    )
    loader = MagicMock()
    loader.get_active = AsyncMock(return_value=frozen)
    loader.get_revision = AsyncMock(return_value=frozen)
    loader.audit_usage = AsyncMock()
    session = AsyncMock()
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [
        SimpleNamespace(id=1, signals_json={"semantic_entry_profile": {"positive_anchors": ["错误工作稿"]}})
    ]
    session.execute.return_value = rows
    session.__aenter__.return_value = session
    monkeypatch.setattr(kbd_search, "_db_manager", SimpleNamespace(async_session_factory=lambda: session))
    monkeypatch.setattr(kbd_search, "_embedding_service", None)
    monkeypatch.setattr(kbd_search, "DynamicResourceLoader", lambda session: loader)
    monkeypatch.setattr(kbd_search, "snapshot_revision_metadata", lambda value: {"revision": value.revision})
    monkeypatch.setattr(playbooks, "_execution_issues", lambda *args, **kwargs: [])
    response = await resolve_semantic_entry(
        SemanticEntryResolveRequest(
            category_id="vm",
            case_context="镜像格式不支持",
            strong_producer_status="not_applicable",
            expected_revisions={"1": 8},
        )
    )
    assert response["decision"] == "executable"
    assert response["candidates"][0]["resource_revision"] == {"revision": 8}
    loader.get_revision.assert_awaited_once_with("kbd", "1", 8)
    loader.audit_usage.assert_awaited_once()
    with pytest.raises(HTTPException) as stale:
        await resolve_semantic_entry(
            SemanticEntryResolveRequest(
                category_id="vm",
                case_context="镜像格式不支持",
                strong_producer_status="not_applicable",
                expected_revisions={"1": 7},
            )
        )
    assert stale.value.status_code == 409
    frozen.status = "disabled"
    response = await resolve_semantic_entry(
        SemanticEntryResolveRequest(
            category_id="vm", case_context="镜像格式不支持", strong_producer_status="not_applicable"
        )
    )
    assert response["candidates"] == []
