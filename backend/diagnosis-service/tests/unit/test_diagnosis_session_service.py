"""诊断会话应用服务测试。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.auth import ActorContext
from app.domain.session_state import DiagnosisSessionStatus
from app.errors import DiagnosisError
from app.repositories.diagnosis_session_repository import IdempotentCreateResult
from app.schemas.diagnosis_session import DiagnosisSessionCreate
from app.services.diagnosis_session_service import DiagnosisSessionService


class AllowCaseAuthorizer:
    """测试用工单授权器。"""

    async def assert_access(self, _actor, _case_id):
        """允许测试请求访问工单。"""


class AllowScenarioAvailability:
    """测试用采集画像可用性校验器。"""

    async def assert_scenario_available(self, _scenario):
        """允许测试请求使用场景。"""


class ObjectRequiredScenarioAvailability:
    async def assert_scenario_available(self, _scenario):
        return make_profile_for_gate("affected_object")


class NodeOnlyScenarioAvailability:
    async def assert_scenario_available(self, _scenario):
        return make_profile_for_gate("source_node")


def make_profile_for_gate(target_scope: str):
    from app.schemas.collection_profile import CollectionProfileDefinition

    return CollectionProfileDefinition.model_validate(
        {
            "profile_id": "vm_backup_failed",
            "display_name": "测试画像",
            "scenario": "vm_backup_failed",
            "supported_product_versions": ["7.*"],
            "items": [
                {
                    "collector_id": "test.collector",
                    "display_name": "测试采集",
                    "required_level": "mandatory",
                    "target_scope": target_scope,
                    "reason": "测试",
                }
            ],
        }
    )


def make_service(repository) -> DiagnosisSessionService:
    """构造已接入工单授权器的服务。"""

    return DiagnosisSessionService(
        repository,
        case_authorizer=AllowCaseAuthorizer(),
        scenario_availability=AllowScenarioAvailability(),
    )


def make_command(*, impact_scope: str = "single_vm") -> DiagnosisSessionCreate:
    """构造合法创建命令。"""

    start = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    return DiagnosisSessionCreate.model_validate(
        {
            "case_id": "Q2026072900001",
            "selected_scenario": "vm_backup_failed",
            "incident": {
                "start_time": start,
                "end_time": start + timedelta(minutes=30),
                "timezone": "Asia/Shanghai",
            },
            "affected_objects": [{"type": "vm", "id": "vm-027"}],
            "impact_scope": impact_scope,
            "current_status": "ongoing",
        }
    )


def make_command_without_object() -> DiagnosisSessionCreate:
    payload = make_command().model_dump(mode="json")
    payload["affected_objects"] = [{"type": "execution_node", "source_node": "node-1"}]
    return DiagnosisSessionCreate.model_validate(payload)


def make_actor(*roles: str, tenant_id: str = "tenant-a") -> ActorContext:
    """构造可信操作者。"""

    return ActorContext(
        tenant_id=tenant_id,
        user_id="user-1",
        roles=frozenset(roles or {"customer_admin"}),
    )


def make_entity(**overrides):
    """构造仓储返回实体。"""

    now = datetime.now(UTC)
    values = {
        "session_id": uuid4(),
        "case_id": "Q2026072900001",
        "tenant_id": "tenant-a",
        "created_by": "user-1",
        "product_line": "HCI",
        "selected_scenario": "vm_backup_failed",
        "selected_category": None,
        "resolved_category": None,
        "incident_start_time": now,
        "incident_end_time": now + timedelta(minutes=30),
        "incident_timezone": "Asia/Shanghai",
        "affected_objects": [{"type": "vm", "id": "vm-027"}],
        "impact_scope": "single_vm",
        "incident_status": "ongoing",
        "experimental": False,
        "status": DiagnosisSessionStatus.CREATED,
        "resume_status": None,
        "supplement_count": 0,
        "version": 1,
        "request_hash": "",
        "trace_id": "a" * 32,
        "created_at": now,
        "updated_at": now,
        "failure_code": None,
        "failure_message": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_create_passes_tenant_and_trace_to_repository():
    """创建会话时写入可信租户和 Trace ID。"""

    repository = SimpleNamespace(create_idempotent=AsyncMock())
    service = make_service(repository)
    entity = make_entity()

    async def create(values):
        entity.request_hash = values["request_hash"]
        return IdempotentCreateResult(entity=entity, created=True)

    repository.create_idempotent.side_effect = create

    result = await service.create(
        actor=make_actor("customer_admin"),
        command=make_command(),
        idempotency_key="create-001",
    )

    assert result.created is True
    values = repository.create_idempotent.await_args.args[0]
    assert values["tenant_id"] == "tenant-a"
    assert values["created_by"] == "user-1"
    assert len(values["trace_id"]) == 32
    assert len(values["request_hash"]) == 64


@pytest.mark.asyncio
async def test_object_required_profile_rejects_missing_business_object_id():
    repository = SimpleNamespace(create_idempotent=AsyncMock())
    service = DiagnosisSessionService(
        repository,
        case_authorizer=AllowCaseAuthorizer(),
        scenario_availability=ObjectRequiredScenarioAvailability(),
    )

    with pytest.raises(DiagnosisError) as exc_info:
        await service.create(
            actor=make_actor("customer_admin"),
            command=make_command_without_object(),
            idempotency_key="create-no-object",
        )

    assert exc_info.value.code == "AFFECTED_OBJECT_REQUIRED"
    repository.create_idempotent.assert_not_awaited()


@pytest.mark.asyncio
async def test_node_only_profile_accepts_execution_node_without_business_object_id():
    repository = SimpleNamespace(create_idempotent=AsyncMock())
    service = DiagnosisSessionService(
        repository,
        case_authorizer=AllowCaseAuthorizer(),
        scenario_availability=NodeOnlyScenarioAvailability(),
    )
    entity = make_entity(affected_objects=[{"type": "execution_node", "source_node": "node-1"}])

    async def create(values):
        entity.request_hash = values["request_hash"]
        return IdempotentCreateResult(entity=entity, created=True)

    repository.create_idempotent.side_effect = create
    result = await service.create(
        actor=make_actor("customer_admin"),
        command=make_command_without_object(),
        idempotency_key="create-node-only",
    )

    assert result.created is True
    assert repository.create_idempotent.await_args.args[0]["affected_objects"] == [
        {"type": "execution_node", "id": None, "name": None, "source_node": "node-1"}
    ]


@pytest.mark.asyncio
async def test_idempotent_replay_returns_existing_entity():
    """相同幂等键和请求体返回原资源。"""

    repository = SimpleNamespace(create_idempotent=AsyncMock())
    service = make_service(repository)
    command = make_command()
    request_hash = service._request_hash(command)
    repository.create_idempotent.return_value = IdempotentCreateResult(
        entity=make_entity(request_hash=request_hash),
        created=False,
    )

    result = await service.create(
        actor=make_actor("customer_admin"),
        command=command,
        idempotency_key="create-001",
    )

    assert result.created is False


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_different_payload_is_rejected():
    """同一幂等键不能复用于不同请求。"""

    repository = SimpleNamespace(create_idempotent=AsyncMock())
    service = make_service(repository)
    repository.create_idempotent.return_value = IdempotentCreateResult(
        entity=make_entity(request_hash="0" * 64),
        created=False,
    )

    with pytest.raises(DiagnosisError) as exc_info:
        await service.create(
            actor=make_actor("customer_admin"),
            command=make_command(impact_scope="cluster"),
            idempotency_key="create-001",
        )

    assert exc_info.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert exc_info.value.http_status == 409


@pytest.mark.asyncio
async def test_unauthorized_role_cannot_create_session():
    """无权限角色不能创建诊断会话。"""

    repository = SimpleNamespace(create_idempotent=AsyncMock())
    service = make_service(repository)

    with pytest.raises(DiagnosisError) as exc_info:
        await service.create(
            actor=make_actor("viewer"),
            command=make_command(),
            idempotency_key="create-001",
        )

    assert exc_info.value.code == "FORBIDDEN"
    repository.create_idempotent.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_tenant_read_is_hidden_as_not_found():
    """跨租户对象读取统一返回不存在。"""

    repository = SimpleNamespace(get_by_id_for_tenant=AsyncMock(return_value=None))
    service = make_service(repository)

    with pytest.raises(DiagnosisError) as exc_info:
        await service.get(actor=make_actor("customer_admin", tenant_id="tenant-b"), session_id=str(uuid4()))

    assert exc_info.value.code == "DIAGNOSIS_SESSION_NOT_FOUND"
    assert exc_info.value.http_status == 404


@pytest.mark.asyncio
async def test_transition_uses_locked_entity_and_increments_version():
    """状态转换通过锁定读取并递增版本。"""

    entity = make_entity()
    repository = SimpleNamespace(
        get_by_id_for_update=AsyncMock(return_value=entity),
        flush=AsyncMock(side_effect=lambda item: item),
    )
    service = make_service(repository)

    updated = await service.transition(
        actor=make_actor("support_engineer"),
        session_id=str(entity.session_id),
        target=DiagnosisSessionStatus.PLAN_READY,
    )

    assert updated.status == DiagnosisSessionStatus.PLAN_READY
    assert updated.version == 2
    repository.get_by_id_for_update.assert_awaited_once()
    repository.flush.assert_awaited_once_with(entity)


@pytest.mark.asyncio
async def test_failed_transition_records_and_restores_resume_status():
    """失败状态保存恢复点，并且只能恢复到原状态。"""

    entity = make_entity(status=DiagnosisSessionStatus.UPLOADING, version=4)
    repository = SimpleNamespace(
        get_by_id_for_update=AsyncMock(return_value=entity),
        flush=AsyncMock(side_effect=lambda item: item),
    )
    service = make_service(repository)
    actor = make_actor("diagnosis_worker")

    await service.transition(
        actor=actor,
        session_id=str(entity.session_id),
        target=DiagnosisSessionStatus.FAILED,
        failure_code="OBJECT_STORE_TIMEOUT",
        failure_message="对象存储超时",
    )
    assert entity.resume_status == DiagnosisSessionStatus.UPLOADING
    assert entity.failure_code == "OBJECT_STORE_TIMEOUT"

    await service.transition(
        actor=actor,
        session_id=str(entity.session_id),
        target=DiagnosisSessionStatus.UPLOADING,
    )
    assert entity.resume_status is None
    assert entity.failure_code is None


@pytest.mark.asyncio
async def test_missing_case_authorizer_is_default_deny():
    """未配置工单授权器时拒绝创建。"""

    repository = SimpleNamespace(create_idempotent=AsyncMock())
    service = DiagnosisSessionService(repository)

    with pytest.raises(DiagnosisError) as exc_info:
        await service.create(
            actor=make_actor("customer_admin"),
            command=make_command(),
            idempotency_key="create-001",
        )

    assert exc_info.value.code == "CASE_AUTHORIZER_UNAVAILABLE"
    repository.create_idempotent.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_supplement_is_rejected():
    """P0 不允许第二次自动补充采集。"""

    entity = make_entity(
        status=DiagnosisSessionStatus.ASSESSING,
        supplement_count=1,
    )
    repository = SimpleNamespace(
        get_by_id_for_update=AsyncMock(return_value=entity),
        flush=AsyncMock(side_effect=lambda item: item),
    )
    service = make_service(repository)

    with pytest.raises(DiagnosisError) as exc_info:
        await service.transition(
            actor=make_actor("support_engineer"),
            session_id=str(entity.session_id),
            target=DiagnosisSessionStatus.SUPPLEMENT_REQUIRED,
        )

    assert exc_info.value.code == "SUPPLEMENT_LIMIT_REACHED"
    repository.flush.assert_not_awaited()
