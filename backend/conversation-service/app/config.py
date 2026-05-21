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

    # LLM 配置（统一使用 dashscope 网关）
    LLM_BASE_URL: str = "https://coding.dashscope.aliyuncs.com/v1"
    LLM_API_KEY: str = ""  # 从 hci-secrets 注入
    LLM_DEFAULT_MODEL: str = "glm-5"

    # KB 服务配置
    KB_SERVICE_URL: str = "http://kb-service:8004"
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"
    # KB 检索参数
    KB_SEARCH_TOP_N: int = 5           # RRF 融合后取 top-N
    KB_CONTEXT_MAX_CHARS: int = 40000  # 注入 Prompt 的最大字符数
    # 40000 依据：GLM-5 128K context × 32K最优区间上限 × 1.5chars/token = 48K chars，
    # 保守取 40K，覆盖当前最大 SOP 文档(18843 chars) 并保留 2.1x 增长余量。
    # 推导：(128K - 4096出力 - 533静态段 - 1728历史20条) × (32K/128K) × 1.5 ≈ 40000
    KB_ENABLED: bool = True             # 是否启用 KB 注入（可通过环境变量动态关闭）

    # Scheduler 配置（用于真实 Pod 分配链路）
    SCHEDULER_SERVICE_URL: str = "http://scheduler-service:8003"
    SCHEDULER_ALLOCATE_TIMEOUT_SEC: int = 8
    SCHEDULER_POD_READY_TIMEOUT_SEC: int = 20
    SCHEDULER_POD_POLL_INTERVAL_SEC: float = 1.0

    # Case Service 配置（用于获取环境上下文）
    CASE_SERVICE_URL: str = "http://case-service:8001"
    ENVIRONMENT_CONTEXT_TIMEOUT_SEC: float = 5.0

    # 多助手注册表（可选覆盖）
    # JSON 格式：
    # {
    #   "htp-agent": {
    #     "base_url": "https://coding.dashscope.aliyuncs.com/v1",
    #     "api_key": "xxx",
    #     "model": "glm-5",
    #     "enabled": true
    #   }
    # }
    ASSISTANT_REGISTRY_JSON: str = "{}"

    # ── [PR-B] agent-service 集成配置 ───────────────────────────────────────
    AGENT_SERVICE_URL: str = "http://agent-service:8005"
    AGENT_SERVICE_ENABLED: bool = True  # false 时回退到直连 ai_registry 路径（兜底）

    @property
    def assistant_registry(self) -> dict[str, dict[str, Any]]:
        """解析助手注册表。优先从 ASSISTANT_REGISTRY_JSON 环境变量读取，与 scheduler-service 配置统一。

        过滤规则：
        1. 仅保留 cfg 为 dict 的有效条目（避免非 dict 配置导致 AttributeError）
        2. 排除 ops-agent（它通过 AgentRouter 独立路由，不通过 ai_registry）

        如果环境变量为空或解析失败，降级为默认 openclaw 配置。
        """
        # 优先使用环境变量注入的统一配置（来自 hci-common-config ASSISTANT_REGISTRY_JSON）
        try:
            registry = json.loads(self.ASSISTANT_REGISTRY_JSON or "{}")
            if isinstance(registry, dict) and registry:
                # 构造新的 dict：仅保留有效条目，排除 ops-agent
                valid_registry: dict[str, dict[str, Any]] = {}
                for atype, cfg in registry.items():
                    # 过滤条件 1：cfg 必须是 dict
                    if not isinstance(cfg, dict):
                        continue
                    # 过滤条件 2：排除 ops-agent（它通过 AgentRouter 独立路由）
                    if atype == "ops-agent":
                        continue
                    # 仅保留 enabled=true 或未配置 enabled 的条目
                    if cfg.get("enabled", True):
                        valid_registry[atype] = cfg

                # 如果过滤后仍有有效条目，返回过滤后的 registry
                if valid_registry:
                    return valid_registry
        except json.JSONDecodeError:
            pass

        # 降级：默认只有 htp-agent（向后兼容）
        default_registry: dict[str, dict[str, Any]] = {
            "htp-agent": {
                "base_url": self.LLM_BASE_URL,
                "api_key": self.LLM_API_KEY,
                "model": self.LLM_DEFAULT_MODEL,
                "enabled": True,
            }
        }
        return default_registry

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
