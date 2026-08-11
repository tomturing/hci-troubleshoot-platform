"""Admin UI 的 hci-sim 控制面代理。

浏览器不能直接访问 Runtime；Gateway 在服务间请求中注入控制面 Token，
并只暴露受限的 capability、build、TestRun 和 result 动作。TestRun 创建
由 Gateway 先创建平台真实工单，再把 case_id 绑定到 Runtime 上下文，禁止
Runtime 生成无法追踪的伪工单号。
"""

import contextlib
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
    return await _post(
        f"/v1/simulations/test-runs/{test_run_id}/result",
        await request.json(),
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
