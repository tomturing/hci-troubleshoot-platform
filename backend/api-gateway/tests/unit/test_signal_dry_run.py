"""Signal 试运行 Gateway 信任边界测试。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes.signal_dry_run import _resolve_authoritative_dataset, _resolve_package_context
from fastapi import FastAPI, Request


def _request() -> Request:
    app = FastAPI()
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app, "client": ("test", 1), "server": ("test", 80), "scheme": "http"})


@pytest.mark.asyncio
@patch("app.routes.signal_dry_run.httpx.AsyncClient")
async def test_authoritative_dataset_overwrites_browser_payload(client_cls):
    client = AsyncMock()
    client_cls.return_value.__aenter__.return_value = client
    response = MagicMock(status_code=200)
    response.json.return_value = {"datasets": [{"dataset_id": "asset-1", "source_ref": "sha256:" + "a" * 64 + ":asset-1", "payload": "trusted stdout"}]}
    client.get.return_value = response
    payload = {"unit_ref": {"signal_id": "sig_1"}, "dataset": {"source_type": "fixture", "source_ref": "sha256:" + "a" * 64 + ":asset-1", "payload": "attacker"}}

    resolved = await _resolve_authoritative_dataset(payload, _request())

    assert resolved["dataset"]["payload"] == "trusted stdout"
    assert resolved["dataset"]["dataset_id"] == "asset-1"


@pytest.mark.asyncio
async def test_authoritative_dataset_requires_bundle_reference():
    payload = {"unit_ref": {"signal_id": "sig_1"}, "dataset": {"source_type": "replay", "source_ref": "browser-id", "payload": "attacker"}}

    with pytest.raises(Exception, match="source_ref"):
        await _resolve_authoritative_dataset(payload, _request())


@pytest.mark.asyncio
@patch("app.routes.signal_dry_run.httpx.AsyncClient")
async def test_package_context_resolves_internal_revision_and_checks_observed_digest(client_cls):
    digest = "sha256:" + "a" * 64
    client = AsyncMock()
    client_cls.return_value.__aenter__.return_value = client
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "package_snapshot_digest": digest,
        "source_knowledge_revision_no": 23,
    }
    client.get.return_value = response
    payload = {
        "support_id": "41446",
        "package_snapshot_digest": digest,
        "observed_snapshot_digest": digest,
    }

    resolved = await _resolve_package_context(payload, _request())
    assert resolved["kbd_revision"] == 23

    with pytest.raises(Exception, match="工作快照身份"):
        await _resolve_package_context({**payload, "observed_snapshot_digest": "sha256:" + "b" * 64}, _request())


def test_preview_token_signing_and_verification():
    from app.routes.signal_dry_run import _preview_input_digest, _sign_preview_result, _verify_preview_token

    payload = {
        "scope": "signal",
        "unit_ref": {"signal_id": "sig_001"},
        "support_id": "41398",
        "kbd_revision": 1,
        "dataset": {"source_type": "pasted", "payload": "input"},
    }
    preview_body = {
        "trace_id": "t-123",
        "config_revision": "sha256:cfg",
        "input_sha256": _preview_input_digest(payload),
        "status": "PASS",
    }

    token = _sign_preview_result(preview_body, payload)
    assert isinstance(token, str) and "." in token

    # 正确验签
    assert _verify_preview_token(token, preview_body, payload) is True

    # 篡改 trace_id
    tampered_result = dict(preview_body, trace_id="t-hacker")
    assert _verify_preview_token(token, tampered_result, payload) is False

    # 结果正文也必须属于签名边界，不能只校验 trace/config 元数据
    assert _verify_preview_token(token, {**preview_body, "value": "tampered"}, payload) is False

    # 篡改 signal_id
    tampered_payload = dict(payload, unit_ref={"signal_id": "sig_other"})
    assert _verify_preview_token(token, preview_body, tampered_payload) is False

    tampered_input = dict(payload, dataset={"source_type": "pasted", "payload": "attacker"})
    assert _verify_preview_token(token, preview_body, tampered_input) is False

    # 状态非 PASS
    fail_body = dict(preview_body, status="FAIL")
    fail_token = _sign_preview_result(fail_body, payload)
    assert _verify_preview_token(fail_token, fail_body, payload) is False


@pytest.mark.asyncio
@patch("app.routes.signal_dry_run._preview")
@patch("app.routes.signal_dry_run.httpx.AsyncClient")
async def test_save_to_bundle_uses_preview_token_without_re_execution(client_cls, preview_mock):
    from app.routes.signal_dry_run import _preview_input_digest, _sign_preview_result, save_verified_preview_to_bundle

    client = AsyncMock()
    client_cls.return_value.__aenter__.return_value = client
    client.post.return_value = MagicMock(status_code=201, json=lambda: {"bundle": {"digest": "new-digest"}})

    dry_run = {"scope": "signal", "unit_ref": {"signal_id": "sig_001"}, "support_id": "41398", "kbd_revision": 1, "dataset": {"source_type": "pasted", "payload": "out"}}
    preview_result = {"trace_id": "t-abc", "config_revision": "sha256:rev", "input_sha256": _preview_input_digest(dry_run), "status": "PASS"}
    token = _sign_preview_result(preview_result, dry_run)

    req = _request()
    req._json = {
        "dry_run": dry_run,
        "preview_token": token,
        "preview_result": preview_result,
    }

    res = await save_verified_preview_to_bundle("sha256:parent", req)
    assert res.status_code == 201

    # 核心断言：未发起二次 _preview 重跑！
    preview_mock.assert_not_called()
    # 核心断言：直接调用控制面写入
    client.post.assert_called_once()


@pytest.mark.asyncio
@patch("app.routes.signal_dry_run._resolve_package_context")
@patch("app.routes.signal_dry_run.httpx.AsyncClient")
async def test_save_to_package_writes_immutable_verification_asset(client_cls, resolve_context):
    from app.routes.signal_dry_run import _preview_input_digest, _sign_preview_result, save_verified_preview_to_package

    package_digest = "sha256:" + "a" * 64
    dry_run = {
        "scope": "qfk_execution_result",
        "verification_scope": "signal",
        "unit_ref": {"signal_id": "sig_001"},
        "support_id": "41398",
        "kbd_revision": 1,
        "package_snapshot_digest": package_digest,
        "observed_snapshot_digest": package_digest,
        "dataset": {"dataset_id": "dataset-1", "source_type": "pasted", "source_ref": "user-input", "payload": "out"},
        "signal": {"id": "sig_001"},
    }
    context = {
        "package_snapshot_digest": package_digest,
        "knowledge_snapshot_digest": "sha256:" + "b" * 64,
        "signal_spec_digest": "sha256:" + "c" * 64,
        "simulation_spec_digest": "sha256:" + "d" * 64,
        "prompt_revision": "prompt-1",
        "tool_contract_revision": "tool-1",
        "policy_revision": "policy-1",
        "compiler_revision": "compiler-1",
    }
    resolve_context.return_value = {**dry_run, "_package_context": context}
    preview_result = {
        "trace_id": "trace-1",
        "config_revision": "sha256:config",
        "input_sha256": _preview_input_digest(dry_run),
        "status": "PASS",
        "value": {"ok": True},
    }
    token = _sign_preview_result(preview_result, dry_run)
    client = AsyncMock()
    client_cls.return_value.__aenter__.return_value = client
    client.post.return_value = MagicMock(
        status_code=201,
        json=lambda: {"package_snapshot_digest": "sha256:" + "f" * 64},
    )
    req = _request()
    req._json = {"dry_run": dry_run, "preview_token": token, "preview_result": preview_result}

    response = await save_verified_preview_to_package(req)

    assert response.status_code == 201
    payload = client.post.await_args.kwargs["json"]
    assert payload["observed_snapshot_digest"] == package_digest
    assert payload["knowledge_snapshot_digest"] == context["knowledge_snapshot_digest"]
    assert payload["result_status"] == "pass"
