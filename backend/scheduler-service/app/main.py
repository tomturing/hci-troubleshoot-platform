"""
Scheduler Service - 主应用

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
- 初始化 Redis 连接用于 Pod 分配状态持久化
- 添加后台初始化任务的异常处理，避免 fire-and-forget 静默失败
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from shared.database.redis import RedisManager
from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry, instrument_app

from app.config import settings
from app.routes import scheduler_routes
from app.services.k8s_client import K8sClient
from app.services.scheduler_service import SchedulerService

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(event="service_starting", message=f"Starting {settings.SERVICE_NAME}", port=settings.SERVICE_PORT)

    # 初始化 Redis
    redis_manager = RedisManager(settings.REDIS_URL)
    await redis_manager.connect()

    # 初始化 K8s 客户端和调度服务
    k8s_client = K8sClient()
    scheduler_service = SchedulerService(k8s_client, redis_manager)

    # 存入 app.state，供路由通过 request.app.state 访问
    app.state.redis_manager = redis_manager
    app.state.k8s_client = k8s_client
    app.state.scheduler_service = scheduler_service

    # 注入依赖到路由（兼容 Depends(get_service) 模式）
    scheduler_routes.set_scheduler_service(scheduler_service)

    # 后台初始化任务（带错误处理，避免 fire-and-forget 静默失败）
    async def _safe_start():
        try:
            await scheduler_service.start()
            logger.info(event="scheduler_started", message="Scheduler background initialization completed")
        except Exception as e:
            logger.error(
                event="scheduler_start_failed", message=f"Scheduler background initialization failed: {e}", error=str(e)
            )

    init_task = asyncio.create_task(_safe_start(), name="scheduler-init")

    yield

    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    # 取消未完成的初始化任务
    if not init_task.done():
        init_task.cancel()
    # 关闭 Redis
    await redis_manager.close()


app = FastAPI(
    title="HCI Troubleshoot - Scheduler Service",
    description="Pod调度与生命周期管理服务",
    version="2.0.0",
    lifespan=lifespan,
)

# 注入 OpenTelemetry 中间件到 app 实例
instrument_app(app)

# 注册路由
app.include_router(scheduler_routes.router)


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": settings.SERVICE_NAME}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.SERVICE_PORT, reload=True)
