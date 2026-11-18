"""acli 插件管理端单测：规格是不可变事实源、保存零副作用、页面零凭据。

链路收口（D1）之后本文件只锁三件事：

1. ``prepare_spec`` 是纯函数——同一规格同一 ``spec_hash``，产物里不含任何凭据
   字段（口令/私钥/节点地址），也不含"下发目标"（插件何时到节点由客户侧决定）；
2. ``save_spec`` 只写两张表（不可变 revision + 插件档案），**不产生任何下发副
   作用**：出现部署任务类 SQL 就等于把已删除的旧链路又写回来；
3. 阶段二期望态按 ``desired_spec_hash`` 指针取，而不是"最新 revision"——保存失败
   或回滚期望指针时，未生效的 revision 绝不能被发给节点。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from app.domain.acli_plugin_spec import TEMPLATE_SPEC, SpecValidationError
from app.services.acli_plugin_admin import (
    AcliPluginAdminStore,
    AdminValidationError,
    prepare_spec,
)

NOW = __import__("datetime").datetime(2026, 11, 18, 6, 0, 0, tzinfo=__import__("datetime").UTC)
SPEC_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


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
    return prepare_spec({**TEMPLATE_SPEC, **overrides}, actor="admin", now=NOW)


def test_prepare_spec_is_deterministic_and_credential_free():
    first = _prepared()
    second = _prepared()
    # 同一规格必须得到同一 spec_hash：它是幂等键，也是阶段二差量三态的比较键。
    assert first["spec_hash"] == second["spec_hash"]
    assert SPEC_HASH_RE.match(first["spec_hash"])
    assert first["plugin_name"] == "hci_lmt_check"

    serialized = json.dumps(first, ensure_ascii=False, default=str)
    for forbidden in ("password", "private_key", "node_ip", "target_case_id", "1mt"):
        assert forbidden not in serialized, forbidden


def test_forbidden_runtime_fields_are_rejected():
    # 运行时字段混进规格就会把"这台机器/这个口令"当成平台事实源，跨工单复用必然出错。
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


def test_prepare_spec_freezes_compiled_artifacts():
    """编译产物必须在保存时定稿：manifest 端点零重算直出，节点才可能判出"无修改"。"""
    prepared = _prepared()
    compiled = prepared["compiled"]
    assert prepared["validation"]["ok"] is True
    assert compiled["files"], "内联文件清单是桥当前唯一的收敛通道，不得为空"
    assert compiled["register_item"]["name"] == prepared["plugin_name"]
    assert compiled["version"] == prepared["spec"]["version"]
    # last_updated_at 取构建时刻：反复重算会让同一条 revision 的 hash 永远不同 → 全量重装。
    assert prepared["spec_hash"] == _prepared()["spec_hash"]


def test_save_spec_writes_spec_and_profile_and_nothing_else():
    db = FakeDB(
        [
            FakeCursor([], returns_rows=False),  # INSERT acli_plugin_spec
            FakeCursor([], returns_rows=False),  # INSERT acli_plugin
            [{"revision": 3}],  # 回读本次 revision（幂等时回读命中同一行）
        ]
    )
    result = asyncio.run(AcliPluginAdminStore(db).save_spec(_prepared()))

    assert result["status"] == "saved"
    assert result["revision"] == 3
    assert db.session.committed

    sqls = " ".join(sql for sql, _ in db.session.calls)
    assert "INSERT INTO acli_plugin_spec" in sqls
    assert "INSERT INTO acli_plugin" in sqls
    assert "ON CONFLICT (spec_hash) DO NOTHING" in sqls  # 重复保存不增长 revision
    # D1 的硬边界：保存规格不得再产生任何下发痕迹。
    for residue in ("deployment", "lease", "relay", "node_ip", "env_code"):
        assert residue not in sqls.lower(), residue

    _, spec_params = next(call for call in db.session.calls if "INSERT INTO acli_plugin_spec" in call[0])
    stored = json.loads(spec_params["spec_json"])
    serialized = json.dumps(stored, ensure_ascii=False)
    assert "password" not in serialized and "private_key" not in serialized
    # compiled 固化进 revision 的 validation：阶段二 manifest 因此不需要重算。
    assert spec_params["spec_hash"] == result["spec_hash"]

    # asyncpg 对同名参数在不同位置会推断出不同类型（varchar vs text），
    # 一条 INSERT 里复用参数名会直接抛 AmbiguousParameterError。
    for sql, _ in db.session.calls:
        if "INSERT INTO" not in sql:
            continue
        names = re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", sql)
        assert len(names) == len(set(names)), sql


def test_compiled_is_persisted_inside_validation_payload():
    db = FakeDB([FakeCursor([], returns_rows=False), FakeCursor([], returns_rows=False), [{"revision": 1}]])
    prepared = _prepared()
    asyncio.run(AcliPluginAdminStore(db).save_spec(prepared))
    _, spec_params = next(call for call in db.session.calls if "INSERT INTO acli_plugin_spec" in call[0])
    validation = json.loads(spec_params["validation"])
    assert validation["compiled"]["spec_hash"] == prepared["compiled"]["spec_hash"]
    assert validation["ok"] is True


def test_write_statements_without_returning_are_not_read_as_rows():
    db = FakeDB(
        [
            FakeCursor([], returns_rows=False),
            FakeCursor([], returns_rows=False),
            [{"revision": 1}],
        ]
    )
    result = asyncio.run(AcliPluginAdminStore(db).save_spec(_prepared()))
    assert result["status"] == "saved"
    assert db.session.committed


def test_list_desired_reads_expected_pointer_not_latest_revision():
    """期望态按档案指针取：未生效的 revision 绝不能被发给节点。"""
    db = FakeDB([[{"plugin_name": "hci_lmt_check", "desired_spec_hash": "a" * 64}]])
    rows = asyncio.run(AcliPluginAdminStore(db).list_desired())
    assert rows and rows[0]["plugin_name"] == "hci_lmt_check"

    sql = db.session.calls[0][0].lower()
    assert "sp.spec_hash = p.desired_spec_hash" in sql
    assert "p.enabled = true" in sql
    assert "order by sp.revision desc" not in sql  # 那是 list_plugins 的口径，不是期望态
    for residue in ("deployment", "lease"):
        assert residue not in sql


def test_admin_store_exposes_exactly_the_spec_maintenance_api():
    """公开方法白名单：链路收口后管理端只会维护与回读期望态。

    用白名单而不是逐个断言旧方法不存在：任何新增方法都要在这里显式登记，
    「服务端重新长出一条下发旁路」与「只是给旧名字换个写法」都会被这条拦住。
    """
    public = {name for name in dir(AcliPluginAdminStore) if not name.startswith("_")}
    assert public == {"save_spec", "list_plugins", "list_desired"}
