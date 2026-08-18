"""仿真控制面代理的真实工单绑定门禁。"""

import hashlib
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.main import app


def test_bundle_factory_reads_c1_and_injects_compiler_identity():
    client = TestClient(app)
    capability = {
        "support_id": "27123",
        "status": "ready_for_artifact_binding",
        "resolved": {
            "support_id": "27123",
            "kbd_revision": 25,
            "kbd_checksum": "sha256:kbd",
            "signals_digest": "sha256:signals",
            "tool_contract_revision": "tool-r25",
            "policy_revision": "policy-r1",
            "synthetic_routes": [{"signal_id": "sig-1", "tool": "qfk_system", "argv": ["acli", "system", "ps"]}],
        },
    }
    runtime_response = JSONResponse({"bundle": {"digest": "sha256:bundle", "status": "draft"}}, status_code=201)
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=httpx.Response(200, json=capability))) as c1_get, patch(
        "app.routes.simulations._post", new=AsyncMock(return_value=runtime_response)
    ) as runtime_post:
        response = client.post("/api/hci-sim/v1/control-plane/bundles", json={"support_id": "27123"})

    assert response.status_code == 201
    assert "/api/kb/hci-sim/capabilities/27123" in c1_get.await_args.args[0]
    assert runtime_post.await_args.kwargs["actor_role"] == "compiler"
    assert len(runtime_post.await_args.kwargs["trace_id"]) == 32
    assert runtime_post.await_args.args[1]["resolved"]["kbd_revision"] == 25


def test_bundle_factory_rejects_c1_gap_without_compiling():
    client = TestClient(app)
    capability = {"support_id": "27123", "status": "capability_gap", "capability_gaps": [{"code": "KBD_NOT_PUBLISHED"}]}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=httpx.Response(200, json=capability))), patch(
        "app.routes.simulations._post", new=AsyncMock()
    ) as runtime_post:
        response = client.post("/api/hci-sim/v1/control-plane/bundles", json={"support_id": "27123"})

    assert response.status_code == 409
    runtime_post.assert_not_awaited()


def test_bundle_factory_actions_use_server_mapped_split_roles():
    client = TestClient(app)
    runtime_response = JSONResponse({"bundle": {"digest": "sha256:bundle", "status": "validated"}}, status_code=200)
    with patch("app.routes.simulations._post", new=AsyncMock(return_value=runtime_response)) as runtime_post:
        assert client.post(
            "/api/hci-sim/v1/control-plane/bundles/sha256:bundle/revise",
            json={"manifest": {}, "reason": "专家修订"},
        ).status_code == 200
        assert runtime_post.await_args.kwargs["actor_role"] == "expert"
        assert runtime_post.await_args.kwargs["actor_purpose"] == "edit"
        assert client.post("/api/hci-sim/v1/control-plane/bundles/sha256:bundle/approve-expert").status_code == 200
        assert runtime_post.await_args.kwargs["actor_role"] == "expert"
        assert "actor_purpose" not in runtime_post.await_args.kwargs
        assert client.post("/api/hci-sim/v1/control-plane/bundles/sha256:bundle/approve-security").status_code == 200
        assert runtime_post.await_args.kwargs["actor_role"] == "security"


def test_simulation_test_run_binds_platform_case_before_runtime():
    client = TestClient(app)
    case_response = JSONResponse({"case_id": "Q2026081100001", "status": "created"}, status_code=201)
    runtime_response = JSONResponse({"test_run_id": "run-27123", "status": "preparing"}, status_code=200)
    with patch("app.routes.simulations._case_request", new=AsyncMock(return_value=case_response)) as case_request, patch(
        "app.routes.simulations._post", new=AsyncMock(return_value=runtime_response)
    ) as runtime_post:
        response = client.post(
            "/api/hci-sim/v1/simulations/test-runs",
            json={
                "kbd_id": "27123",
                "title": "KBD 27123 仿真验证",
                "description": "验证三条确定性关键信号",
                "connection": {"test_run_id": "run-27123"},
                "environment_context": {"test_run_id": "run-27123"},
            },
        )

    assert response.status_code == 200
    assert response.json()["case_id"] == "Q2026081100001"
    case_request.assert_awaited_once()
    runtime_payload = runtime_post.await_args.args[1]
    assert runtime_payload["case_id"] == "Q2026081100001"
    assert runtime_payload["environment_context"]["case_id"] == "Q2026081100001"


def test_simulation_test_run_retry_reuses_existing_case():
    """重试同一 TestRun 不得创建第二个 Case 导致 Runtime 绑定冲突。"""
    client = TestClient(app)
    existing_case = JSONResponse({"case_id": "Q2026081100001", "status": "created"}, status_code=200)
    runtime_response = JSONResponse({"test_run_id": "run-27123", "status": "leased"}, status_code=200)
    with patch("app.routes.simulations._case_request", new=AsyncMock(return_value=existing_case)) as case_request, patch(
        "app.routes.simulations._post", new=AsyncMock(return_value=runtime_response)
    ) as runtime_post:
        response = client.post(
            "/api/hci-sim/v1/simulations/test-runs",
            json={
                "kbd_id": "27123",
                "case_id": "Q2026081100001",
                "title": "KBD 27123 仿真验证",
                "description": "重试三条确定性关键信号",
                "connection": {"test_run_id": "run-27123"},
                "environment_context": {"test_run_id": "run-27123"},
            },
        )

    assert response.status_code == 200
    case_request.assert_awaited_once_with("GET", "/Q2026081100001")
    runtime_post.assert_awaited_once()
    assert runtime_post.await_args.args[1]["case_id"] == "Q2026081100001"


def test_simulation_result_digest_is_generated_from_canonical_summary():
    """HTTP 浏览器只提交结构化摘要，Gateway 生成确定性 digest。"""
    client = TestClient(app)
    runtime_response = JSONResponse({"status": "passed"}, status_code=200)
    summary = {
        "conversation_id": "00000000-0000-0000-0000-000000027123",
        "case_id": "Q2026081100001",
        "execution_mode": "sim-ssh",
        "command_count": 3,
        "failed_command_count": 0,
        "agent_stream_completed": True,
        "outcome": "passed",
    }
    canonical = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_digest = f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    with patch("app.routes.simulations._post", new=AsyncMock(return_value=runtime_response)) as runtime_post:
        response = client.post(
            "/api/hci-sim/v1/simulations/test-runs/run-27123/result",
            headers={"Idempotency-Key": "admin-result-run-27123"},
            json={
                "attempt_no": 1,
                "oracle_version": "admin-agent-session-v1",
                "outcome": "passed",
                "report_uri": "object://hci-sim/run-27123/agent-session",
                "report_summary": summary,
            },
        )

    assert response.status_code == 200
    forwarded = runtime_post.await_args.args[1]
    assert forwarded["report_digest"] == expected_digest
    assert "report_summary" not in forwarded


def test_simulation_result_rejects_browser_supplied_digest():
    """客户端不得绕过 Gateway 提交自声明摘要。"""
    client = TestClient(app)
    response = client.post(
        "/api/hci-sim/v1/simulations/test-runs/run-27123/result",
        json={"report_summary": {}, "report_digest": "sha256:fake"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "report_digest is generated by api-gateway"


def test_simulation_result_rejects_unbounded_summary_fields():
    """摘要不能成为存放命令原文、Lease 或环境数据的旁路。"""
    client = TestClient(app)
    response = client.post(
        "/api/hci-sim/v1/simulations/test-runs/run-27123/result",
        json={"report_summary": {"raw_output": "secret"}},
    )
    assert response.status_code == 400
    assert "unsupported fields" in response.json()["detail"]


def test_simulation_result_rejects_invalid_summary_types():
    """白名单字段也必须满足强类型和范围约束。"""
    client = TestClient(app)
    response = client.post(
        "/api/hci-sim/v1/simulations/test-runs/run-27123/result",
        json={
            "report_summary": {
                "case_id": "Q2026081100001",
                "conversation_id": "conversation-27123",
                "execution_mode": "sim-ssh",
                "command_count": 1,
                "failed_command_count": 2,
                "agent_stream_completed": True,
                "outcome": "failed",
            }
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "failed_command_count cannot exceed command_count"


def test_simulation_result_rejects_false_positive_passed_outcome():
    client = TestClient(app)
    response = client.post(
        "/api/hci-sim/v1/simulations/test-runs/run-27123/result",
        json={
            "outcome": "passed",
            "report_summary": {
                "case_id": "Q2026081100001",
                "conversation_id": "conversation-27123",
                "execution_mode": "sim-ssh",
                "command_count": 0,
                "failed_command_count": 0,
                "agent_stream_completed": True,
                "outcome": "passed",
            },
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "passed result requires at least one successful command"


def test_simulation_result_accepts_structured_inconclusive_without_commands():
    client = TestClient(app)
    runtime_response = JSONResponse({"status": "inconclusive"}, status_code=200)
    with patch("app.routes.simulations._post", new=AsyncMock(return_value=runtime_response)):
        response = client.post(
            "/api/hci-sim/v1/simulations/test-runs/run-27123/result",
            json={
                "outcome": "inconclusive",
                "report_summary": {
                    "case_id": "Q2026081100001",
                    "conversation_id": "conversation-27123",
                    "execution_mode": "sim-ssh",
                    "command_count": 0,
                    "failed_command_count": 0,
                    "agent_stream_completed": True,
                    "outcome": "inconclusive",
                },
            },
        )
    assert response.status_code == 200
