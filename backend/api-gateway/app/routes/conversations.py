"""
Conversation Routes - API Gateway Proxy
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from typing import Optional

from app.config import settings
from shared.utils.logger import get_logger

router = APIRouter(prefix="/api/conversations", tags=["conversations"])
logger = get_logger("gateway-conversations")

CONVERSATION_SERVICE_URL = f"http://conversation-service:{settings.CONVERSATION_SERVICE_PORT}/api/conversations"

async def proxy_request_stream(method: str, path: str, payload: dict, headers: dict):
    """Proxy request with response streaming (SSE)"""
    # SSE 场景下，默认 httpx timeout(约 5s) 很容易触发 ReadTimeout，且 str(e) 可能为空。
    # 这里禁用超时，让上游按实际流式节奏输出。
    client = httpx.AsyncClient(timeout=None)
    url = f"{CONVERSATION_SERVICE_URL}{path}"
    
    async def stream_generator():
        try:
            async with client.stream(method, url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    # 如果不是200，尝试读取错误信息并yield
                    content = await response.read()
                    yield (
                        b"event: error\n"
                        + b"data: {\"error\": \"upstream error\", \"status\": "
                        + str(response.status_code).encode("utf-8")
                        + b"}\n\n"
                        + content
                    )
                    return
                    
                async for chunk in response.aiter_bytes():
                    yield chunk
        except Exception as e:
            logger.error(
                event="gateway_streaming_error",
                message="Streaming error while proxying SSE",
                error_type=type(e).__name__,
                error_message=str(e),
                error_repr=repr(e),
                url=url,
            )
            # 以 SSE 形式通知客户端出错
            yield f"event: error\ndata: {{\"error\": \"Streaming failed\", \"type\": \"{type(e).__name__}\", \"message\": \"{str(e)}\"}}\n\n".encode('utf-8')
        finally:
            await client.aclose()
            
    return StreamingResponse(stream_generator(), media_type="text/event-stream")

async def proxy_request(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    params: Optional[dict] = None,
    headers: Optional[dict] = None
):
    """Standard proxy request"""
    async with httpx.AsyncClient() as client:
        try:
            url = f"{CONVERSATION_SERVICE_URL}{path}"
            response = await client.request(
                method,
                url,
                json=payload,
                params=params,
                headers=headers
            )
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
    return await proxy_request_stream(
        "POST",
        f"/{conversation_id}/message",
        payload,
        headers={}
    )
