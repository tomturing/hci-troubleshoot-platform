"""WeKnora 知识库入口路由测试。

覆盖 /api/weknora/launch 的硬性边界：

1. 鉴权：匿名与普通用户一律 403；知识库是全平台共享资产，问答会消耗模型配额。
2. 未建渠道是**正常态**：embed_url 返回 null，管理台入口照常下发，
   不能报错——否则首次部署就被判成故障。
3. 已建渠道时，浏览器只拿短效 ems_ 令牌；长期 em_ publish token 绝不出现在响应
   或日志里。
4. 上游 exchange 拒绝/不可达统一 503，且不把内部异常文本透给客户端。
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
LAUNCH_URL = "/api/weknora/launch"
PUBLISH_TOKEN = "em_should_never_leak"


def _anon_client() -> TestClient:
    return TestClient(app)


def _user_client() -> TestClient:
    _, cookie_value = issue_identity(settings.INTERNAL_API_TOKEN)
    client = TestClient(app)
    client.cookies.update({IDENTITY_COOKIE_NAME: cookie_value})
    return client


def _admin_client() -> TestClient:
    return TestClient(app, headers=ADMIN_HEADERS)


def _patch_exchange(*, status_code: int = 200, payload=None, side_effect=None):
    upstream = MagicMock()
    upstream.status_code = status_code
    upstream.json.return_value = payload
    client = AsyncMock()
    if side_effect is not None:
        client.post.side_effect = side_effect
    else:
        client.post.return_value = upstream
    return patch(
        "app.routes.weknora.httpx.AsyncClient",
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=client),
            __aexit__=AsyncMock(return_value=None),
        ),
    )


def _configured():
    """模拟环境仓库注入了渠道参数。"""
    return (
        patch.object(settings, "WEKNORA_EMBED_CHANNEL_ID", "ch-abc"),
        patch.object(settings, "WEKNORA_EMBED_PUBLISH_TOKEN", PUBLISH_TOKEN),
        patch.object(settings, "WEKNORA_EMBED_ORIGIN", "https://acli.sangfor.com.cn:4443"),
    )


# ============ 鉴权 ============


def test_anonymous_is_rejected():
    response = _anon_client().get(LAUNCH_URL)
    assert response.status_code == 403


def test_plain_user_is_rejected():
    response = _user_client().get(LAUNCH_URL)
    assert response.status_code == 403


# ============ 未建渠道 ============


def test_missing_channel_returns_console_only():
    """未创建 embed 渠道属于正常中间态：管理台可用，问答为 null。"""
    with (
        patch.object(settings, "WEKNORA_EMBED_CHANNEL_ID", ""),
        patch.object(settings, "WEKNORA_EMBED_PUBLISH_TOKEN", ""),
    ):
        response = _admin_client().get(LAUNCH_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["console_url"] == settings.WEKNORA_PUBLIC_PATH
    assert body["embed_url"] is None


# ============ 已建渠道 ============


def test_admin_gets_short_lived_embed_url():
    session_patch, token_patch, origin_patch = _configured()
    with (
        session_patch,
        token_patch,
        origin_patch,
        _patch_exchange(payload={"success": True, "data": {"session_token": "ems_short"}}),
    ):
        response = _admin_client().get(LAUNCH_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["console_url"] == settings.WEKNORA_PUBLIC_PATH
    assert body["embed_url"] == f"{settings.WEKNORA_PUBLIC_PATH}embed/ch-abc?token=ems_short"
    # 长期令牌绝不能出现在响应里
    assert PUBLISH_TOKEN not in response.text


def test_exchange_forwards_origin_and_uses_publish_token():
    """exchange 必须带 Origin（渠道白名单校验）与 Embed 授权头。"""
    captured = {}

    async def fake_post(self, url, *args, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        upstream = MagicMock()
        upstream.status_code = 200
        upstream.json.return_value = {"data": {"session_token": "ems_short"}}
        return upstream

    session_patch, token_patch, origin_patch = _configured()
    with session_patch, token_patch, origin_patch, patch.object(httpx.AsyncClient, "post", fake_post):
        _admin_client().get(LAUNCH_URL)

    assert captured["url"].endswith("/api/v1/embed/ch-abc/exchange")
    assert captured["headers"]["Authorization"] == f"Embed {PUBLISH_TOKEN}"
    assert captured["headers"]["Origin"] == "https://acli.sangfor.com.cn:4443"


def test_exchange_rejected_returns_503():
    session_patch, token_patch, origin_patch = _configured()
    with session_patch, token_patch, origin_patch, _patch_exchange(status_code=403, payload={}):
        response = _admin_client().get(LAUNCH_URL)
    assert response.status_code == 503
    assert PUBLISH_TOKEN not in response.text


def test_exchange_unreachable_returns_503():
    session_patch, token_patch, origin_patch = _configured()
    with (
        session_patch,
        token_patch,
        origin_patch,
        _patch_exchange(side_effect=httpx.ConnectError("boom")),
    ):
        response = _admin_client().get(LAUNCH_URL)
    assert response.status_code == 503
    assert "boom" not in response.text


def test_exchange_malformed_payload_returns_503():
    session_patch, token_patch, origin_patch = _configured()
    with session_patch, token_patch, origin_patch, _patch_exchange(payload={"data": {}}):
        response = _admin_client().get(LAUNCH_URL)
    assert response.status_code == 503
