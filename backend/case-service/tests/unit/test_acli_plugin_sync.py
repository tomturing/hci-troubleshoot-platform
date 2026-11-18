"""acli 阶段二数据面：票据签发/校验 + manifest/包下载端点契约。

链路收口后服务端不再连桥，改由桥持「按工单签发的短期只读票据」反向拉取。
本文件锁住这条链路上不可让步的四件事：

1. 票据只绑定 case：换工单、过期、伪造一律拒；TTL 由服务端封顶——时长不是
   不可信客户端可以协商的东西；
2. 票据族端点 fail-closed：缺票/坏票在碰数据库**之前**就 401；
3. manifest 的形状就是桥的解析契约（``manifest_version`` 2.0 + 固化编译产物直出）；
   没有期望 revision 的行只能跳过，不能拿"最新一版"去猜；
4. 包下载失败的三种原因必须可区分（未配置存储 / 名称非法 / 文件缺失），桥才会
   回退到内联 files 收敛，而不是把截断响应当成下载成功。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.domain.acli_plugin_spec import TEMPLATE_SPEC
from app.routes import acli_plugins
from app.services.acli_plugin_admin import prepare_spec
from app.services.plugin_ticket import (
    PluginTicketError,
    issue_plugin_ticket,
    verify_plugin_ticket,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

CASE_ID = "Q2026111800001"
OTHER_CASE_ID = "Q2026111800002"
PLUGIN = "hci_lmt_check"
# 固定的构建时刻：只有拿定稿的 revision 去比对，才能区分「直出固化产物」
# 与「拉一次重算一次」（后者会把 last_updated_at 改成当前时间，节点永远跳不过）。
NOW = datetime(2026, 11, 18, 6, 0, 0, tzinfo=UTC)


# ── 票据本体 ────────────────────────────────────────────────────────────────


def test_ticket_roundtrip_is_bound_to_its_case():
    token, expires_at = issue_plugin_ticket(CASE_ID)
    assert expires_at > int(time.time())
    assert f"{expires_at}." == token[: len(str(expires_at)) + 1]  # 形如 {exp}.{sig}
    verify_plugin_ticket(CASE_ID, token)
    with pytest.raises(PluginTicketError) as exc:
        verify_plugin_ticket(OTHER_CASE_ID, token)
    assert exc.value.args[0] == "ticket_signature_mismatch"


def test_ticket_ttl_is_capped_and_floored_by_the_server():
    cap = int(acli_plugins.settings.ACLI_PLUGIN_TICKET_TTL_SECONDS)
    # 客户端要 30 天：截断到服务端上限（票据时长不可协商）。
    long_token, long_exp = issue_plugin_ticket(CASE_ID, 30 * 24 * 3600)
    assert 0 < long_exp - int(time.time()) <= cap
    # 要 1 秒：抬到下限，否则一次慢下载就会让票据在半途失效。
    short_token, short_exp = issue_plugin_ticket(CASE_ID, 1)
    assert short_exp - int(time.time()) >= 60
    verify_plugin_ticket(CASE_ID, long_token)
    verify_plugin_ticket(CASE_ID, short_token)


def test_expired_ticket_is_rejected(monkeypatch):
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 3600)
    token, _ = issue_plugin_ticket(CASE_ID, 60)
    monkeypatch.undo()
    with pytest.raises(PluginTicketError) as exc:
        verify_plugin_ticket(CASE_ID, token)
    assert exc.value.args[0] == "ticket_expired"


@pytest.mark.parametrize("token", ["", "   ", "x", "123", "123.", ".abcdef", "not-a-number.abcdef"])
def test_malformed_tokens_are_rejected(token):
    with pytest.raises(PluginTicketError) as exc:
        verify_plugin_ticket(CASE_ID, token)
    assert exc.value.args[0] == "ticket_malformed"


def test_ticket_signed_with_other_secret_is_rejected(monkeypatch):
    token, _ = issue_plugin_ticket(CASE_ID)
    monkeypatch.setattr(
        acli_plugins.settings, "INTERNAL_API_TOKEN", "a-totally-different-internal-token"
    )
    with pytest.raises(PluginTicketError) as exc:
        verify_plugin_ticket(CASE_ID, token)
    assert exc.value.args[0] == "ticket_signature_mismatch"


# ── 端点公共桩 ──────────────────────────────────────────────────────────────


class FakeStore:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.calls = 0

    async def list_desired(self):
        self.calls += 1
        return self.rows


@pytest.fixture
def client(monkeypatch):
    """挂上真实的 client_router / sync_router，替掉 DB 与期望态来源。"""
    store = FakeStore()
    monkeypatch.setattr(acli_plugins, "_store", lambda: store)
    # ``verify_case_exists`` 里 fetchone() 是同步取行（execute 才是 await），
    # 用 AsyncMock 当 fetchone 会留下永不 await 的协程并让"存在"恒为真。
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(fetchone=lambda: (1,))))
    app = FastAPI()
    app.include_router(acli_plugins.client_router)
    app.include_router(acli_plugins.sync_router)
    app.dependency_overrides[acli_plugins.get_client_id] = lambda: "client-a"
    app.dependency_overrides[acli_plugins.get_session] = lambda: session
    with TestClient(app) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        test_client.store = store  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()


def _case_missing(client) -> None:
    client.db.execute = AsyncMock(return_value=SimpleNamespace(fetchone=lambda: None))


def _desired_row(*, spec_hash="a" * 64, compiled=True, plugin_name=PLUGIN, prepared=None):
    prepared = prepared or prepare_spec(TEMPLATE_SPEC, actor="admin", now=NOW)
    validation = {"ok": True, "compiled": prepared["compiled"]} if compiled else {"ok": True}
    return {
        "plugin_name": plugin_name,
        "desired_spec_hash": spec_hash,
        "validation": validation,
        "spec_json": prepared["spec"],
    }


# ── 取票端点（客户身份族）────────────────────────────────────────────────────


def test_ticket_endpoint_returns_ticket_that_the_sync_endpoints_accept(client):
    issued = client.post("/api/acli-plugins/ticket", json={"case_id": f"  {CASE_ID}  "})
    assert issued.status_code == 200
    body = issued.json()
    assert body["case_id"] == CASE_ID  # 去空白后回显，桥拿到的就是这把
    # 取到的票必须能直接换到 manifest：两侧同一密钥、同一格式，不能各解一套。
    served = client.get(
        "/internal/acli-plugins/manifest",
        params={"case_id": CASE_ID},
        headers={"X-Plugin-Ticket": body["ticket"]},
    )
    assert served.status_code == 200


def test_ticket_endpoint_requires_case_id(client):
    for case_id in ["", "   "]:
        response = client.post("/api/acli-plugins/ticket", json={"case_id": case_id})
        assert response.status_code == 400
    assert client.db.execute.await_count == 0  # 空工单不该去查库


def test_ticket_endpoint_rejects_unknown_case(client):
    """工单不存在不能发票：票据是只读入口，凭空给一张票就是无主凭证。"""
    _case_missing(client)
    response = client.post("/api/acli-plugins/ticket", json={"case_id": "Q0000000000000"})
    assert response.status_code == 404
    assert "ticket" not in response.json()


# ── manifest（票据族）───────────────────────────────────────────────────────


def test_manifest_without_ticket_is_rejected_before_any_db_read(client):
    for headers, params in (
        ({}, {"case_id": CASE_ID}),  # 缺票
        ({"X-Plugin-Ticket": "123.abc"}, {"case_id": CASE_ID}),  # 伪造
        ({"X-Plugin-Ticket": issue_plugin_ticket(OTHER_CASE_ID)[0]}, {"case_id": CASE_ID}),  # 串用
        ({"X-Plugin-Ticket": issue_plugin_ticket(CASE_ID)[0]}, {}),  # 缺 case_id
    ):
        response = client.get("/internal/acli-plugins/manifest", params=params, headers=headers)
        assert response.status_code == 401, (headers, params)
        assert "票据无效" in response.json()["detail"]
    assert client.store.calls == 0, "鉴权失败不得读期望态（fail-closed 且不做无谓查询）"


def test_manifest_shape_is_the_bridge_parse_contract(client):
    prepared = prepare_spec(TEMPLATE_SPEC, actor="admin", now=NOW)
    compiled = prepared["compiled"]
    client.store.rows = [_desired_row(spec_hash=compiled["spec_hash"], prepared=prepared)]
    response = client.get(
        "/internal/acli-plugins/manifest",
        params={"case_id": CASE_ID},
        headers={"X-Plugin-Ticket": issue_plugin_ticket(CASE_ID)[0]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["manifest_version"] == "2.0"
    assert payload["case_id"] == CASE_ID
    [entry] = payload["plugins"]
    # 桥按这些键做差量三态与安装，任何一项改名都要同步桥侧。
    assert entry["name"] == PLUGIN
    assert entry["spec_hash"] == compiled["spec_hash"]
    assert entry["version"] == compiled["version"]
    assert entry["update_mode"] == compiled["update_mode"]
    assert entry["built_in"] == compiled["built_in"]
    assert entry["smoke_args"] == compiled["smoke_args"]
    assert entry["files"] == compiled["files"]
    # 逐字等于固化的那份（含 last_updated_at）：端点一旦重算，这一条就会变。
    assert entry["register_item"] == compiled["register_item"]
    assert entry["register_item"]["last_updated_at"] == NOW.isoformat()
    assert set(entry) >= {"enable_completion", "size", "package_ref", "sha256"}


def test_manifest_skips_plugin_without_expected_revision(client):
    client.store.rows = [
        {"plugin_name": PLUGIN, "desired_spec_hash": "", "spec_json": None, "validation": None},
        _desired_row(plugin_name="netdoctor"),
    ]
    response = client.get(
        "/internal/acli-plugins/manifest",
        params={"case_id": CASE_ID},
        headers={"X-Plugin-Ticket": issue_plugin_ticket(CASE_ID)[0]},
    )
    assert response.status_code == 200
    names = [plugin["name"] for plugin in response.json()["plugins"]]
    # 脏数据只能跳过：拿"最新一版"补上，等于把没生效过的规格发给节点。
    assert names == ["netdoctor"]


def test_manifest_falls_back_to_recompute_when_compiled_is_missing(client):
    client.store.rows = [_desired_row(compiled=False)]
    response = client.get(
        "/internal/acli-plugins/manifest",
        params={"case_id": CASE_ID},
        headers={"X-Plugin-Ticket": issue_plugin_ticket(CASE_ID)[0]},
    )
    assert response.status_code == 200
    [entry] = response.json()["plugins"]
    assert entry["files"], "回退重算也必须给出可收敛的内联文件"
    # 重算拿的是当前时刻，所以同一条 revision 每次拉都不同 → 退化成全量重装；
    # 这条断言守的是「回退只能当兼容旧数据的临时通道，不能当常态」。
    assert entry["register_item"]["last_updated_at"] != NOW.isoformat()


def test_manifest_reports_package_ref_only_when_the_object_exists(client, monkeypatch, tmp_path):
    monkeypatch.setattr(acli_plugins.settings, "ACLI_PLUGIN_PACKAGE_ROOT", str(tmp_path))
    row = _desired_row()
    # 包对象名用的是档案指针上的期望 revision，不是 compiled 里那把：两者正常时相等，
    # 故意不相等才能证明本端点只按指针寻址。
    spec_hash = row["desired_spec_hash"]
    client.store.rows = [row]
    url = "/internal/acli-plugins/manifest"
    headers = {"X-Plugin-Ticket": issue_plugin_ticket(CASE_ID)[0]}

    empty = client.get(url, params={"case_id": CASE_ID}, headers=headers)
    assert empty.json()["plugins"][0]["package_ref"] == ""  # 无包 → 桥走内联 files

    name = f"{PLUGIN}-{spec_hash}.tar"
    (tmp_path / name).write_bytes(b"tar")
    served = client.get(url, params={"case_id": CASE_ID}, headers=headers)
    assert served.json()["plugins"][0]["package_ref"] == name
    assert served.json()["plugins"][0]["sha256"] == ""  # 没摘要就不声称摘要

    (tmp_path / f"{name}.sha256").write_text(f"{'b' * 64}\n", encoding="utf-8")
    with_digest = client.get(url, params={"case_id": CASE_ID}, headers=headers)
    assert with_digest.json()["plugins"][0]["sha256"] == "b" * 64


# ── 包下载（票据族）──────────────────────────────────────────────────────────


def test_package_requires_a_valid_ticket(client):
    response = client.get(f"/internal/acli-plugins/package/{PLUGIN}", params={"case_id": CASE_ID})
    assert response.status_code == 401
    assert "票据无效" in response.json()["detail"]


def test_package_rejects_names_that_are_not_content_addressed(client):
    headers = {"X-Plugin-Ticket": issue_plugin_ticket(CASE_ID)[0]}
    for name, spec_hash in ((PLUGIN, "deadbeef"), (PLUGIN, ""), ("plugin-name", "a" * 64), ("Acli", "a" * 64)):
        response = client.get(
            f"/internal/acli-plugins/package/{name}",
            params={"case_id": CASE_ID, "spec_hash": spec_hash},
            headers=headers,
        )
        assert response.status_code == 404, (name, spec_hash)
        assert response.json()["detail"]["code"] == "package_not_found"


def test_package_distinguishes_unset_storage_from_missing_object(client, monkeypatch, tmp_path):
    headers = {"X-Plugin-Ticket": issue_plugin_ticket(CASE_ID)[0]}
    params = {"case_id": CASE_ID, "spec_hash": "a" * 64}

    monkeypatch.setattr(acli_plugins.settings, "ACLI_PLUGIN_PACKAGE_ROOT", "   ")
    unset = client.get(f"/internal/acli-plugins/package/{PLUGIN}", params=params, headers=headers)
    assert unset.status_code == 404
    assert unset.json()["detail"]["code"] == "package_source_undefined"

    monkeypatch.setattr(acli_plugins.settings, "ACLI_PLUGIN_PACKAGE_ROOT", str(tmp_path))
    absent = client.get(f"/internal/acli-plugins/package/{PLUGIN}", params=params, headers=headers)
    assert absent.status_code == 404
    assert absent.json()["detail"]["code"] == "package_not_found"

    # 两种 404 的原因码不同：桥据此决定是"等平台配存储"还是"直接回退内联文件"。
    assert unset.json()["detail"]["code"] != absent.json()["detail"]["code"]


def test_package_streams_the_object_when_it_exists(client, monkeypatch, tmp_path):
    monkeypatch.setattr(acli_plugins.settings, "ACLI_PLUGIN_PACKAGE_ROOT", str(tmp_path))
    payload = b"\x7fELF-ish tar bytes"
    (tmp_path / f"{PLUGIN}-{'a' * 64}.tar").write_bytes(payload)
    response = client.get(
        f"/internal/acli-plugins/package/{PLUGIN}",
        params={"case_id": CASE_ID, "spec_hash": "a" * 64},
        headers={"X-Plugin-Ticket": issue_plugin_ticket(CASE_ID)[0]},
    )
    assert response.status_code == 200
    assert response.content == payload
    assert PLUGIN in response.headers.get("content-disposition", "")


def test_package_never_escapes_the_object_root(monkeypatch, tmp_path):
    """对象名只由 plugin_name + spec_hash 拼成：路径片段进不来。"""
    monkeypatch.setattr(acli_plugins.settings, "ACLI_PLUGIN_PACKAGE_ROOT", str(tmp_path))
    (tmp_path.parent / "outside.tar").write_bytes(b"secret")
    path = acli_plugins._package_path(PLUGIN, "a" * 64)
    assert path.parent == tmp_path.resolve()
    assert "/" not in path.name
