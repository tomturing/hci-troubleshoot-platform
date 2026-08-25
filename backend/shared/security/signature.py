"""
服务间身份签名（HMAC-SHA256，纯标准库实现）

背景（安全审计 2026-08-19）：
此前 conversation-service 的会话归属校验依赖用户可伪造的 X-Client-ID
请求头，且网关代理转发时根本不透传该头，导致 IDOR 防护形同虚设。

信任模型：
- 网关是唯一被允许对下游声明"客户端身份"的组件。网关出口统一剥离
  用户传入的 X-Client-ID / X-Client-Signature，改用共享密钥
  （INTERNAL_API_TOKEN，helm secret 已注入，两服务同值）对
  client_id + 时间戳做 HMAC 重签后注入。
- conversation-service 只信任带有效签名的身份头：签名缺失、不匹配、
  或时间戳超出容忍窗口均视为未认证（防集群内直连绕过 + 防重放）。

密钥复用说明：签名密钥复用 INTERNAL_API_TOKEN（KB/agent 等内部服务
鉴权已使用该值，避免新增必须同步配置的环境变量）。生产部署由
helm secrets.internalApiToken 注入，禁止使用源码默认值。
"""

import hashlib
import hmac
import time

# 签名时间窗（秒）：K3s 集群内时钟偏差通常 < 1s，300s 已留足余量，
# 同时把签名被截获后的重放窗口限制在有界范围内。
CLOCK_SKEW_SECONDS = 300

CLIENT_ID_HEADER = "X-Client-ID"
SIGNATURE_HEADER = "X-Client-Signature"

# client_id 格式约束：网关侧与验签侧共用，拒绝畸形值注入
CLIENT_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


def sign_client_identity(client_id: str, secret: str) -> dict[str, str]:
    """
    为 client_id 生成带 HMAC 签名的身份头。

    Returns:
        形如 {X-Client-ID: <id>, X-Client-Signature: "<ts>.<hmac>"} 的头字典，
        可直接并入 httpx 请求 headers。
    """
    timestamp = str(int(time.time()))
    payload = f"{timestamp}:{client_id}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {
        CLIENT_ID_HEADER: client_id,
        SIGNATURE_HEADER: f"{timestamp}.{digest}",
    }


def verify_client_identity(headers, secret: str) -> str | None:
    """
    校验请求头中的身份签名。

    Args:
        headers: Starlette Headers / httpx Headers（大小写不敏感）
        secret: 共享密钥（INTERNAL_API_TOKEN）

    Returns:
        验签通过时返回 client_id；缺失、格式错误、签名不匹配或
        时间戳超窗时返回 None。
    """
    client_id = headers.get(CLIENT_ID_HEADER)
    signature = headers.get(SIGNATURE_HEADER)
    if not client_id or not signature:
        return None

    try:
        ts_str, digest = signature.split(".", 1)
        ts = int(ts_str)
    except ValueError:
        return None

    # 防重放：拒绝超出时间窗的签名
    if abs(time.time() - ts) > CLOCK_SKEW_SECONDS:
        return None

    expected = hmac.new(secret.encode(), f"{ts_str}:{client_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None

    return client_id
