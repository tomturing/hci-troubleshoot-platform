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
    #   "openclaw": {
    #     "base_url": "http://openclaw:18789",
    #     "gateway_token": "xxx",
    #     "model": "openclaw",
    #     "enabled": true
    #   }
    # }
    ASSISTANT_REGISTRY_JSON: str = "{}"

    # ── Phase 3：ReAct 引擎配置 ──────────────────────────────────────────────
    # GLMClient 使用 OPENCLAW_BASE_URL + OPENCLAW_GATEWAY_TOKEN，
    # 单独的 API Key 环境变量兼容裸 GLM 直连场景
    OPENCLAW_API_KEY: str = ""           # 若非空则优先于 OPENCLAW_GATEWAY_TOKEN
    GLM_MODEL: str = "glm-4-flash"       # GLM 模型名称

    # SCP REST API 配置（深信服 HCI 管理平台）
    SCP_BASE_URL: str = ""               # 如 http://192.168.1.100:8082
    SCP_API_KEY: str = ""                # x-auth-token 头部认证 Key

    # ── T1: ops-agent 大脑可选集成 ──────────────────────────────────────────
    OPS_AGENT_BASE_URL: str = "http://ops-agent-service:8006"  # ops-agent ClusterIP
    OPS_AGENT_ENABLED: bool = False  # 默认关闭，OPS_AGENT_ENABLED=true 时启用

    # ── pydantic-ai C 大脑可选集成（A/B/C 三向测试）──────────────────────────────────
    PYDANTIC_AI_ENABLED: bool = False  # 默认关闭，PYDANTIC_AI_ENABLED=true 时启用

    # ReAct 是否启用（需要 SCP_BASE_URL + SCP_API_KEY 同时非空才生效）
    REACT_ENABLED: bool = False

    # 人工确认超时（秒）
    CONFIRM_TIMEOUT_SEC: int = 120

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

        # 降级：默认只有 openclaw（向后兼容）
        default_registry: dict[str, dict[str, Any]] = {
            "openclaw": {
                "base_url": self.OPENCLAW_BASE_URL,
                "gateway_token": self.OPENCLAW_GATEWAY_TOKEN,
                "model": self.OPENCLAW_DEFAULT_MODEL,
                "enabled": True,
            }
        }
        return default_registry

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
