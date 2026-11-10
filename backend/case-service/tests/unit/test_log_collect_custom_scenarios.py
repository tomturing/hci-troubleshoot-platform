"""日志收集「自定义场景」端点与序列化单测（无需数据库）。

三条必须锁死的契约：

1. **客户永远拿不到 items**：路径清单是客户目录结构，也是前端可篡改的注入面；
2. **下发内容只有 paths**：acli 的 config 里其它字段因 ``-m`` 的 default 永不生效，
   写进去只会误导后来人；
3. **非法路径必须在校验层被拒绝**：插件把路径直接拼进 ``cp -r`` 且未加引号。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.config import settings
from app.main import app
from app.routes.log_collect_custom_scenarios import get_service
from app.schemas.log_collect_custom_scenario import (
    CustomScenarioItem,
    LogCollectCustomScenarioCreate,
    LogCollectCustomScenarioResponse,
    LogCollectCustomScenarioUpdate,
)
from app.security.auth import get_client_id
from app.services.log_collect_custom_scenario_service import (
    LogCollectCustomScenarioService,
    _serialize,
    serialize_config,
)
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_ADMIN_HEADERS = {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}


def _response(code: str = "cs_demo", *, items: list[str] | None = None) -> LogCollectCustomScenarioResponse:
    now = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
    paths = items or ["/sf/log/cluster/", "/sf/etc/cluster.conf"]
    return LogCollectCustomScenarioResponse(
        scenario_id=uuid4(),
        revision=2,
        code=code,
        label="存储链路完整取证",
        summary="定位卡慢盘",
        items=[CustomScenarioItem(path=path) for path in paths],
        item_count=len(paths),
        config_sha256="a" * 64,
        config_bytes=100,
        source="visual",
        enabled=True,
        sort_order=10,
        review_state="approved",
        created_by="admin",
        updated_by="admin",
        created_at=now,
        updated_at=now,
    )


class _StubService:
    def __init__(self):
        self.rows = [_response(), _response("cs_disabled")]
        self.create_calls: list[tuple[LogCollectCustomScenarioCreate, str | None]] = []
        self.update_calls: list[tuple[str, LogCollectCustomScenarioUpdate, str | None]] = []
        self.deleted: list[str] = []
        self.include_disabled_calls: list[bool] = []

    async def list_latest(self, *, include_disabled: bool):
        self.include_disabled_calls.append(include_disabled)
        return [item for item in self.rows if include_disabled or item.enabled]

    async def create(self, payload, *, actor):
        self.create_calls.append((payload, actor))
        return _response(payload.code)

    async def update(self, code, payload, *, actor):
        self.update_calls.append((code, payload, actor))
        return _response(code)

    async def delete(self, code):
        self.deleted.append(code)

    async def validate(self, payload):
        # 与真实服务同构：真源是 _serialize，通道不匹配时抛 ValueError 由路由转成 issues。
        text, _, size = _serialize([item.model_dump() for item in payload.items], payload.source)
        kind = "spc" if payload.source == "spc" else "config"
        return len(payload.items), size, text, kind


@pytest.fixture
def stub() -> _StubService:
    service = _StubService()
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_client_id] = lambda: "client-a"
    yield service
    app.dependency_overrides.pop(get_service, None)
    app.dependency_overrides.pop(get_client_id, None)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── 序列化契约 ────────────────────────────────────────────────────────────


def test_serialize_config_writes_only_paths():
    text, sha256, size = serialize_config([{"path": "/sf/log/cluster/"}, {"path": "/sf/etc/cluster.conf"}])
    assert text == '{"paths":"/sf/log/cluster/,/sf/etc/cluster.conf"}'
    assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert size == len(text.encode("utf-8"))
    # 其它字段写了也不生效；出现即说明有人在"顺手补充"，必须拦下
    assert "modules" not in text
    assert "version" not in text


def test_serialize_config_is_ascii_safe():
    """acli 侧是 Python2 json.load：转义成 ASCII 可以绕开 BOM/编码校验缺失。"""
    text, _, _ = serialize_config([{"path": "/sf/log/cluster/"}])
    text.encode("ascii")  # 不抛异常即通过


# ── 路径校验 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "relative/path",
        "/sf/a b",  # 空格：cp -r 未加引号，会被拆成两个参数
        "/sf/a,b",  # 逗号：会被拆成两条路径
        "/sf/../etc/shadow",
        "/sf/a;id",
        "/sf/中文目录",
    ],
)
def test_item_rejects_dangerous_paths(path: str):
    with pytest.raises(ValidationError):
        CustomScenarioItem(path=path)


def test_wildcard_allowed_by_schema_but_rejected_for_config():
    # spc 侧由插件 glob，允许 *；但 -c 的 paths 不做 glob，必须在序列化层拒绝。
    item = CustomScenarioItem(path="/sf/a*")
    with pytest.raises(ValueError):
        _serialize([item.model_dump()], "visual")
    _serialize([item.model_dump()], "spc")


def test_serialize_spc_keeps_filters_and_notes():
    item = CustomScenarioItem(
        action="copy",
        path="/sf/log/{date}/*",
        note="1.白盒日志",
        filters=[{"action": "exclude", "match_type": "path", "pattern": "/sf/log/{date}/vs/"}],
    )
    text, sha256, size = _serialize([item.model_dump()], "spc")
    payload = json.loads(text)
    assert payload[0] == {"note": "1.白盒日志"}
    assert payload[1]["path"] == "/sf/log/{date}/*"
    assert payload[1]["filters"][0]["action"] == "exclude"
    assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert size == len(text.encode("utf-8"))


def test_command_item_allowed_only_for_spc():
    item = CustomScenarioItem(action="command", path="ipmitool sel elist")
    with pytest.raises(ValueError):
        _serialize([item.model_dump()], "json")
    _serialize([item.model_dump()], "spc")


def test_item_accepts_date_placeholder():
    item = CustomScenarioItem(path="/sf/log/{date}.tar.gz")
    assert item.path == "/sf/log/{date}.tar.gz"


def test_update_can_switch_channel():
    """通道可改：它决定节点上写什么，管理员需要能纠正建错的场景。"""
    payload = LogCollectCustomScenarioUpdate(source="spc")
    assert payload.source == "spc"
    assert payload.model_dump(exclude_unset=True) == {"source": "spc"}


def test_create_dedupes_items():
    payload = LogCollectCustomScenarioCreate(
        code="cs_demo",
        label="演示",
        items=[CustomScenarioItem(path="/sf/a"), CustomScenarioItem(path="/sf/a")],
    )
    assert [item.path for item in payload.items] == ["/sf/a"]


# ── 客户视角：只回元数据 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_list_never_exposes_items(stub: _StubService):
    async with _client() as client:
        response = await client.get("/api/log-collect/custom-scenarios")

    assert response.status_code == 200
    body = response.json()
    assert len(body["scenarios"]) == 2
    first = body["scenarios"][0]
    assert set(first) == {"scenario_id", "revision", "label", "summary", "item_count"}
    # 整个响应里不得出现任何路径字符串
    assert "/sf/" not in response.text
    assert "items" not in response.text
    # 客户入口必须只取启用项
    assert stub.include_disabled_calls == [False]


# ── 管理端 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_create_passes_actor_and_returns_201(stub: _StubService):
    payload = {
        "code": "cs_demo",
        "label": "演示场景",
        "items": [{"path": "/sf/log/cluster/", "category": "log"}],
    }
    async with _client() as client:
        response = await client.post(
            "/internal/log-collect/custom-scenarios",
            json=payload,
            headers={**_ADMIN_HEADERS, "X-Admin-User": "ops-1"},
        )

    assert response.status_code == 201
    assert stub.create_calls[0][1] == "ops-1"


@pytest.mark.asyncio
async def test_admin_update_reports_new_revision(stub: _StubService):
    async with _client() as client:
        response = await client.put(
            "/internal/log-collect/custom-scenarios/cs_demo",
            json={"label": "新名称"},
            headers=_ADMIN_HEADERS,
        )

    assert response.status_code == 200
    assert stub.update_calls[0][0] == "cs_demo"
    assert response.json()["revision"] == 2


@pytest.mark.asyncio
async def test_admin_validate_returns_size_and_server_preview(stub: _StubService):
    """预览文本必须来自服务端序列化：前端复刻协议迟早与真实下发漂移。"""
    async with _client() as client:
        response = await client.post(
            "/internal/log-collect/custom-scenarios/validate",
            json={"code": "cs_demo", "label": "演示", "items": [{"path": "/sf/a"}]},
            headers=_ADMIN_HEADERS,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["item_count"] == 1
    assert body["preview_kind"] == "config"
    assert body["preview_text"] == '{"paths":"/sf/a"}'
    assert body["config_bytes"] == len(b'{"paths":"/sf/a"}')
    assert stub.create_calls == []


@pytest.mark.asyncio
async def test_admin_validate_preview_follows_spc_channel(stub: _StubService):
    async with _client() as client:
        response = await client.post(
            "/internal/log-collect/custom-scenarios/validate",
            json={
                "code": "cs_demo",
                "label": "演示",
                "source": "spc",
                "items": [
                    {"action": "command", "path": "ipmitool sel elist", "note": "1.硬件"},
                ],
            },
            headers=_ADMIN_HEADERS,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["preview_kind"] == "spc"
    payload = json.loads(body["preview_text"])
    assert payload[0] == {"note": "1.硬件"}
    assert payload[1] == {"action": "command", "path": "ipmitool sel elist"}


@pytest.mark.asyncio
async def test_admin_validate_reports_channel_mismatch_as_issue(stub: _StubService):
    """切回 -c 但残留通配符条目是可预期的用户错误：返回 issues，不能 500。"""
    async with _client() as client:
        response = await client.post(
            "/internal/log-collect/custom-scenarios/validate",
            json={"code": "cs_demo", "label": "演示", "source": "visual", "items": [{"path": "/sf/a*"}]},
            headers=_ADMIN_HEADERS,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "通配符" in body["issues"][0]["message"]


@pytest.mark.asyncio
async def test_admin_delete_requires_token(stub: _StubService):
    async with _client() as client:
        response = await client.delete("/internal/log-collect/custom-scenarios/cs_demo")

    # 与 require_admin_token 的既有口径一致：缺凭证是 403（不是 401）
    assert response.status_code == 403
    assert stub.deleted == []


# ── 路径展开（创建任务时生成 config 的唯一入口）────────────────────────────


def test_expand_paths_replaces_date_placeholder_per_day():
    from datetime import date

    from app.services.log_collect_custom_scenario_service import expand_scenario_paths

    paths = expand_scenario_paths(
        [{"path": "/sf/log/{date}.tar.gz"}],
        date(2026, 11, 3),
        date(2026, 11, 5),
    )
    assert paths == [
        "/sf/log/2026-11-03.tar.gz",
        "/sf/log/2026-11-04.tar.gz",
        "/sf/log/2026-11-05.tar.gz",
    ]


def test_expand_paths_merges_extra_paths_and_dedupes():
    """同时传 -c 与 -p 会被 CLI 覆盖：extra_paths 必须在平台侧合并。"""
    from datetime import date

    from app.services.log_collect_custom_scenario_service import expand_scenario_paths

    paths = expand_scenario_paths(
        [{"path": "/sf/log/cluster/"}],
        date(2026, 11, 3),
        date(2026, 11, 3),
        extra_paths=["/sf/etc/a.conf", "/sf/log/cluster/"],
    )
    assert paths == ["/sf/log/cluster/", "/sf/etc/a.conf"]


def test_expand_paths_rejects_oversized_result():
    from datetime import date

    import pytest
    from app.services.log_collect_custom_scenario_service import expand_scenario_paths

    items = [{"path": f"/sf/log/p{i}/{{date}}"} for i in range(70)]
    with pytest.raises(ValueError, match="超过上限"):
        expand_scenario_paths(items, date(2026, 11, 1), date(2026, 11, 3), max_count=200)


# ── 通道切换后的序列化（不连数据库）──────────────────────────────────────


def _current_row(*, source: str = "visual") -> dict:
    now = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
    return {
        "scenario_id": uuid4(),
        "revision": 1,
        "code": "cs_demo",
        "label": "演示",
        "summary": None,
        "applies_to": None,
        "items": [{"action": "copy", "path": "/sf/a", "category": "log"}],
        "item_count": 1,
        "config_sha256": "b" * 64,
        "config_bytes": 10,
        "source": source,
        "enabled": True,
        "sort_order": 100,
        "review_state": "pending_review",
        "created_by": "ops",
        "updated_by": "ops",
        "created_at": now,
        "updated_at": now,
    }


class _FakeMappings:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        return self._rows[0]


class _FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _CapturingSession:
    """只捕获 update 的落库参数，替代真实 AsyncSession。"""

    def __init__(self, current: dict):
        self.current = current
        self.insert_params: dict | None = None

    async def execute(self, statement, params=None):
        if "ORDER BY revision DESC LIMIT 1" in str(statement):
            return _FakeResult([self.current])
        self.insert_params = params
        row = dict(self.current)
        row.update(
            {
                "revision": params["revision"],
                "label": params["label"],
                "item_count": params["item_count"],
                "config_sha256": params["sha256"],
                "config_bytes": params["size"],
                "source": params["source"],
            }
        )
        return _FakeResult([row])


@pytest.mark.asyncio
async def test_update_serializes_with_switched_channel():
    """切到 spc 后必须用 spc 序列化器。

    沿用 current.source 会把 spc 条目降级成 -c 的 paths 文本，
    filters/action 全部丢失且不报错 —— 这正是本次要锁死的回归点。
    """
    current = _current_row()
    session = _CapturingSession(current)
    service = LogCollectCustomScenarioService(session)  # type: ignore[arg-type]

    updated = await service.update("cs_demo", LogCollectCustomScenarioUpdate(source="spc"), actor="ops")

    assert session.insert_params is not None
    assert session.insert_params["source"] == "spc"
    assert session.insert_params["sha256"] == _serialize(current["items"], "spc")[1]
    assert session.insert_params["sha256"] != _serialize(current["items"], "visual")[1]
    assert updated.source == "spc"


@pytest.mark.asyncio
async def test_update_keeps_current_channel_when_source_absent():
    current = _current_row(source="spc")
    session = _CapturingSession(current)
    service = LogCollectCustomScenarioService(session)  # type: ignore[arg-type]

    await service.update("cs_demo", LogCollectCustomScenarioUpdate(label="改名"), actor="ops")

    assert session.insert_params is not None
    assert session.insert_params["source"] == "spc"
    assert session.insert_params["sha256"] == _serialize(current["items"], "spc")[1]
