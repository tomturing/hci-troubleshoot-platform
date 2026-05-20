"""
Eval Service 测试配置
"""

import os
import sys

import pytest

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_expect = os.path.normpath(os.path.join(_svc_root, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc_root in sys.path:
        sys.path.remove(_svc_root)
    sys.path.insert(0, _svc_root)


@pytest.fixture
def mock_settings(mocker):
    """Mock 设置"""
    from app.config import Settings

    mock_settings = Settings(
        SERVICE_NAME="eval-service-test",
        INTERNAL_API_TOKEN="test-admin-token",
    )
    mocker.patch("app.config.settings", mock_settings)
    return mock_settings
