"""
OpenClaw Client - 调用OpenClaw Pod API
"""

import httpx
from typing import AsyncGenerator, Dict, Any
import json

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.utils.logger import get_logger

logger = get_logger("openclaw-client")

class OpenClawClient:
    """OpenClaw API客户端"""
    
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=60.0)
    
    async def send_message(
        self,
        case_id: str,
        message: str,
        context: Dict[str, Any] = None
    ) -> AsyncGenerator[str, None]:
        """
        发送消息到OpenClaw并流式接收响应
        
        Args:
            case_id: 工单ID
            message: 用户消息
            context: 对话上下文
            
        Yields:
            str: AI响应片段
        """
        payload = {
            "case_id": case_id,
            "message": message,
            "context": context or {}
        }
        
        logger.info(
            event="openclaw_request",
            message="Sending message to OpenClaw",
            case_id=case_id
        )
        
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/chat",
                json=payload
            ) as response:
                response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if line:
                        yield line
        
        except Exception as e:
            logger.error(
                event="openclaw_error",
                message="Error calling OpenClaw",
                case_id=case_id,
                error=e
            )
            raise
    
    async def close(self):
        """关闭客户端"""
        await self.client.aclose()
