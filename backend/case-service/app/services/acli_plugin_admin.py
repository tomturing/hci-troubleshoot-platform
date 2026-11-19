"""acli 插件管理端服务：保存规格即创建不可变 revision。

职责：把 admin-ui 提交的插件规格收敛成不可变 revision，写入事实源。
**插件到达节点不再由本服务驱动**：旧下发链路（部署任务/回报/轮询）已随链路收口
删除，节点收敛改由客户侧 acli_sync 第二阶段拉取平台 manifest 驱动（S4c）。
页面只传规格，不传口令 / 私钥 / bridge 地址，也不再传目标会话。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.domain.acli_plugin_spec import (
    build_descriptor,
    compile_descriptor,
    normalize_spec,
    validate_descriptor,
    validate_spec,
)


class AdminValidationError(ValueError):
    """管理端提交不合法；``errors`` 直接回显到页面。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def prepare_spec(
    raw_spec: dict[str, Any],
    *,
    actor: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """纯函数：校验规格并算出本次保存的全部确定性输入。"""
    spec = normalize_spec(raw_spec)
    validation = validate_spec(spec)
    if not validation["ok"]:
        raise AdminValidationError(validation["errors"])
    descriptor = build_descriptor(spec, now=now)
    descriptor_check = validate_descriptor(descriptor)
    if not descriptor_check["ok"]:
        raise AdminValidationError(descriptor_check["errors"])
    return {
        "spec": spec,
        "validation": validation,
        "spec_hash": descriptor["spec_hash"],
        "plugin_name": spec["plugin_name"],
        "display_name": str(spec.get("display_name") or spec["plugin_name"]),
        "update_mode": str(spec["update_mode"]),
        "actor": actor,
        # 编译产物随 revision 一起固化：manifest 端点因此零重算直出。
        # 必须在保存时定稿而不是每次现算——``register_item.last_updated_at``
        # 取的是构建时刻，同一条 revision 反复重算会让节点侧「无修改」永远判不出来。
        "compiled": compile_descriptor(descriptor),
    }


class AcliPluginAdminStore:
    """管理端持久化；所有 SQL 在同一事务内提交，避免「有规格没档案」。"""

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

    async def save_spec(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """仅保存规格：写不可变 revision + 更新插件档案，不创建任何下发任务。

        同一 ``spec_hash`` 重复保存命中 ON CONFLICT DO NOTHING，revision 不重复增长；
        幂等返回当前版本号供页面展示。
        """
        spec_hash = prepared["spec_hash"]
        plugin_name = prepared["plugin_name"]
        spec_json = json.dumps(prepared["spec"], ensure_ascii=False, sort_keys=True)
        # validation 里并入 compiled：revision 行的 validation 因此同时是
        # 「校验回执 + 编译产物」，阶段二 manifest 直接读它，不再重算。
        validation_payload = {**prepared["validation"], "compiled": prepared["compiled"]}
        validation_json = json.dumps(validation_payload, ensure_ascii=False)
        # asyncpg 会为同一绑定参数在不同位置推断出不同类型（INSERT 值位置是
        # varchar，WHERE 比较位置是 text），复用同名参数会触发
        # AmbiguousParameterError。子查询一律用独立参数并显式 CAST 收敛类型。
        rows = await self._script(
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
                    SELECT revision FROM acli_plugin_spec
                     WHERE plugin_name = CAST(:plugin_name AS varchar)
                       AND spec_hash = CAST(:spec_hash AS varchar)
                    """,
                    {"plugin_name": plugin_name, "spec_hash": spec_hash},
                ),
            ]
        )
        revision = rows[2][0]["revision"] if rows and rows[2] else None
        return {
            "plugin_name": plugin_name,
            "revision": revision,
            "spec_hash": spec_hash,
            "status": "saved",
        }

    async def list_plugins(self) -> list[dict[str, Any]]:
        rows = await self._script(
            [
                (
                    """
                    SELECT p.plugin_name, p.display_name, p.update_mode,
                           p.desired_spec_hash, p.enabled,
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

    async def list_desired(self) -> list[dict[str, Any]]:
        """阶段二期望态：启用插件的「期望 revision」固化编译产物。

        故意按 ``desired_spec_hash`` 而不是「最新 revision」取：保存失败或回滚
        期望指针时，manifest 不能把未生效的 revision 发给节点；两边同一口径
        （档案指针 + 命中该 hash 的 revision）才能保证拉到的就是管理员确认过的期望态。
        """
        rows = await self._script(
            [
                (
                    """
                    SELECT p.plugin_name, p.desired_spec_hash, s.revision, s.spec_json, s.validation
                      FROM acli_plugin p
                      LEFT JOIN LATERAL (
                          SELECT revision, spec_json, validation
                            FROM acli_plugin_spec sp
                           WHERE sp.plugin_name = p.plugin_name
                             AND sp.spec_hash = p.desired_spec_hash
                           LIMIT 1
                      ) s ON true
                     WHERE p.enabled = true
                     ORDER BY p.plugin_name
                    """,
                    {},
                )
            ]
        )
        return rows[0]


__all__ = ["AcliPluginAdminStore", "AdminValidationError", "prepare_spec"]
