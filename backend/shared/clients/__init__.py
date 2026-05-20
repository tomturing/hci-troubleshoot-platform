"""
共享客户端模块

提供各微服务共用的 HTTP 客户端：
- AIAssistantRegistry：AI 助手客户端注册表
- SchedulerClient：调度服务客户端
- KBClient：知识库服务客户端
"""

from .ai_client import AIAssistantRegistry, create_openclaw_client
from .scheduler_client import SchedulerClient
from .kb_client import KBClient

__all__ = [
    "AIAssistantRegistry",
    "create_openclaw_client",
    "SchedulerClient",
    "KBClient",
]
