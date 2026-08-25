"""
安全模块 - 服务间身份验签 + 会话/工单归属校验（IDOR 防护）

安全审计 2026-08-19 修复说明：
1. 身份信任链：只接受网关以共享密钥（INTERNAL_API_TOKEN）HMAC 签名
   注入的 X-Client-ID。用户直接传入的未签名身份头一律拒绝，防止
   集群内绕过网关直连本服务伪造身份。
2. 归属校验：查询 case 表的 client_id 与签名身份比对，不匹配即 403。
   （case 表的 ORM 模型属 case-service 包，跨服务无法导入，故沿用
   本服务 create_conversation 中的参数化 text() 查询先例。）
3. STRICT_IDENTITY_SIGNATURE=False 时为过渡模式：无签名请求放行但
   记录警告日志且跳过归属比对，供存量脚本/测试环境渐进迁移。
"""

import re
import uuid

from fastapi import HTTPException, Request
from shared.observability.logger import get_logger
from shared.security.signature import verify_client_identity
from sqlalchemy import text

from app.config import settings

logger = get_logger("security-auth")


async def authenticate_request(request: Request) -> str | None:
    """
    验证请求身份签名。

    Returns:
        验签通过的 client_id；过渡模式下未签名请求返回 None 并告警。

    Raises:
        HTTPException 401: 严格模式下签名缺失/无效。
    """
    client_id = verify_client_identity(request.headers, settings.INTERNAL_API_TOKEN)
    if client_id:
        return client_id

    if settings.STRICT_IDENTITY_SIGNATURE:
        logger.warning(
            event="auth_identity_signature_invalid",
            path=request.url.path,
        )
        raise HTTPException(status_code=401, detail="身份签名缺失或无效")

    logger.warning(
        event="auth_identity_signature_missing_lenient",
        path=request.url.path,
        message="过渡模式：未签名请求放行（生产环境请开启 STRICT_IDENTITY_SIGNATURE）",
    )
    return None


async def _get_case_client_id(service, case_id: str) -> str | None:
    """查询 case 归属的 client_id（参数化查询，跨服务无共享 ORM 模型）。"""
    res = await service.repository.session.execute(
        text('SELECT client_id FROM "case" WHERE case_id = :case_id'), {"case_id": case_id}
    )
    row = res.fetchone()
    return row[0] if row else None


async def verify_conversation_ownership(
    conversation_id: uuid.UUID,
    request: Request,
    service,
) -> None:
    """
    验证当前客户端是否有权访问指定会话。

    Raises:
        HTTPException 404: 会话不存在
        HTTPException 401: 身份签名缺失/无效（严格模式）
        HTTPException 403: 会话归属的 case 不属于当前客户端
    """
    client_id = await authenticate_request(request)
    if client_id is None:
        return  # 过渡模式：无签名时跳过归属比对

    conversation = await service.get_conversation(conversation_id)
    if not conversation:
        logger.warning(
            event="auth_conversation_not_found",
            conversation_id=str(conversation_id),
            client_id=client_id,
        )
        raise HTTPException(status_code=404, detail="会话不存在")

    case_owner = await _get_case_client_id(service, conversation.case_id)
    if case_owner is None:
        # 会话存在但关联 case 缺失属于数据不一致，按服务端错误处理而非放行
        logger.error(
            event="auth_case_not_found",
            conversation_id=str(conversation_id),
            case_id=conversation.case_id,
        )
        raise HTTPException(status_code=500, detail="内部错误：关联工单不存在")

    if case_owner != client_id:
        logger.warning(
            event="auth_conversation_forbidden",
            conversation_id=str(conversation_id),
            case_id=conversation.case_id,
            client_id=client_id,
        )
        raise HTTPException(status_code=403, detail="无权访问此会话")

    logger.info(
        event="auth_conversation_access_granted",
        conversation_id=str(conversation_id),
        case_id=conversation.case_id,
        client_id=client_id,
    )


async def verify_case_ownership(
    case_id: str,
    request: Request,
    service,
) -> None:
    """
    验证当前客户端是否有权操作指定工单（用于按 case 维度创建/查询会话）。

    Raises:
        HTTPException 401: 身份签名缺失/无效（严格模式）
        HTTPException 403: 工单不属于当前客户端
    """
    client_id = await authenticate_request(request)
    if client_id is None:
        return

    case_owner = await _get_case_client_id(service, case_id)
    if case_owner is None:
        raise HTTPException(status_code=404, detail="工单不存在")

    if case_owner != client_id:
        logger.warning(
            event="auth_case_forbidden",
            case_id=case_id,
            client_id=client_id,
        )
        raise HTTPException(status_code=403, detail="无权访问此工单")


def sanitize_message_content(content: str) -> str:
    """
    清理消息内容中的危险协议（XSS 纵深防御的一层）。

    注意：这是辅助措施，XSS 的主防线是前端渲染层输出编码（DOMPurify）。
    """
    dangerous_protocols = [
        r"javascript\s*:",
        r"vbscript\s*:",
        r"data\s*:",
        r"file\s*:",
    ]

    sanitized = content
    for protocol in dangerous_protocols:
        sanitized = re.sub(protocol, "", sanitized, flags=re.IGNORECASE)

    return sanitized


def check_request_size(content_length: int, max_size_mb: int = 1) -> None:
    """
    检查请求体大小，防止 DoS 攻击。

    Raises:
        HTTPException 413: 请求体过大
    """
    max_size_bytes = max_size_mb * 1024 * 1024

    if content_length > max_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"请求体过大，最大允许 {max_size_mb}MB",
        )
