"""
Scheduler Service Configuration
"""

import json
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings

# 默认AI助手注册表配置
DEFAULT_ASSISTANT_REGISTRY = {
    "htp-agent": {
        "name": "HTPAgent",
        "display_name": "HTP Agent (GLM-5)",
        "description": "HCI智能排障平台核心助手",
        "base_url": "https://coding.dashscope.aliyuncs.com/v1",
        "model": "glm-5",
        "warm_pool_size": 0,
        "max_pool_size": 0,
        "enabled": True,
        "is_default": True,
        "capabilities": ["troubleshooting"],
    },
    "ops-agent": {
        "name": "OpsAgent",
        "display_name": "Ops Agent (GLM-5)",
        "description": "基于SOP知识库的智能排障助手",
        "base_url": "http://ops-agent-service:8006",
        "warm_pool_size": 0,
        "max_pool_size": 0,
        "enabled": True,
        "is_default": False,
        "capabilities": ["troubleshooting"],
    },
    "pai-agent": {
        "name": "PydanticAI",
        "display_name": "PAI Agent (GLM-5)",
        "description": "基于pydantic-ai框架的排障助手",
        "base_url": "http://conversation-service:8002",
        "warm_pool_size": 0,
        "max_pool_size": 0,
        "enabled": True,
        "is_default": False,
        "capabilities": ["troubleshooting", "tool-calling"],
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

    # v2.1：助手选择器显示控制
    ASSISTANT_SHOW_SELECTOR: str = Field(
        default="auto",
        description="助手选择器显示模式: auto(智能判断多于1个可用助手时显示), true(强制显示), false(强制隐藏)"
    )

    @property
    def assistant_registry(self) -> dict[str, Any]:
        """解析AI助手注册表"""
        try:
            return json.loads(self.ASSISTANT_REGISTRY_JSON)
        except json.JSONDecodeError:
            return DEFAULT_ASSISTANT_REGISTRY

    def get_show_selector_mode(self) -> str:
        """获取助手选择器显示模式"""
        mode = self.ASSISTANT_SHOW_SELECTOR.lower()
        if mode not in ("auto", "true", "false"):
            return "auto"
        return mode

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
