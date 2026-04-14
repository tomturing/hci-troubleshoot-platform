"""
Scheduler Service Configuration
"""

import json
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings

# 默认AI助手注册表配置
DEFAULT_ASSISTANT_REGISTRY = {
    "openclaw": {
        "name": "OpenClaw",
        "description": "通用AI排障助手，基于GLM大模型",
        "image": "openclaw:latest",
        "port": 18789,
        "warm_pool_size": 2,
        "max_pool_size": 10,
        "enabled": True,
        "labels": {"app": "openclaw", "assistant-type": "openclaw"}
    }
}


class Settings(BaseSettings):
    """配置类"""

    SERVICE_NAME: str = "scheduler-service"
    SERVICE_PORT: int = 8003
    LOG_LEVEL: str = "INFO"

    # Redis配置（Pod分配状态持久化）
    REDIS_URL: str = "redis://redis:6379/0"

    # K8s配置
    K8S_NAMESPACE: str = "hci-troubleshoot"
    K8S_IMAGE_PULL_SECRET: str = ""  # GHCR 镜像拉取 Secret 名称，空字符串表示不配置
    K8S_IMAGE_PULL_POLICY: str = "IfNotPresent"  # 镜像拉取策略；latest tag 或线上环境建议设为 Always

    # Pod池全局配置
    POD_IDLE_TIMEOUT: int = 300  # 5分钟

    # AI助手注册表 (JSON字符串，支持环境变量注入)
    ASSISTANT_REGISTRY_JSON: str = Field(
        default=json.dumps(DEFAULT_ASSISTANT_REGISTRY),
        description="AI助手注册表，JSON格式"
    )

    @property
    def assistant_registry(self) -> dict[str, Any]:
        """解析AI助手注册表"""
        try:
            return json.loads(self.ASSISTANT_REGISTRY_JSON)
        except json.JSONDecodeError:
            return DEFAULT_ASSISTANT_REGISTRY

    # 向后兼容：保留旧配置项（已弃用）
    OPENCLAW_IMAGE: str = "openclaw:latest"
    WARM_POOL_SIZE: int = 2
    MAX_POOL_SIZE: int = 10

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"

settings = Settings()
