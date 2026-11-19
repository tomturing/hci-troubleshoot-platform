"""WeKnora 知识库入口路由 — API 网关代理层

admin-ui 要在「知识库」页里内嵌两个东西：
- 知识库管理台（WeKnora 完整 SPA）；
- 知识问答面板（WeKnora 官方 embed 渠道，访客免登录）。

设计约束（详见 docs/solution/events/2026-11-19-WeKnora知识库集成.md）：

1. 两个入口都是**同源相对路径**。WeKnora 前端以 `VITE_BASE_PATH=/weknora/`
   构建，由 admin-ui nginx 反代并剥离前缀，浏览器地址栏始终是平台域名。
   因此这里不返回集群内 Service 地址，也不引入跨域 iframe。

2. 问答面板需要 embed token，但**长期令牌不得下发浏览器**：
   - `em_` publish token 只存在于集群 Secret，由本接口在服务端持有；
   - 每次请求用它调用上游 `POST /api/v1/embed/<channel>/exchange`，
     换取 30 分钟有效的 `ems_` 短令牌后再拼进 iframe URL。
   - 上游短令牌过期后，前端重新调用本接口即可，无需人工干预。

3. exchange 接口会校验请求 Origin 是否命中渠道白名单，因此必须显式转发
   `Origin: <WEKNORA_EMBED_ORIGIN>`（浏览器实际地址，含端口）。
   漏掉这个头会被上游按"未授权来源"拒绝，表现成 500/403，容易误判为服务故障。

4. 未创建渠道时（channelId / publishToken 为空）只下发管理台地址，
   问答入口返回 null；前端据此提示"尚未创建问答渠道"而不是报错。

安全约定：
- 强制 require_admin：知识库是全平台共享资产，问答面板也会消耗模型配额。
- 任何分支都不记录 token（publish 与 session 都不落日志），只记录事件名、
  状态码与调用链 ID。
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from app.config import settings
from app.security.gateway_auth import require_admin

router = APIRouter()
logger = get_logger("gateway-weknora")

# 上游 exchange 是集群内的轻量 POST，超时收紧；浏览器侧有"重新连接"入口可重试。
EXCHANGE_TIMEOUT_SECONDS = 5.0


def _embed_configured() -> bool:
    return bool(settings.WEKNORA_EMBED_CHANNEL_ID and settings.WEKNORA_EMBED_PUBLISH_TOKEN)


async def _issue_embed_session_token() -> str:
    """用 publish token 换短效 session token（ems_）。

    返回 token 本身不落日志；失败时抛出 503，前端会提示"问答渠道暂不可用"。
    """
    trace_id = get_current_trace_id()
    channel_id = settings.WEKNORA_EMBED_CHANNEL_ID
    url = f"{settings.WEKNORA_SERVICE_URL.rstrip('/')}/api/v1/embed/{channel_id}/exchange"
    headers = {
        "Authorization": f"Embed {settings.WEKNORA_EMBED_PUBLISH_TOKEN}",
        "Origin": settings.WEKNORA_EMBED_ORIGIN,
    }

    try:
        async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.error(
            event="weknora_embed_exchange_unreachable",
            message="请求 WeKnora embed exchange 失败",
            error_type=type(exc).__name__,
            trace_id=trace_id,
        )
        raise HTTPException(status_code=503, detail="知识库服务不可用") from exc

    if response.status_code != 200:
        # 常见原因：渠道被禁用、publish token 轮换、Origin 不在白名单。
        # 只记录状态码，绝不记录响应体（可能回显 token 片段）。
        logger.warning(
            event="weknora_embed_exchange_rejected",
            message="WeKnora 拒绝了 embed 令牌交换",
            upstream_status=response.status_code,
            trace_id=trace_id,
        )
        raise HTTPException(status_code=503, detail="知识库问答渠道暂不可用")

    try:
        payload = response.json()
        session_token = payload["data"]["session_token"]
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            event="weknora_embed_exchange_malformed",
            message="WeKnora embed exchange 响应缺少 session_token",
            trace_id=trace_id,
        )
        raise HTTPException(status_code=503, detail="知识库问答渠道暂不可用") from exc

    if not isinstance(session_token, str) or not session_token:
        raise HTTPException(status_code=503, detail="知识库问答渠道暂不可用")

    return session_token


@router.get(
    "/api/weknora/launch",
    dependencies=[Depends(require_admin)],
    tags=["weknora"],
)
async def weknora_launch() -> dict[str, str | None]:
    """下发知识库两个入口的同源地址。

    `embed_url` 为 None 表示尚未创建 embed 渠道（实例可用，问答未开通）；
    前端应把它当成"待配置"而不是错误。
    """
    console_url = settings.WEKNORA_PUBLIC_PATH
    if not _embed_configured():
        logger.info(
            event="weknora_launch_embed_not_configured",
            message="未配置 embed 渠道，仅下发管理台入口",
            trace_id=get_current_trace_id(),
        )
        return {"console_url": console_url, "embed_url": None}

    session_token = await _issue_embed_session_token()
    embed_url = (
        f"{settings.WEKNORA_PUBLIC_PATH}embed/{settings.WEKNORA_EMBED_CHANNEL_ID}"
        f"?token={session_token}"
    )
    logger.info(
        event="weknora_launch_issued",
        message="已下发知识库会话入口",
        trace_id=get_current_trace_id(),
    )
    return {"console_url": console_url, "embed_url": embed_url}
