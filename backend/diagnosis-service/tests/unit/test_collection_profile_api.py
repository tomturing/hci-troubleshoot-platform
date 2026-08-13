"""客户侧 Collection Profile（采集画像）场景 API 契约测试。"""

from app.auth import ActorContext, require_actor
from app.dependencies import get_collection_profile_service
from app.routes.collection_profiles import scenario_router
from app.schemas.collection_profile import OfflineScenarioOptionResponse
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
