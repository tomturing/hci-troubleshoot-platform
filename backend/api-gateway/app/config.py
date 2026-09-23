"""
API Gateway Configuration
"""

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """配置类"""

    # 服务配置
    SERVICE_NAME: str = "api-gateway"
    SERVICE_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Redis配置
    REDIS_URL: str = "redis://redis:6379/0"

    # PostgreSQL数据库配置
    DATABASE_URL: str = "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot"

    # CORS 允许的来源（逗号分隔，支持环境变量覆盖）
    ALLOWED_ORIGINS: str = "http://localhost:3001,http://localhost:3002"

    # 下游服务地址
    CASE_SERVICE_URL: str = "http://case-service:8001"
    CONVERSATION_SERVICE_URL: str = "http://conversation-service:8002"
    SCHEDULER_SERVICE_URL: str = "http://scheduler-service:8003"
    KB_SERVICE_URL: str = "http://kb-service:8004"
    AGENT_SERVICE_URL: str = "http://agent-service:8005"
    HCI_SIM_URL: str = "http://hci-sim.hci-sim-dev.svc:8080"
    HCI_SIM_CONTROL_TOKEN: str = ""
    HCI_SIM_COMPILER_ACTOR_ID: str = "bundle-factory-compiler"
    HCI_SIM_EXPERT_EDITOR_ACTOR_ID: str = "bundle-factory-expert-editor"
    HCI_SIM_EXPERT_REVIEWER_ACTOR_ID: str = "bundle-factory-expert-reviewer"
    HCI_SIM_SECURITY_ACTOR_ID: str = "bundle-factory-security"
    HCI_SIM_PUBLISHER_ACTOR_ID: str = "bundle-factory-publisher"
    DIAGNOSIS_SERVICE_URL: str = "http://diagnosis-service:8008"
    DIAGNOSIS_IDENTITY_MODE: str = "internal"
    # internal 模式下网关代表浏览器调用方重签内部身份时使用的租户标识。
    # 必须与 chart `diagnosisService.internalIdentity.tenantId` 保持一致，
    # 否则浏览器发起的诊断请求会被下游判为租户上下文非法（422）。
    DIAGNOSIS_INTERNAL_TENANT_ID: str = "hci-platform"

    # 内部服务间 API 鉴权 Token；同时用作出口身份签名的 HMAC 密钥
    # （见 shared/security/signature.py），必须与 conversation-service 同值。
    # 生产部署由 helm secrets.internalApiToken 注入，源码默认值仅用于本地开发。
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"

    # 服务端签发身份 Cookie（P0 修复：防客户端自报 client_id / 越权）
    # 密钥复用 INTERNAL_API_TOKEN，避免新增必须同步的配置项。
    IDENTITY_COOKIE_NAME: str = "hci_client_id"
    IDENTITY_COOKIE_SECURE: bool = False  # 生产 HTTPS 环境应设为 True

    # === 统一认证（阶段1：网关验签 auth-service 签发的 RS256 JWT）===
    # auth-service 的 JWKS 公钥端点；网关缓存公钥验签，auth-service 短暂不可用不影响已登录用户
    AUTH_JWKS_URL: str = "http://auth-service:8007/.well-known/jwks.json"
    # auth-service 控制面地址；网关转发 /api/auth/*（登录等）到此服务
    AUTH_SERVICE_URL: str = "http://auth-service:8007"
    AUTH_JWT_ISSUER: str = "hci-auth-service"
    AUTH_JWT_AUD_ADMIN: str = "hci-admin"
    # admin 路径是否强制 JWT 登录（关闭=兼容 INTERNAL_API_TOKEN 现状；开启=共享令牌不再赋予 admin）
    AUTHN_ENFORCE_ADMIN: bool = False
    # admin JWT 验签成功、下游未自报租户时，网关重签身份使用的租户标识（与下游信任模型一致）
    AUTH_DEFAULT_TENANT_ID: str = "hci-platform"
    # JWT 验签时钟偏移容忍（秒）
    AUTH_JWT_CLOCK_SKEW_SECONDS: int = 60

    # === 终端 SSH 配置 ===
    TERMINAL_ALLOW_INSECURE_HOSTS: bool = False
    TERMINAL_KNOWN_HOSTS_FILE: str = "~/.ssh/known_hosts"
    TERMINAL_CLEANUP_INTERVAL_SECONDS: int = 60

    @property
    def cors_origins(self) -> list:
        """解析 CORS 允许的来源列表"""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    model_config = ConfigDict(env_file=".env", case_sensitive=True, extra="ignore")


settings = Settings()
