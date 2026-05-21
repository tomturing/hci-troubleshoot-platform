"""
API Gateway 测试 conftest — 激活 api-gateway 的 app 命名空间
"""

import os
import sys
from unittest.mock import AsyncMock

import pytest

_svc_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_expect = os.path.normpath(os.path.join(_svc_root, "app"))
_actual = os.path.normpath(
    getattr(sys.modules.get("app"), "__path__", [""])[0]
) if "app" in sys.modules else ""
if _expect != _actual:
    for _key in list(sys.modules):
        if _key == "app" or _key.startswith("app."):
            del sys.modules[_key]
    if _svc_root in sys.path:
        sys.path.remove(_svc_root)
    sys.path.insert(0, _svc_root)


@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    """自动 mock Redis 连接，避免测试依赖真实 Redis"""

    async def fake_connect(self):
        self.client = AsyncMock()

    async def fake_close(self):
        return None

    monkeypatch.setattr("shared.database.redis.RedisManager.connect", fake_connect)
    monkeypatch.setattr("shared.database.redis.RedisManager.close", fake_close)
