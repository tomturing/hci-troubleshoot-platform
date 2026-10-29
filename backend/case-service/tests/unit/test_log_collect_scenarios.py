"""日志收集「故障场景」字典端点单测（无需数据库）。

这张表承载的是一条容易走偏的边界：**字典只改文案，不改能力**。
- 客户读只出已启用项（停用场景 = 平台已下线的能力，露出去就是虚假承诺）；
- 管理端必须能看见停用项，否则运营无从把下线的场景重新打开；
- ``value`` 是 acli 的枚举值，写入后不可改（历史任务里存的就是它）；
- 每次写都要留审计，否则"谁把某场景的说明改成这样"无从追查。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.main import app
from app.routes.log_collect_scenarios import get_scenario_service
from app.schemas.log_collect_scenario import (
    LogCollectScenarioCreate,
    LogCollectScenarioResponse,
    LogCollectScenarioUpdate,
)
from app.security.auth import get_client_id
from app.services import log_collect_scenario_service as service_module
from app.services.log_collect_scenario_service import (
    LogCollectScenarioNotFoundError,
    LogCollectScenarioService,
)
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.unit

_ADMIN_HEADERS = {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}


def _row(
    value: str,
    *,
    label: str,
    enabled: bool = True,
    exclusive: bool = False,
    sort_order: int = 100,
) -> LogCollectScenarioResponse:
    now = datetime(2026, 10, 29, 10, 0, 0, tzinfo=UTC)
    return LogCollectScenarioResponse(
        value=value,
        label=label,
        summary=f"{label}的一句话释义",
        collects=None,
        typical_symptoms=None,
        duration_hint=None,
        exclusive=exclusive,
        sort_order=sort_order,
        enabled=enabled,
        review_state="pending_review",
        updated_by=None,
        created_at=now,
        updated_at=now,
    )


class _StubService:
    """替身服务：绕开数据库，只验证路由的过滤口径与审计行为。"""

    def __init__(self, scenarios: list[LogCollectScenarioResponse] | None = None):
        self.scenarios = scenarios or []
        self.list_calls: list[bool] = []
        self.update_calls: list[tuple[str, LogCollectScenarioUpdate, str | None]] = []
        self.create_calls: list[tuple[LogCollectScenarioCreate, str | None]] = []
        self.missing: set[str] = set()

    async def list_scenarios(self, *, include_disabled: bool):
        self.list_calls.append(include_disabled)
        return [item for item in self.scenarios if include_disabled or item.enabled]

    async def create_scenario(self, payload, *, updated_by):
        self.create_calls.append((payload, updated_by))
        return _row(payload.value, label=payload.label, enabled=payload.enabled)

    async def update_scenario(self, value, payload, *, updated_by):
        self.update_calls.append((value, payload, updated_by))
        if value in self.missing:
            raise LogCollectScenarioNotFoundError(value)
        base = next((item for item in self.scenarios if item.value == value), None)
        changes = payload.model_dump(exclude_unset=True)
        return base.model_copy(update=changes) if base else _row(value, label="x")


@pytest.fixture
def audit_writer():
    """接管审计写入：断言调用参数，同时防止真的去连数据库。

    审计在路由模块里以名字绑定的形式引用，所以要 patch 路由模块上的名字。
    """
    writer = AsyncMock()
    fake_db = SimpleNamespace(async_session_factory=object())
    with patch("app.routes.log_collect_scenarios.write_operation_audit", writer), patch(
        "app.routes.log_collect_scenarios.database_manager", fake_db, create=True
    ):
        yield writer


async def _client_with(stub: _StubService) -> AsyncClient:
    app.dependency_overrides[get_scenario_service] = lambda: stub
    app.dependency_overrides[get_client_id] = lambda: "client-a"
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_client_list_only_returns_enabled_items():
    stub = _StubService(
        [
            _row("other", label="不收集上述场景日志", exclusive=True, sort_order=10),
            _row("vm_suspend", label="虚拟机挂起专项", sort_order=20),
            _row("retired", label="已下线场景", enabled=False, sort_order=30),
        ]
    )
    client = await _client_with(stub)
    try:
        resp = await client.get("/api/log-collect/scenarios")
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_scenario_service, None)
        app.dependency_overrides.pop(get_client_id, None)

    assert resp.status_code == 200
    values = [item["value"] for item in resp.json()["scenarios"]]
    assert values == ["other", "vm_suspend"]
    # 客户读必须显式要求"排除停用"，不能靠前端过滤
    assert stub.list_calls == [False]


@pytest.mark.asyncio
async def test_admin_list_includes_disabled_items():
    stub = _StubService(
        [
            _row("other", label="不收集上述场景日志"),
            _row("retired", label="已下线场景", enabled=False),
        ]
    )
    client = await _client_with(stub)
    try:
        resp = await client.get("/internal/log-collect/scenarios", headers=_ADMIN_HEADERS)
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_scenario_service, None)
        app.dependency_overrides.pop(get_client_id, None)

    assert resp.status_code == 200
    assert [item["value"] for item in resp.json()["scenarios"]] == ["other", "retired"]
    assert stub.list_calls == [True]


@pytest.mark.asyncio
async def test_admin_endpoints_require_admin_token():
    stub = _StubService([_row("other", label="不收集上述场景日志")])
    client = await _client_with(stub)
    try:
        read = await client.get("/internal/log-collect/scenarios")
        write = await client.put(
            "/internal/log-collect/scenarios/other",
            json={"summary": "改个释义"},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_scenario_service, None)
        app.dependency_overrides.pop(get_client_id, None)

    assert read.status_code == 403
    assert write.status_code == 403
    # 没有凭证时不能有任何写动作
    assert stub.update_calls == []


def test_update_payload_cannot_carry_value():
    """value 是 acli 枚举值，改它等于换场景，历史任务就无法追溯了。"""
    assert "value" not in LogCollectScenarioUpdate.model_fields
    assert "value" not in service_module._UPDATABLE_COLUMNS


@pytest.mark.asyncio
async def test_update_ignores_value_in_body(audit_writer):
    stub = _StubService([_row("other", label="不收集上述场景日志")])
    client = await _client_with(stub)
    try:
        resp = await client.put(
            "/internal/log-collect/scenarios/other",
            headers=_ADMIN_HEADERS,
            json={"value": "hijacked", "summary": "只收基础系统日志"},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_scenario_service, None)
        app.dependency_overrides.pop(get_client_id, None)

    assert resp.status_code == 200
    assert resp.json()["value"] == "other"
    value, payload, _ = stub.update_calls[0]
    assert value == "other"
    # 请求体里的 value 被模型直接忽略，不会进 SET 子句
    assert "value" not in payload.model_dump(exclude_unset=True)


@pytest.mark.asyncio
async def test_update_writes_audit_with_operator(audit_writer):
    stub = _StubService([_row("other", label="不收集上述场景日志")])
    client = await _client_with(stub)
    try:
        resp = await client.put(
            "/internal/log-collect/scenarios/other",
            headers={**_ADMIN_HEADERS, "X-Admin-User": "ops-zhang"},
            json={"summary": "只收基础系统日志", "exclusive": True},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_scenario_service, None)
        app.dependency_overrides.pop(get_client_id, None)

    assert resp.status_code == 200
    audit_writer.assert_awaited_once()
    kwargs = audit_writer.await_args.kwargs
    assert kwargs["action"] == "log_collect.scenario.update"
    assert kwargs["resource_type"] == "log_collect_scenario"
    assert kwargs["resource_id"] == "other"
    assert kwargs["details"] == {
        "summary": "只收基础系统日志",
        "exclusive": True,
        "updated_by": "ops-zhang",
    }


@pytest.mark.asyncio
async def test_update_missing_scenario_is_404(audit_writer):
    stub = _StubService([])
    stub.missing.add("ghost")
    client = await _client_with(stub)
    try:
        resp = await client.put(
            "/internal/log-collect/scenarios/ghost",
            headers=_ADMIN_HEADERS,
            json={"summary": "无所谓"},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_scenario_service, None)
        app.dependency_overrides.pop(get_client_id, None)

    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]
    # 没写成功就不该留下审计（否则审计里会出现"改过但没改"的幽灵记录）
    audit_writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_conflict_returns_409(audit_writer):
    from sqlalchemy.exc import IntegrityError

    stub = _StubService([])
    stub.create_scenario = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("duplicate key")))
    client = await _client_with(stub)
    try:
        resp = await client.post(
            "/internal/log-collect/scenarios",
            headers=_ADMIN_HEADERS,
            json={"value": "other", "label": "不收集上述场景日志"},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_scenario_service, None)
        app.dependency_overrides.pop(get_client_id, None)

    assert resp.status_code == 409
    assert "other" in resp.json()["detail"]
    # 唯一约束冲突没写成，不能留审计
    audit_writer.assert_not_awaited()


def test_create_rejects_blank_label():
    """短的 label 是勾选框第一行，空白等于给用户一个无名选项。"""
    with pytest.raises(ValueError):
        LogCollectScenarioCreate(value="other", label="   ")


def test_create_strips_label_and_normalizes_blank_summary():
    payload = LogCollectScenarioCreate(value="other", label="  不收集上述场景日志  ", summary="  ")
    assert payload.label == "不收集上述场景日志"
    assert payload.summary is None


def test_update_tolerates_absent_label():
    """部分更新：不提交 label 时不应被校验拦住。"""
    payload = LogCollectScenarioUpdate(summary="只收基础系统日志")
    assert payload.label is None
    assert payload.model_dump(exclude_unset=True) == {"summary": "只收基础系统日志"}


class _FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        return self._rows[0]


class _FakeSession:
    """记录 SQL 与绑定参数，用于验证过滤/排序/白名单逻辑。"""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.statements: list[str] = []
        self.params: list[dict] = []

    async def execute(self, statement, params=None):  # noqa: ANN001
        self.statements.append(str(statement))
        self.params.append(params or {})
        return _FakeResult(self._rows)


def _mapping_row(value: str, label: str, *, enabled: bool = True) -> dict:
    now = datetime(2026, 10, 29, 10, 0, 0, tzinfo=UTC)
    return {
        "value": value,
        "label": label,
        "summary": None,
        "collects": None,
        "typical_symptoms": None,
        "duration_hint": None,
        "exclusive": value == "other",
        "sort_order": 10,
        "enabled": enabled,
        "review_state": "pending_review",
        "updated_by": None,
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_service_filters_and_sorts_on_client_read():
    session = _FakeSession([_mapping_row("other", "不收集上述场景日志")])
    service = LogCollectScenarioService(session)

    items = await service.list_scenarios(include_disabled=False)

    sql = session.statements[0]
    # 过滤必须在库里做：靠前端过滤等于把停用项也发给了客户
    assert "WHERE enabled" in sql
    assert "ORDER BY sort_order, value" in sql
    assert [item.value for item in items] == ["other"]


@pytest.mark.asyncio
async def test_service_admin_read_has_no_enabled_filter():
    session = _FakeSession([_mapping_row("retired", "已下线场景", enabled=False)])
    service = LogCollectScenarioService(session)

    items = await service.list_scenarios(include_disabled=True)

    sql = session.statements[0]
    assert "WHERE" not in sql
    assert items[0].enabled is False


@pytest.mark.asyncio
async def test_service_update_rejects_empty_patch():
    session = _FakeSession([])
    service = LogCollectScenarioService(session)

    with pytest.raises(ValueError, match="没有需要更新的字段"):
        await service.update_scenario("other", LogCollectScenarioUpdate(), updated_by="ops")

    assert session.statements == []


@pytest.mark.asyncio
async def test_service_update_only_sets_given_columns():
    session = _FakeSession([_mapping_row("other", "不收集上述场景日志")])
    service = LogCollectScenarioService(session)

    await service.update_scenario(
        "other",
        LogCollectScenarioUpdate(summary="只收基础系统日志"),
        updated_by="ops",
    )

    sql = session.statements[0]
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    # 只改 summary：全量覆盖会把没提交的采集说明清空
    assert "summary = :summary" in set_clause
    assert "collects" not in set_clause
    assert "typical_symptoms" not in set_clause
    assert session.params[0] == {"summary": "只收基础系统日志", "value": "other", "updated_by": "ops"}
