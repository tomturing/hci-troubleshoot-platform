"""离线诊断服务配置。"""

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """服务配置。"""

    SERVICE_NAME: str = "diagnosis-service"
    SERVICE_PORT: int = 8008
    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str = "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot"
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"
    IDENTITY_MODE: str = "internal"
    OIDC_PUBLIC_KEY_PEM_B64: str = ""
    OIDC_JWKS_URL: str = ""
    OIDC_ISSUER: str = ""
    OIDC_AUDIENCE: str = ""
    OIDC_CLOCK_SKEW_SECONDS: int = 60
    COLLECTOR_SIGNING_PRIVATE_KEY_B64: str = ""
    COLLECTOR_SIGNING_KEY_ID: str = ""
    DIAGNOSIS_OBJECT_STORAGE_ROOT: str = "/var/lib/hci-diagnosis"
    # 默认返回同源相对地址，由 UI Nginx/Ingress 将数据面请求直接转发到诊断服务。
    DIAGNOSIS_DIRECT_UPLOAD_BASE_URL: str = "/"
    DIAGNOSIS_UPLOAD_TTL_SECONDS: int = 86400
    DIAGNOSIS_BUNDLE_RETENTION_DAYS: int = 60
    # 与离线运行时容量契约保持一致，避免签发客户端无法完成的采集计划。
    DIAGNOSIS_MAX_BUNDLE_BYTES: int = 512 * 1024 * 1024
    DIAGNOSIS_MAX_EXTRACTED_BYTES: int = 1024 * 1024 * 1024
    DIAGNOSIS_MAX_FILE_BYTES: int = 512 * 1024 * 1024
    DIAGNOSIS_MAX_FILE_COUNT: int = 20000
    DIAGNOSIS_WORKER_TIMEOUT_SECONDS: int = 900
    DIAGNOSIS_WORKER_POLL_SECONDS: float = 2.0
    DIAGNOSIS_WORKER_STALE_SECONDS: int = 1800
    DIAGNOSIS_WORKER_MAINTENANCE_SECONDS: int = 300
    DIAGNOSIS_WORKER_METRICS_PORT: int = 9108
    DIAGNOSIS_ENCRYPTION_PRIVATE_KEY_B64: str = ""
    DIAGNOSIS_ENCRYPTION_KEY_ID: str = ""
    DIAGNOSIS_OBJECT_STORAGE_MODE: str = "local"
    DIAGNOSIS_ALLOWED_ORIGINS: str = "http://localhost:3001,http://localhost:3002"
    # 精简 P0 默认由工程师人工处理证据不足；P1 补采链路保留但需显式启用。
    DIAGNOSIS_ENABLE_AUTOMATIC_SUPPLEMENT: bool = False

    @property
    def diagnosis_cors_origins(self) -> list[str]:
        """解析诊断分片直传允许的浏览器来源。"""

        origins = [origin.strip() for origin in self.DIAGNOSIS_ALLOWED_ORIGINS.split(",") if origin.strip()]
        if not origins or "*" in origins:
            raise ValueError("DIAGNOSIS_ALLOWED_ORIGINS 必须是非空显式来源列表，禁止使用通配符")
        return origins

    model_config = ConfigDict(env_file=".env", case_sensitive=True, extra="ignore")


settings = Settings()
