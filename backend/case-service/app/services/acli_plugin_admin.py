"""acli 插件管理端服务：保存配置即创建下发任务。

职责：把 admin-ui 提交的插件规格收敛成不可变 revision，写入事实源，并创建
``pending`` 下发任务交给 ``AcliPluginDeployRelay``。页面只传规格与环境代号，
不传 node_ip / 凭据 / bridge 地址；环境表里的目标是服务端解析的。

幂等：同一 ``spec_hash`` + 环境已有进行中或已成功且 ``current_spec_hash``
已对齐的任务时，直接返回原 ``deploy_id``，不重复写 HCI。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.domain.acli_plugin_spec import (
    build_descriptor,
    normalize_spec,
    validate_descriptor,
    validate_spec,
)


class AdminValidationError(ValueError):
    """管理端提交不合法；``errors`` 直接回显到页面。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def prepare_publication(
    raw_spec: dict[str, Any],
    *,
    env_code: str,
    actor: str = "",
    deploy_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """纯函数：校验规格并算出本次发布的全部确定性输入。"""
    if not env_code or not env_code.strip():
        raise AdminValidationError(["env_code 必填"])
    spec = normalize_spec(raw_spec)
    validation = validate_spec(spec)
    if not validation["ok"]:
        raise AdminValidationError(validation["errors"])
    descriptor = build_descriptor(spec, deploy_id=deploy_id, now=now)
    descriptor_check = validate_descriptor(descriptor)
    if not descriptor_check["ok"]:
        raise AdminValidationError(descriptor_check["errors"])
    return {
        "spec": spec,
        "validation": validation,
        "spec_hash": descriptor["spec_hash"],
        "deploy_id": descriptor["deploy_id"],
        "plugin_name": spec["plugin_name"],
        "display_name": str(spec.get("display_name") or spec["plugin_name"]),
        "update_mode": str(spec["update_mode"]),
        "env_code": env_code.strip(),
        "actor": actor,
        "smoke_command": descriptor["smoke_command"],
        "verify_commands": descriptor["verify_commands"],
    }


# 回显给管理端的列：刻意排除 node_ip / lease_owner 等服务端内部字段，
# 保证页面既不能提交、也不会读到目标主机地址。
_DEPLOYMENT_COLUMNS = """deploy_id, plugin_name, revision, spec_hash, env_code, status,
    attempt, exec_id, trace_id, verification, bridge_result,
    error_code, error_message, created_at, finished_at"""


class AcliPluginAdminStore:
    """管理端持久化；所有 SQL 在同一事务内提交，避免「有规格没任务」。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def _script(self, statements: list[tuple[str, dict[str, Any]]]) -> list[list[dict[str, Any]]]:
        from sqlalchemy import text

        async with self._db.async_session_factory() as session:
            results: list[list[dict[str, Any]]] = []
            for sql, params in statements:
                cursor = await session.execute(text(sql), params)
                results.append([dict(row) for row in cursor.mappings().all()])
            await session.commit()
            return results

    async def save_and_publish(self, prepared: dict[str, Any]) -> dict[str, Any]:
        env_code = prepared["env_code"]
        spec_hash = prepared["spec_hash"]
        plugin_name = prepared["plugin_name"]

        env_rows = await self._script(
            [
                (
                    """
                    SELECT env_code, node_ip FROM acli_plugin_environment
                     WHERE env_code = :env_code AND enabled = true
                    """,
                    {"env_code": env_code},
                )
            ]
        )
        env = env_rows[0][0] if env_rows and env_rows[0] else None
        if env is None:
            raise AdminValidationError([f"环境 {env_code} 未登记或已停用"])

        existing = await self._script(
            [
                (
                    """
                    SELECT d.deploy_id, d.status
                      FROM acli_plugin_deployment d
                      JOIN acli_plugin p ON p.plugin_name = d.plugin_name
                     WHERE d.plugin_name = :plugin_name
                       AND d.spec_hash = :spec_hash
                       AND d.env_code = :env_code
                       AND d.status IN ('pending', 'claimed', 'running', 'succeeded')
                       AND p.current_spec_hash = :spec_hash
                     ORDER BY d.created_at DESC
                     LIMIT 1
                    """,
                    {"plugin_name": plugin_name, "spec_hash": spec_hash, "env_code": env_code},
                )
            ]
        )
        if existing and existing[0]:
            row = existing[0][0]
            return {
                "deploy_id": row["deploy_id"],
                "spec_hash": spec_hash,
                "status": row["status"],
                "idempotent": True,
                "env_code": env_code,
            }

        spec_json = json.dumps(prepared["spec"], ensure_ascii=False, sort_keys=True)
        validation_json = json.dumps(prepared["validation"], ensure_ascii=False)
        # asyncpg 会为同一绑定参数在不同位置推断出不同类型（INSERT 值位置是
        # varchar，WHERE 比较位置是 text），复用同名参数会触发
        # AmbiguousParameterError。子查询一律用独立参数并显式 CAST 收敛类型。
        await self._script(
            [
                (
                    """
                    INSERT INTO acli_plugin_spec
                        (plugin_name, revision, spec_json, spec_hash, validation, created_by)
                    VALUES (
                        CAST(:plugin_name AS varchar),
                        (SELECT COALESCE(MAX(revision), 0) + 1 FROM acli_plugin_spec
                          WHERE plugin_name = CAST(:revision_plugin_name AS varchar)),
                        CAST(:spec_json AS jsonb), CAST(:spec_hash AS varchar),
                        CAST(:validation AS jsonb), CAST(:actor AS varchar)
                    )
                    ON CONFLICT (spec_hash) DO NOTHING
                    """,
                    {
                        "plugin_name": plugin_name,
                        "revision_plugin_name": plugin_name,
                        "spec_json": spec_json,
                        "spec_hash": spec_hash,
                        "validation": validation_json,
                        "actor": prepared["actor"],
                    },
                ),
                (
                    """
                    INSERT INTO acli_plugin
                        (plugin_name, display_name, update_mode, desired_spec_hash, created_by)
                    VALUES (:plugin_name, :display_name, :update_mode, :spec_hash, :actor)
                    ON CONFLICT (plugin_name) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        update_mode = EXCLUDED.update_mode,
                        desired_spec_hash = EXCLUDED.desired_spec_hash,
                        updated_at = now()
                    """,
                    {
                        "plugin_name": plugin_name,
                        "display_name": prepared["display_name"],
                        "update_mode": prepared["update_mode"],
                        "spec_hash": spec_hash,
                        "actor": prepared["actor"],
                    },
                ),
                (
                    """
                    INSERT INTO acli_plugin_deployment
                        (deploy_id, plugin_name, revision, spec_hash, env_code, node_ip, status)
                    SELECT CAST(:deploy_id AS varchar), CAST(:plugin_name AS varchar),
                           COALESCE((SELECT MAX(revision) FROM acli_plugin_spec
                                      WHERE spec_hash = CAST(:revision_spec_hash AS varchar)), 1),
                           CAST(:spec_hash AS varchar), CAST(:env_code AS varchar),
                           CAST(:node_ip AS varchar), 'pending'
                    RETURNING deploy_id, status
                    """,
                    {
                        "deploy_id": prepared["deploy_id"],
                        "plugin_name": plugin_name,
                        "revision_spec_hash": spec_hash,
                        "spec_hash": spec_hash,
                        "env_code": env_code,
                        "node_ip": env["node_ip"],
                    },
                ),
            ]
        )
        return {
            "deploy_id": prepared["deploy_id"],
            "spec_hash": spec_hash,
            "status": "pending",
            "idempotent": False,
            "env_code": env_code,
        }

    async def list_plugins(self) -> list[dict[str, Any]]:
        rows = await self._script(
            [
                (
                    """
                    SELECT p.plugin_name, p.display_name, p.update_mode,
                           p.current_spec_hash, p.desired_spec_hash,
                           p.last_deploy_status, p.last_deploy_at, p.enabled,
                           s.revision, s.spec_json, s.validation
                      FROM acli_plugin p
                      LEFT JOIN LATERAL (
                          SELECT revision, spec_json, validation
                            FROM acli_plugin_spec sp
                           WHERE sp.plugin_name = p.plugin_name
                           ORDER BY sp.revision DESC
                           LIMIT 1
                      ) s ON true
                     ORDER BY p.plugin_name
                    """,
                    {},
                )
            ]
        )
        return rows[0]

    async def list_deployments(self, plugin_name: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._script(
            [
                (
                    f"""
                    SELECT {_DEPLOYMENT_COLUMNS} FROM acli_plugin_deployment
                     WHERE plugin_name = :plugin_name
                     ORDER BY created_at DESC
                     LIMIT :limit
                    """,
                    {"plugin_name": plugin_name, "limit": limit},
                )
            ]
        )
        return rows[0]

    async def get_deployment(self, deploy_id: str) -> dict[str, Any] | None:
        rows = await self._script(
            [
                (
                    f"SELECT {_DEPLOYMENT_COLUMNS} FROM acli_plugin_deployment WHERE deploy_id = :deploy_id",
                    {"deploy_id": deploy_id},
                )
            ]
        )
        return rows[0][0] if rows and rows[0] else None

    async def list_environments(self) -> list[dict[str, Any]]:
        """只返回页面需要的字段；node_ip / credential_ref / bridge_url 不下发给页面。"""
        rows = await self._script(
            [
                (
                    """
                    SELECT env_code, display_name, enabled, auth_type
                      FROM acli_plugin_environment
                     ORDER BY env_code
                    """,
                    {},
                )
            ]
        )
        return rows[0]


__all__ = ["AcliPluginAdminStore", "AdminValidationError", "prepare_publication"]
