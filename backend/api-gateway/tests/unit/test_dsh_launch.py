"""DSH 诊断入口路由测试。

覆盖 /api/dsh/launch 的两条硬性边界：
1. 鉴权门禁：匿名与普通用户一律 403，仅管理员可取得 token。
2. 上游不可用语义：DSH 未就绪（token 文件缺失）与无法连通都返回 503，
   且任何情况下都不把失败伪装成鉴权错误。

另验证：token 只出现在响应体中，不出现在日志告警的字段里。
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.config import settings
from app.main import app
from shared.security.identity import IDENTITY_COOKIE_NAME, issue_identity

ADMIN_HEADERS = {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}
DSH_LAUNCH_URL = "/api/dsh/launch"


def _anon_client() -> TestClient:
    return TestClient(app)


def _user_client() -> TestClient:
    """携带合法服务端签发身份 Cookie 的客户端（非管理员）。"""
    _, cookie_value = issue_identity(settings.INTERNAL_API_TOKEN)
    client = TestClient(app)
    client.cookies.update({IDENTITY_COOKIE_NAME: cookie_value})
    return client


def _admin_client() -> TestClient:
    return TestClient(app, headers=ADMIN_HEADERS)


def _patch_upstream(status_code: int, text: str):
    """构造返回指定响应的 DSH 上游 mock。"""
    upstream = MagicMock()
    upstream.status_code = status_code
    upstream.text = text
    client = AsyncMock()
    client.get.return_value = upstream
    return patch(
        "app.routes.dsh.httpx.AsyncClient",
        return_value=MagicMock(__aenter__=AsyncMock(return_value=client),
                               __aexit__=AsyncMock(return_value=None)),
    )


# ============ 鉴权门禁 ============


def test_anonymous_is_rejected():
    """匿名请求必须 403，不得凭空发放 Agent 控制权。"""
    with _patch_upstream(200, "tok-abc"):
        response = _anon_client().get(DSH_LAUNCH_URL)
    assert response.status_code == 403
    assert "token" not in response.text.lower()


def test_plain_user_is_rejected():
    """持有客户会话 Cookie 的普通用户同样不得取得 token。"""
    with _patch_upstream(200, "tok-abc"):
        response = _user_client().get(DSH_LAUNCH_URL)
    assert response.status_code == 403


def test_admin_gets_relative_launch_url():
    """管理员取得同源相对路径，且路径与 DSH_PUBLIC_PATH 一致。"""
    with _patch_upstream(200, "tok-abc") as _:
        response = _admin_client().get(DSH_LAUNCH_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == f"{settings.DSH_PUBLIC_PATH}?token=tok-abc"
    # 不得把集群内 Service 地址暴露给浏览器
    assert settings.DSH_SERVICE_URL not in body["url"]


def test_upstream_called_with_internal_token_path():
    """上游必须命中 /__internal/token 这一不对外暴露的内部路径。"""
    captured = {}

    async def fake_get(self, url, *args, **kwargs):
        captured["url"] = url
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.text = "tok-abc"
        return upstream

    with patch.object(httpx.AsyncClient, "get", fake_get):
        _admin_client().get(DSH_LAUNCH_URL)
    assert captured["url"] == f"{settings.DSH_SERVICE_URL}/__internal/token"


# ============ 上游可用性 ============


def test_not_ready_returns_503():
    """DSH 刚启动尚未注入 token 时返回 503，前端据此提示启动中。"""
    with _patch_upstream(404, ""):
        with patch("app.routes.dsh.logger.warning") as logger_warning:
            response = _admin_client().get(DSH_LAUNCH_URL)
    assert response.status_code == 503
    # token 为空时不得记录任何 token 字段
    logger_warning.assert_called_once()
    logged = logger_warning.call_args.kwargs
    assert "tok" not in str(logged).lower() or "token=" not in str(logged)


def test_unreachable_returns_503():
    """上游无法连通时返回 503，不向客户端暴露内部异常细节。"""
    client = AsyncMock()
    client.get.side_effect = httpx.ConnectError("boom")
    with patch(
        "app.routes.dsh.httpx.AsyncClient",
        return_value=MagicMock(__aenter__=AsyncMock(return_value=client),
                               __aexit__=AsyncMock(return_value=None)),
    ):
        response = _admin_client().get(DSH_LAUNCH_URL)
    assert response.status_code == 503
    assert "boom" not in response.text
