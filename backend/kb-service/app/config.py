"""
KB Service Configuration
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """KB Service 配置"""

    SERVICE_NAME: str = "kb-service"
    SERVICE_PORT: int = 8004
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str = "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot"

    # ---- Embedding 配置 ----
    # 主力：z.ai API（与其他服务使用同一 AI 服务层）
    ZAI_BASE_URL: str = "http://host.docker.internal:18790"
    ZAI_API_KEY: str = "default_token"
    ZAI_EMBEDDING_MODEL: str = "embedding-3"          # z.ai embedding 模型

    # 降级：本地 bge-small-zh-v1.5（网络故障时使用）
    BGE_MODEL_PATH: str = "/models/bge-small-zh-v1.5"  # 容器内路径
    EMBEDDING_DIM: int = 512                            # 向量维度（bge-small-zh-v1.5 实际输出 512 维）

    # Embedding 超时（超时后自动降级到本地模型）
    EMBEDDING_TIMEOUT_SEC: float = 5.0

    # ---- 分块配置 ----
    CHUNK_SIZE: int = 512                               # 块大小（tokens）
    CHUNK_OVERLAP: int = 128                            # 重叠大小（tokens）

    # ---- 检索配置 ----
    BM25_TOP_K: int = 20                                # BM25 初始召回数
    VECTOR_TOP_K: int = 20                              # 向量初始召回数
    RRF_K: int = 60                                     # RRF 融合参数
    RERANK_THRESHOLD: float = 0.5                       # Reranker 过滤阈值（<0.5 丢弃）
    DEFAULT_SEARCH_TOP_N: int = 5                       # 最终返回的 chunk 数

    # ---- 内部鉴权 ----
    # LearningClaw/ProductionClaw 调用 KB Service 时携带此 Token
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"

    # ---- SOP Skills 文件路径 ----
    SOP_SKILLS_DIR: str = "/data/sop_skills"            # 容器内 SOP 文件挂载路径

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
