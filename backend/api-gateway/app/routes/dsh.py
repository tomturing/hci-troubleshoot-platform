"""
DSH 诊断入口路由 — API 网关代理层

把 DSH（DeepSeek Harness）的 Web UI 会话入口下发给 admin-ui，供其 iframe 内嵌。

设计约束（详见 docs/solution/events/2026-10-22-DSH诊断集成.md）：
- DSH 无匿名访问模式：进入其 Web UI 必须携带 launch token 完成一次交换，
  以换取上游签发的 session cookie，缺失时上游直接返回 401。
- launch token 是上游的**进程级凭据**，每次 DSH 进程启动都随机生成，且不可
  通过环境变量固定，只能运行时抓取。因此本接口不做缓存，每次请求实时向
  上游拉取，天然跟随 DSH 的 Pod 重建。

安全约定（不可弱化）：
- 持有 token 等同于取得该 Agent 的完整控制权（上游保留 shell / ssh /
  computer-use 能力），因此本接口强制 require_admin，
  未携带管理员 JWT 的调用方一律 403。
- token 本身绝不写入日志，日志只记录事件名、状态码与调用链 ID。
- 回传的是同源相对路径，不暴露集群内 Service 地址。
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from app.config import settings
from app.security.gateway_auth import require_admin

router = APIRouter()
logger = get_logger("gateway-dsh")

# 上游 token 交换接口是集群内的轻量 GET，超时不宜放宽，避免浏览器长时间等待
TOKEN_FETCH_TIMEOUT_SECONDS = 5.0


@router.api_route(
    "/api/dsh/launch",
    methods=["GET"],
    dependencies=[Depends(require_admin)],
    tags=["dsh"],
)
async def dsh_launch():
    """取得 DSH Web UI 的会话入口地址（内含一次性 launch token）。

    返回的是同源相对路径，admin-ui 前端直接作为 iframe 的 src 使用；
    浏览器完成 token 交换后上游会 302 到干净路径，token 随即从地址栏消失。
    """
    trace_id = get_current_trace_id()

    try:
        async with httpx.AsyncClient(timeout=TOKEN_FETCH_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{settings.DSH_SERVICE_URL}/__internal/token")
    except httpx.HTTPError as exc:
        # 只记录异常类型，不记录任何可能携带凭据的内容
        logger.error(
            event="dsh_launch_upstream_unreachable",
            message="请求 DSH token 接口失败",
            error_type=type(exc).__name__,
            trace_id=trace_id,
        )
        raise HTTPException(status_code=503, detail="DSH 服务不可用")

    token = response.text.strip()
    if response.status_code != 200 or not token:
        # DSH 进程刚启动、尚未打印就绪行时 token 文件还不存在，属于正常的中间态，
        # 前端应据此提示"DSH 正在启动"而不是当作鉴权失败。
        logger.warning(
            event="dsh_launch_token_not_ready",
            message="DSH 尚未注入 launch token",
            upstream_status=response.status_code,
            trace_id=trace_id,
        )
        raise HTTPException(status_code=503, detail="DSH 服务尚未就绪")

    logger.info(
        event="dsh_launch_token_issued",
        message="已下发 DSH 会话入口",
        trace_id=trace_id,
    )
    return {"url": f"{settings.DSH_PUBLIC_PATH}?token={token}"}
