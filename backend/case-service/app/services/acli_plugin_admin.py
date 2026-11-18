"""acli 插件管理端服务：保存配置即创建下发任务。

职责：把 admin-ui 提交的插件规格收敛成不可变 revision，写入事实源，创建
``pending`` 下发任务，并把**部署命令**一并返回给页面——执行由持有 SSH 会话的
浏览器在桥连接上完成（与 acli_sync / log_collect 同一模型），服务端不连桥。
页面只传规格与会话标识，不传口令 / 私钥 / bridge 地址。

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
        # 设备侧部署命令（大脚本已按分片切成多条），由浏览器逐条执行。
        "deploy_commands": descriptor.get("deploy_commands") or [descriptor["deploy_command"]],
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
                # 不带 RETURNING 的 INSERT/UPDATE 没有结果集，读取 mappings()
                # 会抛 ResourceClosedError；只有查询类语句才需要收集行。
                if cursor.returns_rows:
                    results.append([dict(row) for row in cursor.mappings().all()])
                else:
                    results.append([])
            await session.commit()
            return results

    async def save_and_publish(self, prepared: dict[str, Any]) -> dict[str, Any]:
        # 目标来自 custom-ui + terminal_bridge 当前已登录会话，不查静态环境白名单。
        env_code = str(prepared.get("target_case_id") or prepared["env_code"])
        target_node_ip = str(prepared.get("target_node_ip") or "")
        if not env_code or not target_node_ip:
            raise AdminValidationError(["目标会话无效：请先从 bridge 已登录会话列表选择"])
        spec_hash = prepared["spec_hash"]
        plugin_name = prepared["plugin_name"]

        env = {"node_ip": target_node_ip}

        # 幂等判定拆成两段（原实现把两件事塞进一条 SQL 才出的 bug）：
        #   (a) in-flight：同一 spec 正在下发 → 复用 deploy_id。
        #       不能带 current_spec_hash——它只在成功后推进，带上会让"首次未成功"的
        #       重复提交不命中，同一脚本排队执行两次。
        #   (b) succeeded：只有节点当前态确实等于该规格才算完成态幂等。
        # force=True 跳过 (b)：acli 重装会清空插件目录，节点被清空后管理员必须能重发。
        force = bool(prepared.get("force"))
        params = {"plugin_name": plugin_name, "spec_hash": spec_hash, "env_code": env_code}
        checks: list[tuple[str, dict[str, Any]]] = []
        if not force:
            checks = [
                (
                    """
                    SELECT d.deploy_id, d.status
                      FROM acli_plugin_deployment d
                     WHERE d.plugin_name = :plugin_name
                       AND d.spec_hash = :spec_hash
                       AND d.env_code = :env_code
                       AND d.status IN ('pending', 'claimed', 'running')
                     ORDER BY d.created_at DESC
                     LIMIT 1
                    """,
                    params,
                ),
                (
                    """
                    SELECT d.deploy_id, d.status
                      FROM acli_plugin_deployment d
                      JOIN acli_plugin p ON p.plugin_name = d.plugin_name
                     WHERE d.plugin_name = :plugin_name
                       AND d.spec_hash = :spec_hash
                       AND d.env_code = :env_code
                       AND d.status = 'succeeded'
                       AND p.current_spec_hash = :spec_hash
                     ORDER BY d.created_at DESC
                     LIMIT 1
                    """,
                    params,
                ),
            ]
        existing = await self._script(checks) if checks else []
        for rows in existing:
            if not rows:
                continue
            row = rows[0]
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
                        (deploy_id, plugin_name, revision, spec_hash, env_code, node_ip,
                         status, exec_id, trace_id)
                    SELECT CAST(:deploy_id AS varchar), CAST(:plugin_name AS varchar),
                           COALESCE((SELECT MAX(revision) FROM acli_plugin_spec
                                      WHERE spec_hash = CAST(:revision_spec_hash AS varchar)), 1),
                           CAST(:spec_hash AS varchar), CAST(:env_code AS varchar),
                           CAST(:node_ip AS varchar), 'pending',
                           CAST(:exec_id AS varchar), CAST(:trace_id AS varchar)
                    RETURNING deploy_id, status
                    """,
                    {
                        "deploy_id": prepared["deploy_id"],
                        "plugin_name": plugin_name,
                        "revision_spec_hash": spec_hash,
                        "spec_hash": spec_hash,
                        "env_code": env_code,
                        "node_ip": env["node_ip"],
                        "exec_id": str(prepared.get("exec_id") or ""),
                        "trace_id": str(prepared.get("trace_id") or ""),
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

    async def finish_reported(
        self,
        deploy_id: str,
        *,
        status: str,
        verification: dict[str, Any],
        bridge_result: dict[str, Any],
        error_code: str,
        error_message: str,
    ) -> dict[str, Any] | None:
        """写入浏览器回报的终态；已终结的任务不再覆盖（幂等）。"""
        await self._script(
            [
                (
                    """
                    UPDATE acli_plugin_deployment
                       SET status = :status,
                           verification = CAST(:verification AS jsonb),
                           bridge_result = CAST(:bridge_result AS jsonb),
                           error_code = :error_code,
                           error_message = :error_message,
                           finished_at = now()
                     WHERE deploy_id = :deploy_id
                       AND status IN ('pending', 'claimed', 'running')
                    """,
                    {
                        "deploy_id": deploy_id,
                        "status": status,
                        "verification": json.dumps(verification, ensure_ascii=False),
                        "bridge_result": json.dumps(bridge_result, ensure_ascii=False),
                        "error_code": error_code[:64],
                        "error_message": error_message[:2000],
                    },
                )
            ]
        )
        return await self.get_deployment(deploy_id)

    async def mark_plugin_succeeded(self, plugin_name: str, spec_hash: str) -> None:
        """只有设备侧自检通过后才推进 current_spec_hash。"""
        await self._script(
            [
                (
                    """
                    UPDATE acli_plugin
                       SET current_spec_hash = :spec_hash,
                           last_deploy_status = 'succeeded',
                           last_deploy_at = now(),
                           updated_at = now()
                     WHERE plugin_name = :plugin_name
                    """,
                    {"plugin_name": plugin_name, "spec_hash": spec_hash},
                )
            ]
        )

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
        """兼容旧调用：静态环境白名单已不再是下发目标事实源，返回空列表。"""
        return []


__all__ = ["AcliPluginAdminStore", "AdminValidationError", "prepare_publication"]
