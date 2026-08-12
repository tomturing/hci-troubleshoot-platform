"""Admin UI 的 hci-sim 控制面代理。

浏览器不能直接访问 Runtime；Gateway 在服务间请求中注入控制面 Token，
并只暴露受限的 capability、build、TestRun 和 result 动作。TestRun 创建
由 Gateway 先创建平台真实工单，再把 case_id 绑定到 Runtime 上下文，禁止
Runtime 生成无法追踪的伪工单号。
"""

import contextlib
import hashlib
import json

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings

router = APIRouter(prefix="/api/hci-sim", tags=["hci-sim"])


async def _post(path: str, payload: dict, idempotency_key: str | None = None) -> JSONResponse:
    headers = {"Content-Type": "application/json"}
    if settings.HCI_SIM_CONTROL_TOKEN:
        headers["Authorization"] = f"Bearer {settings.HCI_SIM_CONTROL_TOKEN}"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{settings.HCI_SIM_URL.rstrip('/')}{path}", json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="hci-sim Runtime unavailable") from exc
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text}
    return JSONResponse(content=body, status_code=response.status_code)


async def _case_request(method: str, path: str, payload: dict | None = None) -> JSONResponse:
    """调用平台 Case Service，保留其结构化错误响应。"""
    url = f"{settings.CASE_SERVICE_URL.rstrip('/')}/api/cases{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            kwargs = {"headers": {"Content-Type": "application/json"}}
            if payload is not None:
                kwargs["json"] = payload
            response = await client.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="case-service unavailable") from exc
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text}
    return JSONResponse(content=body, status_code=response.status_code)


def _json_response_body(response: JSONResponse) -> dict:
    """读取内部 JSONResponse，不把响应对象或凭据传入下游。"""
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@router.post("/v1/simulations/build")
async def build(request: Request) -> JSONResponse:
    return await _post("/v1/simulations/build", await request.json(), request.headers.get("Idempotency-Key"))


@router.get("/v1/simulations/capabilities/{kbd_id}")
async def capability(kbd_id: str) -> JSONResponse:
    """代理 Runtime capability 预检，供 Admin UI 在构建前展示可审计原因。"""
    return await _get(f"/v1/simulations/capabilities/{kbd_id}")


@router.post("/v1/simulations/test-runs")
async def create_test_run(request: Request) -> JSONResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="simulation TestRun payload must be an object")
    title = str(payload.get("title", "")).strip()
    description = str(payload.get("description", "")).strip()
    if not title or not description:
        raise HTTPException(status_code=400, detail="title and description are required")

    # 首次请求创建真实平台工单；重试请求如果带回既有 case_id，必须复用原工单，
    # 不能先创建第二个 Case 再让 Runtime 拒绝跨 Case 绑定。
    requested_case_id = str(payload.get("case_id") or "").strip()
    created_case = not requested_case_id
    if requested_case_id:
        case_response = await _case_request("GET", f"/{requested_case_id}")
    else:
        case_payload = {
            "client_id": str(payload.get("client_id") or "hci-sim-admin").strip(),
            "title": title,
            "description": description,
            "assistant_type": payload.get("assistant_type") or "htp-agent",
        }
        case_response = await _case_request("POST", "/", case_payload)
    if case_response.status_code >= 400:
        return case_response
    case_body = _json_response_body(case_response)
    case_id = str(case_body.get("case_id", "")).strip()
    if not case_id:
        raise HTTPException(status_code=502, detail="case-service response missing case_id")

    runtime_payload = dict(payload)
    runtime_payload["case_id"] = case_id
    connection = dict(runtime_payload.get("connection") or {})
    connection["case_id"] = case_id
    runtime_payload["connection"] = connection
    environment_context = dict(runtime_payload.get("environment_context") or {})
    environment_context["case_id"] = case_id
    runtime_payload["environment_context"] = environment_context
    runtime_response = await _post(
        "/v1/simulations/test-runs", runtime_payload, request.headers.get("Idempotency-Key")
    )
    if runtime_response.status_code >= 400:
        if created_case:
            with contextlib.suppress(HTTPException):
                await _case_request("PUT", f"/{case_id}/close", {"close_reason": "abandon"})
        return runtime_response

    runtime_body = _json_response_body(runtime_response)
    runtime_body["case_id"] = case_id
    runtime_body["case"] = case_body
    return JSONResponse(content=runtime_body, status_code=runtime_response.status_code)


@router.post("/v1/simulations/test-runs/{test_run_id}/result")
async def record_test_run_result(test_run_id: str, request: Request) -> JSONResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="simulation result payload must be an object")

    # report_digest 只是报告内容的完整性摘要，并不是客户端签名。由浏览器生成既会
    # 受 HTTP 非安全上下文限制，也允许客户端提交与报告不一致的摘要。因此 Gateway
    # 只接受结构化摘要，在信任边界内规范化并计算 digest，再转发给 Runtime。
    report_summary = payload.pop("report_summary", None)
    if not isinstance(report_summary, dict):
        raise HTTPException(status_code=400, detail="report_summary must be an object")
    if payload.get("report_digest"):
        raise HTTPException(status_code=400, detail="report_digest is generated by api-gateway")

    allowed_fields = {
        "case_id",
        "conversation_id",
        "execution_mode",
        "command_count",
        "failed_command_count",
        "agent_stream_completed",
        "outcome",
    }
    unknown_fields = sorted(set(report_summary) - allowed_fields)
    if unknown_fields:
        raise HTTPException(
            status_code=400,
            detail=f"report_summary contains unsupported fields: {', '.join(unknown_fields)}",
        )
    missing_fields = sorted(allowed_fields - set(report_summary))
    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"report_summary missing required fields: {', '.join(missing_fields)}",
        )
    for field in ("case_id", "conversation_id"):
        value = report_summary[field]
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise HTTPException(status_code=400, detail=f"report_summary.{field} must be a non-empty string")
    if report_summary["execution_mode"] != "sim-ssh":
        raise HTTPException(status_code=400, detail="report_summary.execution_mode must be sim-ssh")
    for field in ("command_count", "failed_command_count"):
        value = report_summary[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10000:
            raise HTTPException(status_code=400, detail=f"report_summary.{field} must be an integer from 0 to 10000")
    if report_summary["failed_command_count"] > report_summary["command_count"]:
        raise HTTPException(status_code=400, detail="failed_command_count cannot exceed command_count")
    if report_summary["agent_stream_completed"] is not True:
        raise HTTPException(status_code=400, detail="report_summary.agent_stream_completed must be true")
    outcome = report_summary["outcome"]
    if outcome not in {"passed", "failed", "inconclusive"}:
        raise HTTPException(status_code=400, detail="report_summary.outcome must be passed, failed, or inconclusive")
    if payload.get("outcome") != outcome:
        raise HTTPException(status_code=400, detail="result outcome must match report_summary.outcome")
    if outcome == "passed" and (
        report_summary["command_count"] < 1 or report_summary["failed_command_count"] != 0
    ):
        raise HTTPException(status_code=400, detail="passed result requires at least one successful command")
    if outcome == "failed" and report_summary["failed_command_count"] < 1:
        raise HTTPException(status_code=400, detail="failed result requires failed_command_count greater than zero")
    encoded_summary = json.dumps(
        report_summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded_summary) > 4096:
        raise HTTPException(status_code=413, detail="report_summary exceeds 4096 bytes")
    payload["report_digest"] = f"sha256:{hashlib.sha256(encoded_summary).hexdigest()}"
    return await _post(
        f"/v1/simulations/test-runs/{test_run_id}/result",
        payload,
        request.headers.get("Idempotency-Key"),
    )


async def _get(path: str) -> JSONResponse:
    """执行带控制面 Token 的 Runtime GET 请求。"""
    headers = {}
    if settings.HCI_SIM_CONTROL_TOKEN:
        headers["Authorization"] = f"Bearer {settings.HCI_SIM_CONTROL_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.HCI_SIM_URL.rstrip('/')}{path}", headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="hci-sim Runtime unavailable") from exc
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text}
    return JSONResponse(content=body, status_code=response.status_code)
