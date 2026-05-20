"""
Eval Service 测试配置
"""

import os
import sys

import pytest

# 添加服务根目录到路径
_svc_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _svc_root not in sys.path:
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
