"""诊断会话 API 契约测试。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.auth import ActorContext, require_actor
from app.dependencies import get_session_service
from app.errors import DiagnosisError, register_error_handlers
from app.routes.diagnosis_sessions import router
from app.services.diagnosis_session_service import SessionCreateResult
from fastapi import FastAPI
from fastapi.testclient import TestClient


def make_entity():
    """构造 API 响应实体。"""

    now = datetime.now(UTC)
    return SimpleNamespace(
        session_id=uuid4(),
        case_id="Q2026072900001",
        tenant_id="tenant-a",
        created_by="user-1",
        product_line="HCI",
        selected_scenario="vm_backup_failed",
        selected_category="虚拟机备份与CDP",
        resolved_category=None,
        incident_start_time=now,
        incident_end_time=now + timedelta(minutes=30),
        incident_timezone="Asia/Shanghai",
        affected_objects=[{"type": "vm", "id": "vm-027"}],
        impact_scope="single_vm",
        incident_status="ongoing",
        experimental=False,
        status="created",
        supplement_count=0,
        version=1,
        trace_id="a" * 32,
        created_at=now,
        updated_at=now,
    )


def make_payload() -> dict:
    """构造 API 请求体。"""

    start = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    return {
        "case_id": "Q2026072900001",
        "product_line": "HCI",
        "selected_scenario": "vm_backup_failed",
        "selected_category": "虚拟机备份与CDP",
        "incident": {
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=30)).isoformat(),
            "timezone": "Asia/Shanghai",
        },
        "affected_objects": [{"type": "vm", "id": "vm-027"}],
        "impact_scope": "single_vm",
        "current_status": "ongoing",
    }


def build_app(service, *, with_actor: bool = True) -> FastAPI:
    """构造只包含诊断路由的测试应用。"""

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_session_service] = lambda: service
    if with_actor:
        app.dependency_overrides[require_actor] = lambda: ActorContext(
            tenant_id="tenant-a",
            user_id="user-1",
            roles=frozenset({"customer_admin"}),
        )
    return app


def test_create_api_returns_idempotency_and_version_headers():
    """创建接口返回幂等和资源版本响应头。"""

    entity = make_entity()

    class Service:
        async def create(self, **_kwargs):
            return SessionCreateResult(entity=entity, created=True)

    client = TestClient(build_app(Service()))
    response = client.post(
        "/api/diagnosis-sessions",
        json=make_payload(),
        headers={"Idempotency-Key": "create-001"},
    )

    assert response.status_code == 201
    assert response.headers["Idempotent-Replayed"] == "false"
    assert response.headers["ETag"] == '"1"'
    assert response.json()["tenant_id"] == "tenant-a"


def test_missing_idempotency_key_uses_unified_error_contract():
    """缺失幂等键返回统一参数错误。"""

    client = TestClient(build_app(SimpleNamespace()))
    response = client.post("/api/diagnosis-sessions", json=make_payload())

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["retryable"] is False
    assert "trace_id" in error


def test_identity_provider_is_default_deny():
    """未注入身份验证器时业务接口默认拒绝。"""

    app = build_app(SimpleNamespace(), with_actor=False)
    app.state.identity_verifier = None
    client = TestClient(app)
    response = client.post(
        "/api/diagnosis-sessions",
        json=make_payload(),
        headers={"Idempotency-Key": "create-001"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "IDENTITY_PROVIDER_UNAVAILABLE"


def test_unavailable_profile_error_is_json_serializable():
    """场景画像不可用时也必须符合统一 JSON 错误契约。"""

    class Service:
        async def create(self, **_kwargs):
            raise DiagnosisError(
                code="COLLECTION_PROFILE_NOT_AVAILABLE",
                message="所选离线诊断场景尚未发布或已停用，请刷新后重新选择",
                http_status=409,
            )

    client = TestClient(build_app(Service()))
    payload = make_payload()
    payload["selected_scenario"] = "unknown_scene"
    response = client.post(
        "/api/diagnosis-sessions",
        json=payload,
        headers={"Idempotency-Key": "create-001"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COLLECTION_PROFILE_NOT_AVAILABLE"
