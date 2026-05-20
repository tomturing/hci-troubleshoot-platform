"""
Assistants Routes - AI助手API路由 (v2.1)

代理到 Scheduler Service 的 /api/scheduler/assistants
返回结构化响应：{assistants, show_selector, default_assistant, selector_mode}
"""

import httpx
from fastapi import APIRouter, HTTPException
from shared.observability.logger import get_logger

from app.config import settings

logger = get_logger("gateway-assistants")

router = APIRouter(prefix="/api/assistants", tags=["assistants"])


@router.get("/")
async def list_assistants():
    """
    获取可用的AI助手列表（v2.1 结构化响应）

    代理到 scheduler-service /api/scheduler/assistants
    返回: {assistants: [...], show_selector: bool, default_assistant: str|null, selector_mode: str}
    """
    url = f"{settings.SCHEDULER_SERVICE_URL}/api/scheduler/assistants"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)

        if response.status_code == 200:
            data = response.json()

            # 处理新格式（结构化对象）
            if isinstance(data, dict) and "assistants" in data:
                normalized_assistants = []
                for item in data.get("assistants", []):
                    if not isinstance(item, dict):
                        continue
                    normalized_assistants.append({
                        "type": item.get("type", "openclaw"),
                        "display_name": item.get("display_name") or item.get("name") or item.get("type", "openclaw"),
                        "description": item.get("description", ""),
                        "capabilities": item.get("capabilities", []),
                        "available": item.get("available", item.get("enabled", True)),
                        "is_default": item.get("is_default", False),
                        "pool_stats": item.get("pool_stats", {}),
                    })
                return {
                    "assistants": normalized_assistants,
                    "show_selector": data.get("show_selector", False),
                    "default_assistant": data.get("default_assistant"),
                    "selector_mode": data.get("selector_mode", "auto"),
                }

            # 兼容旧格式（列表）- 转换为新格式
            elif isinstance(data, list):
                normalized_assistants = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    normalized_assistants.append({
                        "type": item.get("type", "openclaw"),
                        "display_name": item.get("display_name") or item.get("name") or item.get("type", "openclaw"),
                        "description": item.get("description", ""),
                        "capabilities": item.get("capabilities", []),
                        "available": item.get("available", item.get("enabled", True)),
                        "is_default": False,
                        "pool_stats": item.get("pool_stats", {}),
                    })
                available_count = sum(1 for a in normalized_assistants if a["available"])
                return {
                    "assistants": normalized_assistants,
                    "show_selector": available_count > 1,
                    "default_assistant": normalized_assistants[0]["type"] if normalized_assistants else None,
                    "selector_mode": "auto",
                }

            return {
                "assistants": [],
                "show_selector": False,
                "default_assistant": None,
                "selector_mode": "auto",
            }
        else:
            logger.error(
                event="assistants_proxy_error",
                message=f"Scheduler returned {response.status_code}",
                status=response.status_code
            )
            raise HTTPException(
                status_code=response.status_code,
                detail="Failed to fetch assistants from scheduler"
            )
    except httpx.ConnectError:
        logger.warning(
            event="scheduler_unreachable",
            message="Scheduler service unreachable, returning default assistants"
        )
        # 降级响应：单助手，不显示选择器
        return {
            "assistants": [{
                "type": "openclaw",
                "display_name": "OpenClaw",
                "description": "通用AI排障助手，基于GLM大模型",
                "capabilities": ["troubleshooting"],
                "available": True,
                "is_default": True,
                "pool_stats": {},
            }],
            "show_selector": False,
            "default_assistant": "openclaw",
            "selector_mode": "auto",
        }
    except HTTPException:
        # 直接重新抛出 HTTPException
        raise
    except Exception as e:
        logger.error(
            event="assistants_proxy_exception",
            message=f"Error proxying assistants request: {e}",
            error=str(e)
        )
        raise HTTPException(status_code=502, detail="Failed to connect to scheduler service")
