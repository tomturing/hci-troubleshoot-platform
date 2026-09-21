"""
对话路由 - API 网关代理层

负责将对话相关请求代理到 conversation-service，处理 SSE 流式响应的透传。

安全审计 2026-08-19 修复：出口身份签名。网关是唯一允许对下游声明客户端
身份的组件——统一剥离用户传入的 X-Client-ID / X-Client-Signature，改用
共享密钥 HMAC 重签后注入（见 shared/security/signature.py），下游
conversation-service 据此做会话归属校验（IDOR 防护）。

P0 修复（2026-09）：client_id 不再来自用户自报的 X-Client-ID，而是来自
服务端签发的身份 Cookie（require_user 依赖解析），从根本上消除"自报他人
身份越权"。admin 只读路由改由 require_admin 强制 INTERNAL_API_TOKEN。
"""

import json

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from shared.observability.logger import get_logger
from shared.security.signature import sign_client_identity
from shared.utils.exceptions import ErrorCode

from app.config import settings
from app.security.gateway_auth import require_admin, require_user

router = APIRouter(prefix="/api/conversations", tags=["conversations"])
logger = get_logger("gateway-conversations")

CONVERSATION_SERVICE_URL = f"{settings.CONVERSATION_SERVICE_URL}/api/conversations"
MAX_EXEC_RESULT_BODY_BYTES = 2 * 1024 * 1024

# 用户可伪造的身份头变体（出口前一律剥离）
_IDENTITY_HEADERS = ("x-client-id", "x-client-signature")


def _signed_outbound_headers(headers: dict | None, client_id: str | None) -> dict:
    """剥离用户传入的身份头，注入网关 HMAC 签名（client_id 来自服务端 Cookie）。"""
    merged = {k: v for k, v in (headers or {}).items() if k.lower() not in _IDENTITY_HEADERS}
    if client_id:
        merged.update(sign_client_identity(client_id, settings.INTERNAL_API_TOKEN))
    return merged


async def _read_json_body_limited(request: Request, max_bytes: int) -> dict:
    """流式读取 JSON 并在解析前限制大小，避免大结果在 Gateway 内存中多次展开。"""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="执行结果回传超过 2 MiB 安全上限")
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length 非法") from None

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="执行结果回传超过 2 MiB 安全上限")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="请求 JSON 非法") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求 JSON 必须为对象")
    return payload


async def proxy_request_stream(
    method: str, path: str, payload: dict | None, headers: dict, client_id: str | None = None
):
    """Proxy request with response streaming (SSE)"""
    client = httpx.AsyncClient(timeout=None)
    url = f"{CONVERSATION_SERVICE_URL}{path}"
    headers = _signed_outbound_headers(headers, client_id)

    async def stream_generator():
        try:
            stream_kwargs = {"headers": headers}
            if payload is not None:
                stream_kwargs["json"] = payload
            async with client.stream(method, url, **stream_kwargs) as response:
                if response.status_code != 200:
                    error_data = json.dumps(
                        {
                            "code": ErrorCode.GATEWAY_ERROR.value,
                            "message": "上游服务暂时不可用",
                            "detail": f"status {response.status_code}",
                        },
                        ensure_ascii=False,
                    )
                    yield f"event: error\ndata: {error_data}\n\n".encode()
                    return
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.TimeoutException as e:
            logger.error(
                event="gateway_timeout", message="Upstream timeout while proxying SSE", error_type=type(e).__name__
            )
            error_data = json.dumps(
                {"code": ErrorCode.AI_TIMEOUT.value, "message": "上游服务响应超时", "detail": "gateway timeout"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n".encode()
        except Exception as e:
            logger.error(
                event="gateway_streaming_error",
                message="Streaming error while proxying SSE",
                error_type=type(e).__name__,
                error_message=str(e),
                error_repr=repr(e),
                url=url,
            )
            error_data = json.dumps(
                {"code": ErrorCode.STREAMING_ERROR.value, "message": "流传输错误", "detail": type(e).__name__},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n".encode()
        finally:
            await client.aclose()

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


async def proxy_request(
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    client_id: str | None = None,
):
    """Standard proxy request"""
    async with httpx.AsyncClient() as client:
        try:
            url = f"{CONVERSATION_SERVICE_URL}{path}"
            signed = _signed_outbound_headers(headers, client_id)
            response = await client.request(method, url, json=payload, params=params, headers=signed)
            return response
        except httpx.RequestError as exc:
            logger.error(f"Error requesting {exc.request.url!r}.")
            raise HTTPException(status_code=503, detail="Service unavailable")


@router.post("/")
async def create_conversation(request: Request, client_id: str = Depends(require_user)):
    """创建对话"""
    query_params = dict(request.query_params)
    response = await proxy_request("POST", "/", params=query_params, client_id=client_id)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, client_id: str = Depends(require_user)):
    """获取对话详情"""
    response = await proxy_request("GET", f"/{conversation_id}", client_id=client_id)
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JSONResponse(content=response.json(), status_code=response.status_code)


# ── Admin 只读路由（使用 INTERNAL_API_TOKEN 校验，置于动态路径之前避免冲突）──────


@router.get("/admin/cases/{case_id}/conversations")
async def admin_get_conversations_by_case(
    case_id: str,
    _: None = Depends(require_admin),
    authorization: str | None = Header(default=None),
):
    """
    [Admin] 查询指定工单的所有对话列表（绕过 client 身份签名）

    透传 Authorization: Bearer INTERNAL_API_TOKEN 到 conversation-service，
    由下游的 require_admin_token 依赖完成校验。
    管理后台专用，不携带 X-Client-ID，不限制工单归属。
    """
    headers = {}
    if authorization:
        headers["Authorization"] = authorization
    else:
        headers["Authorization"] = f"Bearer {settings.INTERNAL_API_TOKEN}"
    response = await proxy_request("GET", f"/admin/cases/{case_id}/conversations", headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/admin/conversations/{conversation_id}/messages")
async def admin_get_messages(
    conversation_id: str,
    _: None = Depends(require_admin),
    authorization: str | None = Header(default=None),
):
    """
    [Admin] 查询指定会话的消息历史（绕过 client 身份签名）

    透传 Authorization: Bearer INTERNAL_API_TOKEN 到 conversation-service，
    由下游的 require_admin_token 依赖完成校验。
    """
    headers = {}
    if authorization:
        headers["Authorization"] = authorization
    else:
        headers["Authorization"] = f"Bearer {settings.INTERNAL_API_TOKEN}"
    response = await proxy_request("GET", f"/admin/conversations/{conversation_id}/messages", headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


# ── 用户路由（需服务端身份 Cookie，按 case/conversation 归属校验）───────────


@router.get("/case/{case_id}")
async def get_conversations_by_case(case_id: str, client_id: str = Depends(require_user)):
    """获取工单的所有对话"""
    response = await proxy_request("GET", f"/case/{case_id}", client_id=client_id)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/{conversation_id}/messages")
async def get_messages(conversation_id: str, client_id: str = Depends(require_user)):
    """获取对话的消息历史"""
    response = await proxy_request("GET", f"/{conversation_id}/messages", client_id=client_id)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.post("/{conversation_id}/message")
async def send_message(conversation_id: str, request: Request, client_id: str = Depends(require_user)):
    """发送消息 (SSE流式返回)"""
    payload = await request.json()
    return await proxy_request_stream("POST", f"/{conversation_id}/message", payload, headers={}, client_id=client_id)


@router.post("/{conversation_id}/interactive-response")
async def submit_interactive_response(conversation_id: str, request: Request, client_id: str = Depends(require_user)):
    """提交 ops-agent 交互式响应（用户选择备选项后回传）"""
    payload = await request.json()
    response = await proxy_request(
        "POST", f"/{conversation_id}/interactive-response", payload=payload, client_id=client_id
    )
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.post("/{conversation_id}/exec-result")
async def submit_exec_result(conversation_id: str, request: Request, client_id: str = Depends(require_user)):
    """回传命令执行结果"""
    payload = await _read_json_body_limited(request, MAX_EXEC_RESULT_BODY_BYTES)
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    else:
        # 兜底注入 MVP 阶段临时 Token，绕过下游会话鉴权
        headers["Authorization"] = "Bearer client-session-placeholder-token"
    if traceparent := request.headers.get("traceparent"):
        headers["traceparent"] = traceparent
    if tracestate := request.headers.get("tracestate"):
        headers["tracestate"] = tracestate

    response = await proxy_request(
        "POST", f"/{conversation_id}/exec-result", payload=payload, headers=headers, client_id=client_id
    )
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.post("/{conversation_id}/vm-console-result")
async def submit_vm_console_result(conversation_id: str, request: Request, client_id: str = Depends(require_user)):
    """回传虚拟机控制台截图元数据结果（qkv_vm_console；不含图片字节）。"""
    payload = await _read_json_body_limited(request, MAX_EXEC_RESULT_BODY_BYTES)
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    else:
        # 兜底注入 MVP 阶段临时 Token，绕过下游会话鉴权（对齐 exec-result）
        headers["Authorization"] = "Bearer client-session-placeholder-token"
    if traceparent := request.headers.get("traceparent"):
        headers["traceparent"] = traceparent

    response = await proxy_request(
        "POST", f"/{conversation_id}/vm-console-result", payload=payload, headers=headers, client_id=client_id
    )
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/{conversation_id}/vm-console-artifacts/{artifact_id}")
async def download_vm_console_artifact(conversation_id: str, artifact_id: str, request: Request):
    """授权下载控制台截图制品（会话鉴权在 conversation-service 完成并记审计）。"""
    headers = {}
    if auth := request.headers.get("Authorization"):
        headers["Authorization"] = auth
    else:
        headers["Authorization"] = "Bearer client-session-placeholder-token"
    response = await proxy_request("GET", f"/{conversation_id}/vm-console-artifacts/{artifact_id}", headers=headers)
    media_type = response.headers.get("content-type", "application/octet-stream")
    return Response(content=response.content, status_code=response.status_code, media_type=media_type)


@router.get("/{conversation_id}/resume-stream")
async def resume_stream(conversation_id: str, request: Request, client_id: str = Depends(require_user)):
    """重连 ops-agent outbox SSE 流（页面刷新后恢复会话续写）"""
    return await proxy_request_stream(
        "GET", f"/{conversation_id}/resume-stream", payload=None, headers={}, client_id=client_id
    )


@router.post("/{conversation_id}/evaluate")
async def submit_evaluation(conversation_id: str, request: Request, client_id: str = Depends(require_user)):
    """提交用户评分"""
    payload = await request.json()
    response = await proxy_request("POST", f"/{conversation_id}/evaluate", payload=payload, client_id=client_id)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/{conversation_id}/evaluation")
async def get_evaluation(conversation_id: str, request: Request, client_id: str = Depends(require_user)):
    """获取对话的评分信息"""
    response = await proxy_request("GET", f"/{conversation_id}/evaluation", client_id=client_id)
    return JSONResponse(content=response.json(), status_code=response.status_code)
