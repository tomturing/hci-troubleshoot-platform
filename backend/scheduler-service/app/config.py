"""
Scheduler Service Configuration
"""

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """配置类"""
    
    SERVICE_NAME: str = "scheduler-service"
    SERVICE_PORT: int = 8003
    LOG_LEVEL: str = "INFO"
    
    # K8s配置
    K8S_NAMESPACE: str = "hci-troubleshoot"
    OPENCLAW_IMAGE: str = "openclaw:latest"
    
    # Pod池配置
    WARM_POOL_SIZE: int = 2
    MAX_POOL_SIZE: int = 10
    POD_IDLE_TIMEOUT: int = 300  # 5分钟
    
    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"

settings = Settings()
