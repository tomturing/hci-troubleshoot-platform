"""acli 插件管理端单测：目标来自 bridge 已登录会话、保存即入队、幂等、页面零凭据。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime

import pytest
from app.domain.acli_plugin_spec import TEMPLATE_SPEC, SpecValidationError
from app.services.acli_plugin_admin import (
    AcliPluginAdminStore,
    AdminValidationError,
    prepare_publication,
)

NOW = datetime(2026, 11, 14, 6, 0, 0, tzinfo=UTC)
DEPLOY_ID = "htp-acli-test-0001"
CASE_ID = "case-0042"
NODE_IP = "172.28.24.1"


class FakeCursor:
    def __init__(self, rows, *, returns_rows: bool = True):
        self._rows = rows
        self.returns_rows = returns_rows

    def mappings(self):
        if not self.returns_rows:
            raise AssertionError("无 RETURNING 的写语句不应读取结果集")
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple[str, dict]] = []
        self.committed = False

    async def execute(self, sql, params):
        self.calls.append((str(sql), dict(params)))
        result = self._results.pop(0) if self._results else []
        return result if isinstance(result, FakeCursor) else FakeCursor(result)

    async def commit(self):
        self.committed = True


class FakeSessionCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_args):
        return None


class FakeDB:
    def __init__(self, results):
        self.session = FakeSession(results)

    def async_session_factory(self):
        return FakeSessionCM(self.session)


def _prepared(**overrides):
    spec = {**TEMPLATE_SPEC, **overrides}
    return prepare_publication(spec, env_code="24", actor="admin", deploy_id=DEPLOY_ID, now=NOW)


def _targeted(prepared=None, *, case_id: str = CASE_ID, node_ip: str = NODE_IP):
    """模拟路由层：prepare_publication 之后补上 bridge 会话目标。"""
    result = dict(prepared if prepared is not None else _prepared())
    result["target_case_id"] = case_id
    result["target_node_ip"] = node_ip
    return result


def test_prepare_publication_is_deterministic_and_credential_free():
    first = _prepared()
    second = _prepared()
    assert first["spec_hash"] == second["spec_hash"]
    assert first["deploy_id"] == DEPLOY_ID
    assert first["plugin_name"] == "hci_lmt_check"
    serialized = json.dumps(first, ensure_ascii=False, default=str)
    assert "password" not in serialized
    assert "private_key" not in serialized
    assert "node_ip" not in serialized
    assert "1mt" not in serialized


def test_forbidden_runtime_fields_are_rejected():
    with pytest.raises(SpecValidationError):
        _prepared(node_ip=NODE_IP)
    with pytest.raises(SpecValidationError):
        _prepared(password="prefix")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"smoke_args": ""}, "smoke_args"),
        ({"version": "v1"}, "version"),
        ({"update_mode": "builtin_override", "plugin_name": "netdoctor"}, "plugin_uuid"),
    ],
)
def test_invalid_specs_are_rejected(overrides, expected):
    with pytest.raises(AdminValidationError) as exc:
        _prepared(**overrides)
    assert any(expected in error for error in exc.value.errors), exc.value.errors


def test_prepare_publication_requires_case_id():
    with pytest.raises(AdminValidationError):
        prepare_publication(TEMPLATE_SPEC, env_code="  ")


def test_save_and_publish_creates_pending_task_in_one_transaction():
    db = FakeDB(
        [
            [],  # 幂等 (a) in-flight
            [],  # 幂等 (b) succeeded+对齐
            FakeCursor([], returns_rows=False),  # INSERT acli_plugin_spec
            FakeCursor([], returns_rows=False),  # INSERT acli_plugin
            [{"deploy_id": DEPLOY_ID, "status": "pending"}],  # INSERT deployment RETURNING
        ]
    )
    result = asyncio.run(AcliPluginAdminStore(db).save_and_publish(_targeted()))
    assert result["status"] == "pending"
    assert result["idempotent"] is False
    assert result["env_code"] == CASE_ID
    assert db.session.committed

    sqls = " ".join(sql for sql, _ in db.session.calls)
    assert "INSERT INTO acli_plugin_spec" in sqls
    assert "INSERT INTO acli_plugin" in sqls
    assert "INSERT INTO acli_plugin_deployment" in sqls
    assert "acli_plugin_environment" not in sqls  # 不再查静态环境表

    spec_params = next(params for sql, params in db.session.calls if "INSERT INTO acli_plugin_spec" in sql)
    assert "password" not in json.loads(spec_params["spec_json"])
    assert "private_key" not in json.loads(spec_params["spec_json"])
    deployment_params = next(params for sql, params in db.session.calls if "INSERT INTO acli_plugin_deployment" in sql)
    assert deployment_params["env_code"] == CASE_ID
    assert deployment_params["node_ip"] == NODE_IP

    # asyncpg 对同一绑定参数在不同位置推断类型不同，INSERT 里不得复用参数名。
    for sql, _ in db.session.calls:
        if "INSERT INTO" not in sql:
            continue
        names = re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", sql)
        assert len(names) == len(set(names)), sql
    assert spec_params["revision_plugin_name"] == "hci_lmt_check"
    assert deployment_params["revision_spec_hash"] == result["spec_hash"]


def test_write_statements_without_returning_are_not_read_as_rows():
    db = FakeDB(
        [
            [],
            [],
            FakeCursor([], returns_rows=False),
            FakeCursor([], returns_rows=False),
            FakeCursor([], returns_rows=False),
        ]
    )
    result = asyncio.run(AcliPluginAdminStore(db).save_and_publish(_targeted()))
    assert result["status"] == "pending"
    assert db.session.committed


@pytest.mark.parametrize(
    ("target_case_id", "target_node_ip", "env_code"),
    [
        ("", NODE_IP, ""),  # 无 case_id
        (CASE_ID, "", "24"),  # 无 node_ip（env_code 兜底不参与目标校验）
        ("", "", ""),  # 两者都缺
    ],
)
def test_save_and_publish_without_target_session_is_rejected(target_case_id, target_node_ip, env_code):
    prepared = _targeted(case_id=target_case_id, node_ip=target_node_ip)
    prepared["env_code"] = env_code
    db = FakeDB([])
    with pytest.raises(AdminValidationError) as exc:
        asyncio.run(AcliPluginAdminStore(db).save_and_publish(prepared))
    assert "目标会话无效" in exc.value.errors[0]
    assert db.session.calls == []  # 任何写操作之前就拒绝


def test_repeated_publish_is_idempotent():
    # (a) in-flight 未命中，(b) succeeded+current_spec_hash 对齐命中。
    db = FakeDB([[], [{"deploy_id": "htp-acli-old", "status": "succeeded"}]])
    result = asyncio.run(AcliPluginAdminStore(db).save_and_publish(_targeted()))
    assert result["idempotent"] is True
    assert result["deploy_id"] == "htp-acli-old"
    assert not any("INSERT" in sql for sql, _ in db.session.calls)


def test_in_flight_publish_reuses_deploy_id_without_waiting_for_current_hash():
    """首次下发未成功（current_spec_hash 未推进）时，重复提交也必须命中 in-flight 幂等。

    旧实现把 current_spec_hash 写进同一条 SQL，导致这里返回 NULL → 重复建任务、
    同一脚本排队执行两次。
    """
    db = FakeDB([[{"deploy_id": "htp-acli-running", "status": "running"}]])
    result = asyncio.run(AcliPluginAdminStore(db).save_and_publish(_targeted()))
    assert result["idempotent"] is True
    assert result["deploy_id"] == "htp-acli-running"
    assert result["status"] == "running"
    # 两条幂等查询同事务执行；关键是在飞判定语句里不得出现 current_spec_hash。
    assert "current_spec_hash" not in db.session.calls[0][0]
    assert not any("INSERT" in sql for sql, _ in db.session.calls)


def test_force_skips_idempotency_so_cleared_node_can_be_reinstalled():
    """acli 重装会清空插件目录：节点被清空后管理员必须能用 force 重发同一规格。"""
    prepared = _targeted()
    prepared["force"] = True
    db = FakeDB(
        [
            FakeCursor([], returns_rows=False),  # INSERT acli_plugin_spec
            FakeCursor([], returns_rows=False),  # INSERT acli_plugin
            [{"deploy_id": DEPLOY_ID, "status": "pending"}],  # INSERT deployment RETURNING
        ]
    )
    result = asyncio.run(AcliPluginAdminStore(db).save_and_publish(prepared))
    assert result["idempotent"] is False
    assert result["status"] == "pending"
    sqls = " ".join(sql for sql, _ in db.session.calls)
    assert "'pending', 'claimed', 'running'" not in sqls  # 未做任何幂等查询


def test_deployment_queries_never_expose_node_ip_or_lease_owner():
    db = FakeDB([[], []])
    store = AcliPluginAdminStore(db)
    asyncio.run(store.list_deployments("hci_lmt_check"))
    asyncio.run(store.get_deployment(DEPLOY_ID))
    assert len(db.session.calls) == 2
    for sql, _ in db.session.calls:
        assert "SELECT *" not in sql
        assert "node_ip" not in sql.lower()
        assert "lease_owner" not in sql.lower()


def test_list_environments_is_always_empty():
    db = FakeDB([])
    rows = asyncio.run(AcliPluginAdminStore(db).list_environments())
    assert rows == []
    assert db.session.calls == []  # 连静态环境表都不再查询，天然不泄露凭据
