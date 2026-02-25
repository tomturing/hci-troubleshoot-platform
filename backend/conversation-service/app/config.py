"""
Conversation Service Configuration
"""

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """配置类"""
    
    SERVICE_NAME: str = "conversation-service"
    SERVICE_PORT: int = 8002
    LOG_LEVEL: str = "INFO"
    
    DATABASE_URL: str = "postgresql+asyncpg://hci_user:hci_password@postgres:5432/hci_troubleshoot"
    REDIS_URL: str = "redis://redis:6379/0"
    
    # OpenClaw配置
    OPENCLAW_BASE_URL: str = "http://openclaw:8080"
    OPENCLAW_GATEWAY_TOKEN: str = "default_token"
    
    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"

settings = Settings()
