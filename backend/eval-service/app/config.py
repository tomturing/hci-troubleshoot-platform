"""
Eval Service Configuration
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Eval Service 配置"""

    SERVICE_NAME: str = "eval-service"
    SERVICE_PORT: int = 8007
    LOG_LEVEL: str = "INFO"

    # 数据库（eval-service 直接访问 assistant_evaluation 和 conversation 表）
    DATABASE_URL: str = "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot"

    # 管理接口鉴权 Token
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"

    # Prometheus 查询地址（agent_stats 接口拉取实时指标）
    PROMETHEUS_URL: str = "http://prometheus:9090"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
