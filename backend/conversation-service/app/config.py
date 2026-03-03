"""
Conversation Service Configuration
"""

import json
from typing import Any

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """配置类"""

    SERVICE_NAME: str = "conversation-service"
    SERVICE_PORT: int = 8002
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str = "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot"
    REDIS_URL: str = "redis://redis:6379/0"

    # OpenClaw配置
    OPENCLAW_BASE_URL: str = "http://host.docker.internal:18790"
    OPENCLAW_GATEWAY_TOKEN: str = "default_token"
    OPENCLAW_DEFAULT_MODEL: str = "openclaw"

    # Scheduler 配置（用于真实 Pod 分配链路）
    SCHEDULER_SERVICE_URL: str = "http://scheduler-service:8003"
    SCHEDULER_ALLOCATE_TIMEOUT_SEC: int = 8
    SCHEDULER_POD_READY_TIMEOUT_SEC: int = 20
    SCHEDULER_POD_POLL_INTERVAL_SEC: float = 1.0

    # 多助手注册表（可选覆盖）
    # JSON 格式：
    # {
    #   "openclaw": {
    #     "base_url": "http://openclaw:18789",
    #     "gateway_token": "xxx",
    #     "model": "openclaw",
    #     "enabled": true
    #   }
    # }
    ASSISTANT_REGISTRY_JSON: str = "{}"

    @property
    def assistant_registry(self) -> dict[str, dict[str, Any]]:
        """解析助手注册表并与默认 openclaw 配置合并。"""
        default_registry: dict[str, dict[str, Any]] = {
            "openclaw": {
                "base_url": self.OPENCLAW_BASE_URL,
                "gateway_token": self.OPENCLAW_GATEWAY_TOKEN,
                "model": self.OPENCLAW_DEFAULT_MODEL,
                "enabled": True,
            }
        }
        try:
            custom = json.loads(self.ASSISTANT_REGISTRY_JSON or "{}")
            if isinstance(custom, dict):
                for assistant_type, cfg in custom.items():
                    if isinstance(cfg, dict):
                        merged = {**default_registry.get(assistant_type, {}), **cfg}
                        default_registry[assistant_type] = merged
        except json.JSONDecodeError:
            pass
        return default_registry

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
