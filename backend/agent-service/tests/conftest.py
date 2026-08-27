"""
Agent Service 测试配置
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
        SERVICE_NAME="agent-service-test",
        OPS_AGENT_ENABLED=False,
        PYDANTIC_AI_ENABLED=False,
        KB_ENABLED=False,
        REACT_ENABLED=False,
    )
    mocker.patch("app.config.settings", mock_settings)
    return mock_settings


@pytest.fixture
def sample_messages():
    """示例消息列表"""
    return [{"role": "user", "content": "我的虚拟机无法启动"}]


@pytest.fixture(autouse=True)
def ai_processing_prompt_for_unit_tests(monkeypatch):
    """为不启动数据库的单元测试提供受控 Prompt 管理桩。"""

    async def load_prompt(_factory, *, mode, output_type, **kwargs):
        return (
            "你是 HCI 排障平台的受控 AI 数据后处理器。"
            f"当前处理模式：{mode}；输出类型：{output_type}。"
            '只能返回 JSON：{"status":"success","output":结果,"evidence":[],"reason":"理由"}',
            None,
        )

    monkeypatch.setattr("shared.signals.ai_extractor._load_ai_processing_system_prompt", load_prompt)
