"""
KB Service 测试配置

注意：KB Service 使用独立的 conftest，不依赖 backend/conftest.py，
以便独立运行测试，不被其他服务的全局状态干扰。
"""

import pytest
import pytest_asyncio


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
