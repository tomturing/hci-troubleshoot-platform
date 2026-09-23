"""auth-service 配置（环境变量单例）。

所有密钥类配置均应从 K8s Secret / 运维注入，**禁止源码默认值泄露真实密钥**。

兼容性说明：生产环境使用 pydantic_settings；无该依赖的精简环境（如受限 CI）
自动降级为 os.getenv 实现，行为一致。
"""

from __future__ import annotations

import os

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class Settings(BaseSettings):
        """运行时配置（pydantic-settings）。"""

        model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

        SERVICE_NAME: str = "auth-service"
        SERVICE_PORT: int = 8007
        LOG_LEVEL: str = "INFO"

        DATABASE_URL: str = os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/hci",
        )

        # RS256 私钥（PEM）。生产由 K8s Secret 注入；缺失时生成临时内存密钥（仅 dev/test）。
        AUTH_RSA_PRIVATE_KEY_PEM: str = ""
        JWT_ISSUER: str = "hci-auth-service"
        JWT_AUD_CUSTOMER: str = "hci-customer"
        JWT_AUD_ADMIN: str = "hci-admin"
        ACCESS_TOKEN_TTL_CUSTOMER: int = 60 * 60 * 24 * 7  # 7 天
        ACCESS_TOKEN_TTL_ADMIN: int = 60 * 60 * 4  # 4 小时（短时效，配合 token_version）

        ADMIN_ROLES: frozenset[str] = frozenset({"platform_admin", "support_engineer", "diagnosis_worker"})
        CUSTOMER_ROLES: frozenset[str] = frozenset({"customer"})

        REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

except ImportError:  # pragma: no cover - 精简环境降级路径

    class Settings:
        """os.getenv 降级实现（无 pydantic-settings 时）。"""

        SERVICE_NAME = os.getenv("SERVICE_NAME", "auth-service")
        SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8007"))
        LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
        DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/hci")
        AUTH_RSA_PRIVATE_KEY_PEM = os.getenv("AUTH_RSA_PRIVATE_KEY_PEM", "")
        JWT_ISSUER = os.getenv("JWT_ISSUER", "hci-auth-service")
        JWT_AUD_CUSTOMER = os.getenv("JWT_AUD_CUSTOMER", "hci-customer")
        JWT_AUD_ADMIN = os.getenv("JWT_AUD_ADMIN", "hci-admin")
        ACCESS_TOKEN_TTL_CUSTOMER = int(os.getenv("ACCESS_TOKEN_TTL_CUSTOMER", str(60 * 60 * 24 * 7)))
        ACCESS_TOKEN_TTL_ADMIN = int(os.getenv("ACCESS_TOKEN_TTL_ADMIN", str(60 * 60 * 4)))
        ADMIN_ROLES = frozenset({"platform_admin", "support_engineer", "diagnosis_worker"})
        CUSTOMER_ROLES = frozenset({"customer"})
        REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


settings = Settings()
