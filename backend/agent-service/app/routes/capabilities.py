"""Agent 可执行 Capability 运行时发现。

该端点只报告当前进程内真实导入的 Validator/Handler 和执行桥状态，不读取 Admin 可
编辑配置，也不把“Schema 已声明”冒充为“现场可执行”。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.tools.qfk.handlers import HandlerRegistry
from app.tools.qfk.signal import BackendSignal
from app.tools.qkv.signal import FrontendSignal

router = APIRouter(prefix="/internal", tags=["capabilities"])


def _check_internal_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header.split(" ", 1)[1] != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="内部 Token 无效")


def runtime_capability_document() -> dict:
    """从当前 Agent 进程构造确定性的 Handler/Validator 部署快照。"""

    from app.tools.acli import executor as executor_module

    bridge_ready = executor_module._executor is not None
    qkv_tools = ("qkv_alert", "qkv_task", "qkv_dialog")
    qfk_tools = tuple(f"qfk_{namespace}" for namespace in HandlerRegistry.supported_namespaces())
    capabilities = []
    for capability_id in (*qkv_tools, *qfk_tools):
        producer = capability_id.startswith("qkv_")
        handler_ready = producer or capability_id.removeprefix("qfk_") in HandlerRegistry.supported_namespaces()
        validator_ready = FrontendSignal is not None if producer else BackendSignal is not None
        usable = handler_ready and validator_ready and bridge_ready
        capabilities.append(
            {
                "capability_id": capability_id,
                "implemented": handler_ready,
                "deployed": True,
                "validator_ready": validator_ready,
                "executor_ready": bridge_ready,
                "usable": usable,
                "runtime_status": "available" if usable else "degraded",
                "reason": None if usable else "Terminal Bridge Executor 尚未注入，参数可校验但暂不可执行",
            }
        )
    return {
        "schema_version": 1,
        "service": settings.SERVICE_NAME,
        "capabilities": capabilities,
        "count": len(capabilities),
    }


@router.get("/capabilities")
async def get_runtime_capabilities(request: Request) -> dict:
    """返回当前 Agent Pod 的真实 Capability 部署状态。"""

    _check_internal_auth(request)
    return runtime_capability_document()
