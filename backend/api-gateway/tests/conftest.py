"""
API Gateway 测试 conftest — mock Redis，API Gateway 测试文件自行管理命名空间隔离
"""

import os
import sys
from unittest.mock import AsyncMock

import pytest

_svc_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_expect = os.path.normpath(os.path.join(_svc_root, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _key in list(sys.modules):
        if _key == "app" or _key.startswith("app."):
            del sys.modules[_key]
    # 仅在路径不存在时添加，不删除其他测试目录的路径，避免污染 sys.path
    if _svc_root not in sys.path:
        sys.path.insert(0, _svc_root)


@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    """自动 mock Redis 连接，避免测试依赖真实 Redis"""

    async def fake_connect(self):
        self.client = AsyncMock()
        # 为 TerminalService 后台清理任务设置默认返回值
        self.client.smembers = AsyncMock(return_value=set())
        self.client.sadd = AsyncMock(return_value=1)
        self.client.srem = AsyncMock(return_value=1)

    async def fake_close(self):
        return None

    monkeypatch.setattr("shared.database.redis.RedisManager.connect", fake_connect)
    monkeypatch.setattr("shared.database.redis.RedisManager.close", fake_close)
