"""acli 插件管理端单测：保存即入队、幂等、页面零凭据。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from app.domain.acli_plugin_spec import TEMPLATE_SPEC, SpecValidationError
from app.services.acli_plugin_admin import (
    AcliPluginAdminStore,
    AdminValidationError,
    prepare_publication,
)

NOW = datetime(2026, 11, 14, 6, 0, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
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
        return FakeCursor(self._results.pop(0) if self._results else [])

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
    return prepare_publication(spec, env_code="24", actor="admin", deploy_id="htp-acli-test-0001", now=NOW)


def test_prepare_publication_is_deterministic_and_credential_free():
    first = _prepared()
    second = _prepared()
    assert first["spec_hash"] == second["spec_hash"]
    assert first["deploy_id"] == "htp-acli-test-0001"
    assert first["plugin_name"] == "hci_lmt_check"
    serialized = json.dumps(first, ensure_ascii=False, default=str)
    assert "password" not in serialized
    assert "private_key" not in serialized
    assert "node_ip" not in serialized
    assert "1mt" not in serialized


def test_forbidden_runtime_fields_are_rejected():
    with pytest.raises(SpecValidationError):
        _prepared(node_ip="172.28.24.1")
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


def test_env_code_is_required():
    with pytest.raises(AdminValidationError):
        prepare_publication(TEMPLATE_SPEC, env_code="  ")


def test_save_and_publish_creates_pending_task_in_one_transaction():
    db = FakeDB(
        [
            [{"env_code": "24", "node_ip": "172.28.24.1"}],
            [],
            [],
            [],
            [{"deploy_id": "htp-acli-test-0001", "status": "pending"}],
        ]
    )
    result = asyncio.run(AcliPluginAdminStore(db).save_and_publish(_prepared()))
    assert result["status"] == "pending"
    assert result["idempotent"] is False
    assert db.session.committed

    sqls = " ".join(sql for sql, _ in db.session.calls)
    assert "INSERT INTO acli_plugin_spec" in sqls
    assert "INSERT INTO acli_plugin" in sqls
    assert "INSERT INTO acli_plugin_deployment" in sqls
    spec_params = next(params for sql, params in db.session.calls if "INSERT INTO acli_plugin_spec" in sql)
    assert "password" not in json.loads(spec_params["spec_json"])
    assert "private_key" not in json.loads(spec_params["spec_json"])
    deployment_params = next(
        params for sql, params in db.session.calls if "INSERT INTO acli_plugin_deployment" in sql
    )
    assert deployment_params["node_ip"] == "172.28.24.1"


def test_missing_environment_is_rejected_before_any_write():
    db = FakeDB([[]])
    with pytest.raises(AdminValidationError) as exc:
        asyncio.run(AcliPluginAdminStore(db).save_and_publish(_prepared()))
    assert "未登记或已停用" in exc.value.errors[0]
    assert not any("INSERT" in sql for sql, _ in db.session.calls)


def test_repeated_publish_is_idempotent():
    db = FakeDB(
        [
            [{"env_code": "24", "node_ip": "172.28.24.1"}],
            [{"deploy_id": "htp-acli-old", "status": "succeeded"}],
        ]
    )
    result = asyncio.run(AcliPluginAdminStore(db).save_and_publish(_prepared()))
    assert result["idempotent"] is True
    assert result["deploy_id"] == "htp-acli-old"
    assert not any("INSERT" in sql for sql, _ in db.session.calls)


def test_environment_listing_never_exposes_addresses_or_credential_refs():
    db = FakeDB([[{"env_code": "24", "display_name": "24 环境", "enabled": True, "auth_type": "key"}]])
    rows = asyncio.run(AcliPluginAdminStore(db).list_environments())
    assert rows[0]["env_code"] == "24"
    sql = db.session.calls[0][0]
    assert "credential_ref" not in sql
    assert "node_ip" not in sql
    assert "bridge_url" not in sql
