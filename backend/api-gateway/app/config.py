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
    
    # 数据库配置
    REDIS_URL: str = "redis://redis:6379/0"
    
    # 下游服务地址
    CASE_SERVICE_URL: str = "http://case-service:8001"
    CONVERSATION_SERVICE_URL: str = "http://conversation-service:8002"
    SCHEDULER_SERVICE_URL: str = "http://scheduler-service:8003"
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
