"""
Case Service Configuration
"""

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """配置类"""
    
    # 服务配置
    SERVICE_NAME: str = "case-service"
    SERVICE_PORT: int = 8001
    LOG_LEVEL: str = "INFO"
    
    # 数据库配置
    DATABASE_URL: str = "postgresql+asyncpg://hci_user:hci_password@postgres:5432/hci_troubleshoot"
    
    # Redis配置 (可选，用于缓存)
    REDIS_URL: str = "redis://redis:6379/0"
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
