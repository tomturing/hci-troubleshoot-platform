"""KBD Pipeline 配置契约测试。"""

from kbd.config import KbdSettings


def test_asyncpg_database_url_accepts_sqlalchemy_asyncpg_scheme():
    settings = KbdSettings(DATABASE_URL="postgresql+asyncpg://user:pass@postgres:5432/kbd")

    assert settings.asyncpg_database_url == "postgresql://user:pass@postgres:5432/kbd"


def test_asyncpg_database_url_accepts_legacy_postgres_scheme():
    settings = KbdSettings(DATABASE_URL="postgres://user:pass@postgres:5432/kbd")

    assert settings.asyncpg_database_url == "postgresql://user:pass@postgres:5432/kbd"


def test_asyncpg_database_url_preserves_native_scheme():
    settings = KbdSettings(DATABASE_URL="postgresql://user:pass@postgres:5432/kbd")

    assert settings.asyncpg_database_url == "postgresql://user:pass@postgres:5432/kbd"
