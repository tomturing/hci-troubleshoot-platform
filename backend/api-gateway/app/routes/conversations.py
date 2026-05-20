"""
对话路由 — API 网关代理层

负责将对话相关请求代理到 conversation-service，处理 SSE 流式响应的透传。
"""

import json

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from shared.utils.exceptions import ErrorCode
from shared.observability.logger import get_logger

from app.config import settings

router = APIRouter(prefix="/api/conversations", tags=["conversations"])
logger = get_logger("gateway-conversations")

CONVERSATION_SERVICE_URL = f"{settings.CONVERSATION_SERVICE_URL}/api/conversations"


async def proxy_request_stream(method: str, path: str, payload: dict | None, headers: dict):
    """Proxy request with response streaming (SSE)"""
    # SSE 场景下，默认 httpx timeout(约 5s) 很容易触发 ReadTimeout，且 str(e) 可能为空。
    # 这里禁用超时，让上游按实际流式节奏输出。
    client = httpx.AsyncClient(timeout=None)
    url = f"{CONVERSATION_SERVICE_URL}{path}"

    async def stream_generator():
        try:
            # GET 请求不传 json body，避免部分服务端拒绝带 body 的 GET
            stream_kwargs = {"headers": headers}
            if payload is not None:
                stream_kwargs["json"] = payload
            async with client.stream(method, url, **stream_kwargs) as response:
                if response.status_code != 200:
                    # 使用 json.dumps 安全序列化错误信息
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
                event="gateway_timeout",
                message="Upstream timeout while proxying SSE",
                error_type=type(e).__name__,
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
            # 使用 json.dumps 安全序列化，避免 str(e) 中特殊字符破坏 SSE 帧结构
            error_data = json.dumps(
                {"code": ErrorCode.STREAMING_ERROR.value, "message": "流传输错误", "detail": type(e).__name__},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n".encode()
        finally:
            await client.aclose()

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


async def proxy_request(
    method: str, path: str, payload: dict | None = None, params: dict | None = None, headers: dict | None = None
):
    """Standard proxy request"""
    async with httpx.AsyncClient() as client:
        try:
            url = f"{CONVERSATION_SERVICE_URL}{path}"
            response = await client.request(method, url, json=payload, params=params, headers=headers)
            return response
        except httpx.RequestError as exc:
            logger.error(f"Error requesting {exc.request.url!r}.")
            raise HTTPException(status_code=503, detail="Service unavailable")


@router.post("/")
async def create_conversation(request: Request):
    """创建对话"""
    query_params = dict(request.query_params)
    response = await proxy_request("POST", "/", params=query_params)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request):
    """获取对话详情"""
    response = await proxy_request("GET", f"/{conversation_id}")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/case/{case_id}")
async def get_conversations_by_case(case_id: str, request: Request):
    """获取工单的所有对话"""
    response = await proxy_request("GET", f"/case/{case_id}")
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/{conversation_id}/messages")
async def get_messages(conversation_id: str, request: Request):
    """获取对话的消息历史"""
    response = await proxy_request("GET", f"/{conversation_id}/messages")
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.post("/{conversation_id}/message")
async def send_message(conversation_id: str, request: Request):
    """发送消息 (SSE流式返回)"""
    payload = await request.json()
    return await proxy_request_stream("POST", f"/{conversation_id}/message", payload, headers={})


@router.post("/{conversation_id}/interactive-response")
async def submit_interactive_response(conversation_id: str, request: Request):
    """提交 ops-agent 交互式响应（用户选择备选项后回传）"""
    payload = await request.json()
    response = await proxy_request("POST", f"/{conversation_id}/interactive-response", payload=payload)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/{conversation_id}/resume-stream")
async def resume_stream(conversation_id: str, request: Request):
    """重连 ops-agent outbox SSE 流（页面刷新后恢复会话续写）"""
    return await proxy_request_stream("GET", f"/{conversation_id}/resume-stream", payload=None, headers={})
