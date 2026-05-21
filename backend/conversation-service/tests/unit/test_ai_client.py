"""AI Client 单元测试。"""

import os
import sys

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

from shared.clients.ai_client import OpenClawAssistant


class TestOpenClawAssistantAuth:
    """验证内部 gateway 与外部模型提供商使用不同认证。"""

    def test_internal_gateway_prefers_api_key(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "provider-key")

        client = OpenClawAssistant(
            base_url="https://coding.dashscope.aliyuncs.com/v1",
            api_key="api-token",
            assistant_type="htp-agent",
        )

        assert client._resolve_auth_token("http://10.42.0.123:18789") == "api-token"

    def test_external_provider_prefers_provider_api_key(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "provider-key")

        client = OpenClawAssistant(
            base_url="https://coding.dashscope.aliyuncs.com/v1",
            api_key="api-token",
            assistant_type="htp-agent",
        )

        assert client._resolve_auth_token("https://coding.dashscope.aliyuncs.com/v1") == "provider-key"

    def test_cluster_service_dns_is_treated_as_internal_gateway(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "provider-key")

        client = OpenClawAssistant(
            base_url="http://htp-agent:18789",
            api_key="api-token",
            assistant_type="htp-agent",
        )

        assert client._resolve_auth_token("http://htp-agent.hci-troubleshoot.svc.cluster.local:18789") == "api-token"
