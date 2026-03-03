"""
API Gateway Configuration
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """配置类"""

    # 服务配置
    SERVICE_NAME: str = "api-gateway"
    SERVICE_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Redis配置
    REDIS_URL: str = "redis://redis:6379/0"

    # CORS 允许的来源（逗号分隔，支持环境变量覆盖）
    ALLOWED_ORIGINS: str = "http://localhost:3001,http://localhost:3002"

    # 下游服务地址
    CASE_SERVICE_URL: str = "http://case-service:8001"
    CONVERSATION_SERVICE_URL: str = "http://conversation-service:8002"
    SCHEDULER_SERVICE_URL: str = "http://scheduler-service:8003"

    @property
    def cors_origins(self) -> list:
        """解析 CORS 允许的来源列表"""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
