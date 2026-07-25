"""
KB Service Configuration

包含：
  - Settings：Pydantic Settings 配置类
  - settings：全局配置实例
  - sop_template_rules.yaml 验证规则加载器（子模块）
"""

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """KB Service 配置"""

    SERVICE_NAME: str = "kb-service"
    SERVICE_PORT: int = 8004
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str = "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot"

    # ---- Embedding 配置 ----
    # 复用 hci-common-config 中已注入的 LLM 公共配置：
    #   LLM_BASE_URL  — 来自 configmap（DashScope / OpenClaw 网关）
    #   LLM_API_KEY   — 来自 secret（与 agent-service 共用同一 API Key）
    # 原 ZAI_BASE_URL / ZAI_API_KEY 从未注入到 Pod，导致 embedding 始终失败并降级 hash。
    LLM_BASE_URL: str = "http://host.docker.internal:18790"  # 开发默认值；生产由 configmap 覆盖
    LLM_API_KEY: str = ""  # 生产由 secret 覆盖
    LLM_EMBEDDING_MODEL: str = "embedding-3"  # 由 configmap LLM_EMBEDDING_MODEL 覆盖

    EMBEDDING_DIM: int = 1536  # 向量维度（与 DB Vector(1536) 保持一致）

    # Embedding 超时；失败后搜索走词法检索，入库保存 NULL
    EMBEDDING_TIMEOUT_SEC: float = 5.0

    # ---- 分块配置 ----
    CHUNK_SIZE: int = 512  # 块大小（tokens）
    CHUNK_OVERLAP: int = 128  # 重叠大小（tokens）

    # ---- 检索配置 ----
    BM25_TOP_K: int = 20  # BM25 初始召回数
    VECTOR_TOP_K: int = 20  # 向量初始召回数
    RRF_K: int = 60  # RRF 融合参数
    RERANK_THRESHOLD: float = 0.5  # Reranker 过滤阈值（<0.5 丢弃）
    KBD_MIN_SIMILARITY: float = Field(0.3, ge=0.0, le=1.0)  # KBD 向量候选最低余弦相似度
    DEFAULT_SEARCH_TOP_N: int = 5  # 最终返回的 chunk 数

    # ---- 内部鉴权 ----
    # LearningClaw/ProductionClaw 调用 KB Service 时携带此 Token
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"

    model_config = ConfigDict(env_file=".env", case_sensitive=True, extra="ignore")


settings = Settings()
