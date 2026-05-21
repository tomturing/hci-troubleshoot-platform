"""
Agent Service Configuration
"""

import json
from typing import Any

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Agent Service 配置"""

    SERVICE_NAME: str = "agent-service"
    SERVICE_PORT: int = 8005
    LOG_LEVEL: str = "INFO"

    # Redis（用于 confirm_service / dialog_tools BRPOP）
    REDIS_URL: str = "redis://redis:6379/0"

    # LLM 配置（统一使用 dashscope 网关）
    LLM_BASE_URL: str = "https://coding.dashscope.aliyuncs.com/v1"
    LLM_API_KEY: str = ""  # 从 hci-secrets 注入
    LLM_DEFAULT_MODEL: str = "glm-5"

    # GLM 模型（ReAct 引擎，从 configmap 注入）
    GLM_MODEL: str = "glm-5"

    # KB 服务配置
    KB_SERVICE_URL: str = "http://kb-service:8004"
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"
    KB_SEARCH_TOP_N: int = 5
    KB_CONTEXT_MAX_CHARS: int = 40000
    KB_ENABLED: bool = True

    # Scheduler 配置（HTP 大脑调用 ProductionClaw/LearningClaw）
    SCHEDULER_SERVICE_URL: str = "http://scheduler-service:8003"
    SCHEDULER_ALLOCATE_TIMEOUT_SEC: int = 8
    SCHEDULER_POD_READY_TIMEOUT_SEC: int = 20
    SCHEDULER_POD_POLL_INTERVAL_SEC: float = 1.0

    # ── ops-agent 大脑集成 ──────────────────────────────────────────────────
    OPS_AGENT_BASE_URL: str = "http://ops-agent-service:8006"
    OPS_AGENT_ENABLED: bool = False
    OPS_AGENT_READ_TIMEOUT_SEC: float = 300.0
    OPS_AGENT_FALLBACK_ASSISTANT_TYPE: str = "glm-5"

    # pydantic-ai C 大脑集成
    PYDANTIC_AI_ENABLED: bool = False

    # ReAct 引擎开关
    REACT_ENABLED: bool = False

    # 人工确认超时（秒）
    CONFIRM_TIMEOUT_SEC: int = 120

    # 多助手注册表（与 conversation-service 保持同步）
    ASSISTANT_REGISTRY_JSON: str = "{}"

    @property
    def assistant_registry(self) -> dict[str, dict[str, Any]]:
        """解析助手注册表。"""
        try:
            registry = json.loads(self.ASSISTANT_REGISTRY_JSON or "{}")
            if isinstance(registry, dict) and registry:
                valid: dict[str, dict[str, Any]] = {}
                for atype, cfg in registry.items():
                    if not isinstance(cfg, dict):
                        continue
                    if atype == "ops-agent":
                        continue
                    if cfg.get("enabled", True):
                        valid[atype] = cfg
                if valid:
                    return valid
        except json.JSONDecodeError:
            pass
        return {
            "htp-agent": {
                "base_url": self.LLM_BASE_URL,
                "api_key": self.LLM_API_KEY,
                "model": self.LLM_DEFAULT_MODEL,
                "enabled": True,
            }
        }

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
