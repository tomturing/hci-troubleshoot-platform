"""
Assistants Routes - AI助手API路由 (v2.0)

代理到 Scheduler Service 的 /api/scheduler/assistants
"""

from fastapi import APIRouter, HTTPException
import httpx

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from app.config import settings
from shared.utils.logger import get_logger

logger = get_logger("gateway-assistants")

router = APIRouter(prefix="/api/assistants", tags=["assistants"])


@router.get("/")
async def list_assistants():
    """
    获取可用的AI助手列表
    
    代理到 scheduler-service /api/scheduler/assistants
    """
    url = f"{settings.SCHEDULER_SERVICE_URL}/api/scheduler/assistants"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            
        if response.status_code == 200:
            return response.json()
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
        # 降级: 返回默认助手列表 (不依赖 scheduler 可用性)
        return [
            {
                "type": "openclaw",
                "name": "OpenClaw",
                "description": "通用AI排障助手，基于GLM大模型",
                "enabled": True,
                "pool_stats": {}
            }
        ]
    except Exception as e:
        logger.error(
            event="assistants_proxy_exception",
            message=f"Error proxying assistants request: {e}",
            error=str(e)
        )
        raise HTTPException(status_code=502, detail="Failed to connect to scheduler service")
