"""客户侧 Collection Profile（采集画像）场景 API 契约测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.auth import ActorContext, require_actor
from app.dependencies import get_collection_profile_service
from app.errors import DiagnosisError
from app.routes.collection_profiles import scenario_router
from app.schemas.collection_profile import OfflineScenarioOptionResponse
from app.services.collection_profile_service import CollectionProfileService
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_customer_scenario_api_only_returns_safe_profile_metadata():
    """客户接口只返回场景显示信息和版本，不暴露 Collector 命令。"""

    class Service:
        async def list_available_scenarios(self, **_kwargs):
            return [
                OfflineScenarioOptionResponse(
                    scenario="vm_backup_failed",
                    display_name="虚拟机备份失败采集画像",
                    profile_revision=7,
                    profile_version="1.0.7",
                    supported_product_versions=["7.*"],
                    requires_affected_object=True,
                )
            ]

    app = FastAPI()
    app.include_router(scenario_router)
    app.dependency_overrides[get_collection_profile_service] = lambda: Service()
    app.dependency_overrides[require_actor] = lambda: ActorContext(
        tenant_id="tenant-a",
        user_id="customer-1",
        roles=frozenset({"customer_admin"}),
    )

    response = TestClient(app).get("/api/diagnosis-scenarios")

    assert response.status_code == 200
    assert response.json() == [
        {
            "scenario": "vm_backup_failed",
            "display_name": "虚拟机备份失败采集画像",
            "profile_revision": 7,
            "profile_version": "1.0.7",
            "supported_product_versions": ["7.*"],
            "requires_affected_object": True,
        }
    ]


@pytest.mark.asyncio
async def test_offline_guidance_reads_published_snapshot_without_creating_collection(monkeypatch):
    profile = {
        "schema_version": 1,
        "diagnosis_capability": "guidance_only",
        "canonical_symptoms": ["客户机蓝屏"],
        "positive_anchors": ["蓝屏"],
        "manual_evidence_request": ["请提供完整错误文字，由人工复核"],
    }
    snapshot = SimpleNamespace(
        status="published",
        resource_name="1",
        revision=3,
        checksum="frozen",
        content={
            "id": 1,
            "title": "蓝屏方向",
            "category_id": "vm",
            "signals_json": {"semantic_entry_profile": profile, "signals": [{"acquire": {"tool": "qkv_case_context"}}]},
        },
    )
    loader = SimpleNamespace(list_active=AsyncMock(return_value=[snapshot]))
    monkeypatch.setattr("app.services.collection_profile_service.DynamicResourceLoader", lambda _: loader)
    session = AsyncMock()
    service = CollectionProfileService(session)
    actor = ActorContext(tenant_id="t", user_id="u", roles=frozenset({"customer_admin"}))
    assert await service.semantic_advice(actor=actor) == [{"category_id": "vm", "display_name": "vm"}]
    result = await service.semantic_advice(actor=actor, category_id="vm", context={"description": "客户机蓝屏"})
    assert result["decision"] == "inconclusive"
    assert result["reason"] == "guidance_only"
    assert result["next_action"]["question"] == "请提供完整错误文字，由人工复核"
    session.execute.assert_not_called()
    snapshot.content["signals_json"]["signals"].append({"acquire": {"tool": "qkv_task"}})
    result = await service.semantic_advice(actor=actor, category_id="vm", context={"description": "客户机蓝屏"})
    assert result["decision"] == "strong_producer_first"
    snapshot.status = "disabled"
    assert await service.semantic_advice(actor=actor) == []
    with pytest.raises(DiagnosisError):
        await service.semantic_advice(
            actor=ActorContext(tenant_id="t", user_id="u", roles=frozenset({"unrelated_role"}))
        )
