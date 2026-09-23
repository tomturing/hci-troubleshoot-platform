"""数据访问层：账号凭证校验、角色读取、登录审计与会话写入。

自包含 SQLAlchemy async session（与 shared DatabaseManager 解耦，避免引入未知接口）。
所有查询走现有 unified schema：user / user_credential / user_role / auth_audit / auth_session。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.security.password import verify_password

_engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


# ── 角色白名单（T2a）：应用层校验，弥补 roles jsonb / user_role 无 DB 级 CHECK ──
def validate_roles(realm: str, roles: list[str]) -> bool:
    """校验角色集合全部属于该 realm 的合法白名单。"""
    allowed = settings.ADMIN_ROLES if realm == "admin" else settings.CUSTOMER_ROLES
    return set(roles).issubset(allowed)


async def get_roles(session: AsyncSession, user_id: Any) -> list[str]:
    res = await session.execute(
        text("SELECT role FROM user_role WHERE user_id = :uid"),
        {"uid": user_id},
    )
    return [r[0] for r in res.all()]


async def _write_audit(
    session: AsyncSession,
    *,
    user_id: Any | None,
    realm: str,
    action: str,
    result: str,
    identifier: str | None,
    ip: str | None,
    ua: str | None,
    credential_id: Any | None = None,
    trace_id: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO auth_audit
                (user_id, realm, action, result, identifier, ip, user_agent, trace_id)
            VALUES
                (:user_id, :realm, :action, :result, :identifier, :ip, :ua, :trace_id)
            """
        ),
        {
            "user_id": user_id,
            "realm": realm,
            "action": action,
            "result": result,
            "identifier": identifier,
            "ip": ip,
            "ua": ua,
            "trace_id": trace_id,
        },
    )


async def _write_session(
    session: AsyncSession,
    *,
    user_id: Any,
    realm: str,
    token_version: int,
    ip: str | None,
    ua: str | None,
    expires_at: float,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO auth_session
                (user_id, realm, token_version, ip, user_agent, expires_at)
            VALUES
                (:user_id, :realm, :tv, :ip, :ua, to_timestamp(:exp))
            """
        ),
        {
            "user_id": user_id,
            "realm": realm,
            "tv": token_version,
            "ip": ip,
            "ua": ua,
            "exp": expires_at,
        },
    )


class AuthError(Exception):
    """认证失败的可恢复错误（含语义）。"""


async def authenticate_by_password(
    *,
    realm: str,
    identifier: str,
    password: str,
    ip: str | None = None,
    ua: str | None = None,
    trace_id: str | None = None,
) -> dict:
    """按账号密码认证。返回含 user_id / roles / token_version 的字典；失败抛 AuthError。"""
    async with SessionLocal() as session:
        res = await session.execute(
            text(
                """
                SELECT uc.credential_id, uc.secret, uc.failed_attempts, uc.locked_until,
                       u.user_id, u.status, u.token_version, u.realm
                FROM user_credential uc
                JOIN "user" u ON u.user_id = uc.user_id
                WHERE uc.credential_type = 'password'
                  AND uc.identifier = :ident
                  AND u.realm = :realm
                  AND uc.status = 'active'
                """
            ),
            {"ident": identifier, "realm": realm},
        )
        row = res.mappings().first()

        if row is None:
            await _write_audit(
                session,
                user_id=None,
                realm=realm,
                action="login",
                result="denied",
                identifier=identifier,
                ip=ip,
                ua=ua,
                trace_id=trace_id,
            )
            await session.commit()
            raise AuthError("invalid_credentials")

        # 锁定检查
        locked_until = row["locked_until"]
        if locked_until is not None and locked_until.timestamp() > time.time():
            await _write_audit(
                session,
                user_id=row["user_id"],
                realm=realm,
                action="login",
                result="denied",
                identifier=identifier,
                ip=ip,
                ua=ua,
                credential_id=row["credential_id"],
                trace_id=trace_id,
            )
            await session.commit()
            raise AuthError("locked")

        # 密码校验
        if not verify_password(row["secret"], password):
            await session.execute(
                text("UPDATE user_credential SET failed_attempts = failed_attempts + 1 WHERE credential_id = :cid"),
                {"cid": row["credential_id"]},
            )
            await _write_audit(
                session,
                user_id=row["user_id"],
                realm=realm,
                action="login",
                result="failed",
                identifier=identifier,
                ip=ip,
                ua=ua,
                credential_id=row["credential_id"],
                trace_id=trace_id,
            )
            await session.commit()
            raise AuthError("invalid_credentials")

        if row["status"] != "active":
            await session.commit()
            raise AuthError("disabled")

        # 成功：清零失败计数 + 读取角色 + 审计 + 会话
        await session.execute(
            text("UPDATE user_credential SET failed_attempts = 0 WHERE credential_id = :cid"),
            {"cid": row["credential_id"]},
        )
        roles = await get_roles(session, row["user_id"])
        if not validate_roles(realm, roles):
            # 角色越权（数据异常），拒绝签发，记录告警
            await _write_audit(
                session,
                user_id=row["user_id"],
                realm=realm,
                action="login",
                result="denied",
                identifier=identifier,
                ip=ip,
                ua=ua,
                credential_id=row["credential_id"],
                trace_id=trace_id,
            )
            await session.commit()
            raise AuthError("role_violation")

        ttl = settings.ACCESS_TOKEN_TTL_ADMIN if realm == "admin" else settings.ACCESS_TOKEN_TTL_CUSTOMER
        expires_at = time.time() + ttl
        await _write_audit(
            session,
            user_id=row["user_id"],
            realm=realm,
            action="login",
            result="success",
            identifier=identifier,
            ip=ip,
            ua=ua,
            credential_id=row["credential_id"],
            trace_id=trace_id,
        )
        await _write_session(
            session,
            user_id=row["user_id"],
            realm=realm,
            token_version=row["token_version"],
            ip=ip,
            ua=ua,
            expires_at=expires_at,
        )
        await session.commit()

        return {
            "user_id": str(row["user_id"]),
            "roles": roles,
            "token_version": row["token_version"],
            "realm": realm,
            "ttl": ttl,
        }
