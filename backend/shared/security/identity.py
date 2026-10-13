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

身份生命周期设计（修复工单 Q2026101330905「上游服务暂时不可用」误报）：
- 身份 Cookie 是"长寿命会话身份"，不是"每请求一次性 nonce"。其防伪完全依赖
  HMAC 签名；时间戳仅用于把"Cookie 被窃取后的重放窗口"限制在有界范围内。
- 早期实现复用出口签名的 300s 硬窗口，且签发后从不刷新时间戳，导致用户读长
  回复/跑命令间隔超过 5 分钟后，下一次请求被判超窗 → 网关静默铸造全新
  client_id → 工单仍归属旧 id → 下游归属校验 403「无权访问此会话」。这是回归
  的根因。故本模块把"过期"语义改为对齐浏览器 Cookie 寿命的 IDENTITY_MAX_AGE
  _SECONDS（默认 30 天），并由网关中间件对每次成功验签做滑动续期（见
  refresh_identity），使活跃/短暂离线的会话身份绝不轮换。
- 注意：本模块的窗口与服务间出口签名 signature.py 的 CLOCK_SKEW_SECONDS（每请
  求重签、防重放）相互独立，放宽此处不影响下游对 X-Client-Signature 的防重放。
"""

import hashlib
import hmac
import time
import uuid

IDENTITY_COOKIE_NAME = "hci_client_id"

# 身份 Cookie 最大寿命（秒）：对齐"记住我"式会话语义，默认 30 天。配合网关滑动
# 续期，只要用户在此窗口内有任意活动即不会轮换身份。
IDENTITY_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

# 允许的未来时间戳偏差（秒）：抵御客户端/服务端轻微时钟偏差；时间戳明显来自
# 未来且超过该容差视为异常，拒绝。
CLOCK_SKEW_SECONDS = 300


def issue_identity(secret: str) -> tuple[str, str]:
    """生成随机 client_id 并返回 (client_id, cookie_value)。"""
    client_id = uuid.uuid4().hex
    return client_id, _sign(client_id, secret)


def refresh_identity(client_id: str, secret: str) -> str:
    """为既有 client_id 重新签名（滑动续期）：保持身份不变，仅刷新时间戳。

    网关中间件在每次验签成功后调用，令活跃会话的身份 Cookie 永不过期，从而
    彻底消除"离线数分钟后身份轮换 → 会话归属校验 403"的误杀。
    """
    return _sign(client_id, secret)


def _sign(client_id: str, secret: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{timestamp}:{client_id}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}.{client_id}.{digest}"


def verify_identity(cookie_value: str | None, secret: str) -> str | None:
    """校验 Cookie，返回 client_id；缺失/非法/超身份最大寿命返回 None。

    防伪由 HMAC 签名保证；时间戳仅用于限制重放窗口（IDENTITY_MAX_AGE_SECONDS），
    不再复用出口签名的 300s 硬窗口——那会使长会话身份被静默轮换。
    """
    if not cookie_value:
        return None
    try:
        ts_str, client_id, digest = cookie_value.split(".", 2)
        ts = int(ts_str)
    except ValueError:
        return None
    age = time.time() - ts
    # 时间戳明显来自未来且偏差过大 → 异常，拒绝
    if age < -CLOCK_SKEW_SECONDS:
        return None
    # 超过身份最大寿命 → 视为过期（浏览器通常也已丢弃该 Cookie）
    if age > IDENTITY_MAX_AGE_SECONDS:
        return None
    expected = hmac.new(secret.encode(), f"{ts_str}:{client_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    return client_id
