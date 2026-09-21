"""
服务端签发的身份 Cookie（P0 修复：防客户端自报 client_id / 越权访问）

信任模型：
- 网关为每位访客签发一个不可猜测、服务端签名的 client_id，通过 HttpOnly
  Cookie 下发。后续请求一律以 Cookie 中的 client_id 为准，彻底忽略请求头/
  查询参数中的自报 X-Client-ID / client_id。
- 密钥复用 INTERNAL_API_TOKEN（与出口身份签名一致），避免新增必须同步的
  配置项。生产部署由 helm secrets.internalApiToken 注入，禁用源码默认值。

说明：采用"网关自动签发"而非强制登录，是为了在不改变产品形态（无登录的
自服务排障）的前提下关闭越权。代价是历史匿名工单（绑定旧的浏览器自生成
client_id）在新身份下不可达——这是无登录模型修复越权的必然取舍。
"""

import hashlib
import hmac
import time
import uuid

IDENTITY_COOKIE_NAME = "hci_client_id"

# 签名时间窗（秒）：与出口签名保持一致，限制重放窗口。
CLOCK_SKEW_SECONDS = 300


def issue_identity(secret: str) -> tuple[str, str]:
    """生成随机 client_id 并返回 (client_id, cookie_value)。"""
    client_id = uuid.uuid4().hex
    return client_id, _sign(client_id, secret)


def _sign(client_id: str, secret: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{timestamp}:{client_id}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}.{client_id}.{digest}"


def verify_identity(cookie_value: str | None, secret: str) -> str | None:
    """校验 Cookie，返回 client_id；缺失/非法/超窗返回 None。"""
    if not cookie_value:
        return None
    try:
        ts_str, client_id, digest = cookie_value.split(".", 2)
        ts = int(ts_str)
    except ValueError:
        return None
    if abs(time.time() - ts) > CLOCK_SKEW_SECONDS:
        return None
    expected = hmac.new(secret.encode(), f"{ts_str}:{client_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    return client_id
