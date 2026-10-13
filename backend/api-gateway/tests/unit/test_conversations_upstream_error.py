"""网关 SSE 代理对上游非 200 响应的错误分类回归测试。

历史缺陷：proxy_request_stream 把所有非 200 一律包装为「上游服务暂时不可用」，
使 403「无权访问此会话」这类真实原因被掩盖（工单 Q2026101330905 排障被误导）。
现按状态分类透传真因。
"""

import os
import sys

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _key in list(sys.modules):
        if _key == "app" or _key.startswith("app."):
            del sys.modules[_key]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

from app.routes.conversations import _upstream_error_payload  # noqa: E402


def test_403_maps_to_session_invalid_with_actionable_message():
    code, message, detail = _upstream_error_payload(403, '{"detail":"无权访问此会话"}')
    assert code == "SESSION_INVALID"
    # 绝不能再伪装成「上游服务暂时不可用」
    assert message != "上游服务暂时不可用"
    assert "刷新" in message
    # 透传上游真实 detail
    assert detail == "无权访问此会话"


def test_401_maps_to_session_invalid():
    code, _, _ = _upstream_error_payload(401, "")
    assert code == "SESSION_INVALID"


def test_other_4xx_maps_to_validation_error():
    code, message, _ = _upstream_error_payload(422, '{"detail":"字段校验失败"}')
    assert code == "VALIDATION_ERROR"
    assert message == "请求被上游服务拒绝"


def test_5xx_still_reports_upstream_unavailable():
    code, message, detail = _upstream_error_payload(503, "")
    assert code == "GATEWAY_ERROR"
    assert message == "上游服务暂时不可用"
    assert detail == "status 503"


def test_non_json_body_falls_back_to_snippet_detail():
    code, _, detail = _upstream_error_payload(403, "plain text not json")
    assert code == "SESSION_INVALID"
    assert "plain text not json" in detail
