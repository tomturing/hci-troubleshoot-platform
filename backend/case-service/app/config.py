"""
Case Service Configuration
"""

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """配置类"""

    # 服务配置
    SERVICE_NAME: str = "case-service"
    SERVICE_PORT: int = 8001
    LOG_LEVEL: str = "INFO"

    # 数据库配置
    DATABASE_URL: str = "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot"

    # Redis配置 (可选，用于缓存)
    REDIS_URL: str = "redis://redis:6379/0"

    # KB Service 配置（Case 关闭时推送摘要，异步 fire-and-forget）
    KB_SERVICE_URL: str = "http://kb-service:8004"
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"
    KB_PUSH_ENABLED: bool = True  # 环境变量 KB_PUSH_ENABLED=false 可禁用

    # 日志收集制品存储（P3 段②：客户 PC → 平台 分块上传）
    # 单任务 2–20 GiB，使用本地共享卷；平台不做自动清理，容量告警由前端展示。
    LOG_COLLECT_STORAGE_ROOT: str = "/var/lib/log-collect"
    LOG_COLLECT_CHUNK_SIZE_BYTES: int = 32 * 1024 * 1024
    LOG_COLLECT_MAX_ARCHIVE_BYTES: int = 25 * 1024 * 1024 * 1024  # 25 GiB
    LOG_COLLECT_UPLOAD_TOKEN_TTL_HOURS: int = 24
    # 数据面 token 签名密钥；留空时由 INTERNAL_API_TOKEN 派生，避免新增必填配置。
    LOG_COLLECT_UPLOAD_SECRET: str = ""

    model_config = ConfigDict(env_file=".env", case_sensitive=True, extra="ignore")


settings = Settings()
