"""acli 插件下发状态机：抢租约 → 服务端下发 → 设备侧回执 → 幂等收敛。

设计参照 ``LogCollectRelayService`` 的租约/心跳/自愈骨架，但下发通道完全不同：
这里由 case-service 自己连 terminal_bridge，不依赖任何浏览器在线，也不接受
前端上报的「成功」。只有设备侧 ``ACP_RESULT status=success`` + 退出码 0 +
非空真实冒烟输出，才会把 ``current_spec_hash`` 推进到本次规格。

状态：
    pending → claimed → running → succeeded | failed
    failed 且 attempt < max_attempts：按指数退避冷却（写 lease_until）后重新认领
    claimed/running 且租约过期：视为执行者已死，由 claim() 重新接管
``lease_until`` 在 running 表示租约到期时间，在 failed 表示下次可重试时间。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.domain.acli_plugin_spec import (
    build_descriptor,
    normalize_spec,
    parse_deploy_result,
    validate_descriptor,
    validate_spec,
)
from app.services.acli_plugin_bridge import (
    BridgeDeployError,
    load_credentials,
    resolve_bridge_url,
)

logger = logging.getLogger(__name__)


class RelayDeployError(Exception):
    """下发阶段的结构化失败；``code`` 落到 ``error_code``。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


class AcliPluginStore(Protocol):
    """状态机持久化接口；生产实现为 ``SqlAcliPluginStore``，单测用内存实现。"""

    async def claim(
        self,
        *,
        owner: str,
        lease_seconds: int,
        max_attempts: int,
        now: datetime,
        exec_id: str,
        trace_id: str,
    ) -> dict[str, Any] | None: ...

    async def mark_running(self, deploy_id: str, owner: str, until: datetime) -> bool: ...

    async def heartbeat(self, deploy_id: str, owner: str, until: datetime) -> bool: ...

    async def get_context(self, deploy_id: str) -> dict[str, Any] | None: ...

    async def finish(
        self,
        deploy_id: str,
        *,
        owner: str,
        status: str,
        verification: Mapping[str, Any],
        bridge_result: Mapping[str, Any],
        error_code: str,
        error_message: str,
        finished_at: datetime,
        lease_until: datetime | None,
    ) -> bool: ...

    async def mark_succeeded_plugin(self, plugin_name: str, spec_hash: str, at: datetime) -> None: ...


class SqlAcliPluginStore:
    """基于 ``acli_plugin_deployment`` 的原子租约实现。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def _rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._db.async_session_factory() as session:
            result = await session.execute(text(sql), params)
            # 不带 RETURNING 的 UPDATE 没有结果集；只有 SELECT/RETURNING 才读行。
            rows = [dict(row) for row in result.mappings().all()] if result.returns_rows else []
            await session.commit()
            return rows

    async def claim(
        self,
        *,
        owner: str,
        lease_seconds: int,
        max_attempts: int,
        now: datetime,
        exec_id: str,
        trace_id: str,
    ) -> dict[str, Any] | None:
        rows = await self._rows(
            """
            UPDATE acli_plugin_deployment
               SET status = 'claimed',
                   lease_owner = :owner,
                   lease_until = :lease_until,
                   attempt = attempt + 1,
                   exec_id = :exec_id,
                   trace_id = :trace_id,
                   error_code = '',
                   error_message = ''
             WHERE deploy_id = (
                   SELECT deploy_id FROM acli_plugin_deployment
                    WHERE status = 'pending'
                       OR (status = 'failed' AND attempt < :max_attempts
                           AND (lease_until IS NULL OR lease_until <= :now))
                       OR (status IN ('claimed', 'running') AND lease_until <= :now)
                    ORDER BY created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
             )
            RETURNING deploy_id, plugin_name, spec_hash, env_code, node_ip,
                      attempt, exec_id, trace_id, status
            """,
            {
                "owner": owner,
                "lease_until": now + timedelta(seconds=lease_seconds),
                "max_attempts": max_attempts,
                "now": now,
                "exec_id": exec_id,
                "trace_id": trace_id,
            },
        )
        return rows[0] if rows else None

    async def mark_running(self, deploy_id: str, owner: str, until: datetime) -> bool:
        rows = await self._rows(
            """
            UPDATE acli_plugin_deployment SET status = 'running', lease_until = :until
             WHERE deploy_id = :deploy_id AND lease_owner = :owner AND status = 'claimed'
            RETURNING deploy_id
            """,
            {"deploy_id": deploy_id, "owner": owner, "until": until},
        )
        return bool(rows)

    async def heartbeat(self, deploy_id: str, owner: str, until: datetime) -> bool:
        rows = await self._rows(
            """
            UPDATE acli_plugin_deployment SET lease_until = :until
             WHERE deploy_id = :deploy_id AND lease_owner = :owner AND status = 'running'
            RETURNING deploy_id
            """,
            {"deploy_id": deploy_id, "owner": owner, "until": until},
        )
        return bool(rows)

    async def get_context(self, deploy_id: str) -> dict[str, Any] | None:
        rows = await self._rows(
            """
            SELECT d.deploy_id, d.plugin_name, d.spec_hash, d.env_code, d.node_ip AS snapshot_node_ip,
                   s.spec_json,
                   e.node_ip, e.ssh_port, e.auth_type, e.credential_ref, e.bridge_url, e.enabled
              FROM acli_plugin_deployment d
              JOIN acli_plugin_spec s ON s.spec_hash = d.spec_hash
              LEFT JOIN acli_plugin_environment e ON e.env_code = d.env_code
             WHERE d.deploy_id = :deploy_id
            """,
            {"deploy_id": deploy_id},
        )
        return rows[0] if rows else None

    async def finish(
        self,
        deploy_id: str,
        *,
        owner: str,
        status: str,
        verification: Mapping[str, Any],
        bridge_result: Mapping[str, Any],
        error_code: str,
        error_message: str,
        finished_at: datetime,
        lease_until: datetime | None,
    ) -> bool:
        rows = await self._rows(
            """
            UPDATE acli_plugin_deployment
               SET status = :status,
                   verification = CAST(:verification AS jsonb),
                   bridge_result = CAST(:bridge_result AS jsonb),
                   error_code = :error_code,
                   error_message = :error_message,
                   finished_at = :finished_at,
                   lease_owner = '',
                   lease_until = :lease_until
             WHERE deploy_id = :deploy_id
               AND lease_owner = :owner
               AND status IN ('claimed', 'running')
            RETURNING deploy_id
            """,
            {
                "deploy_id": deploy_id,
                "owner": owner,
                "status": status,
                "verification": json.dumps(dict(verification), ensure_ascii=False),
                "bridge_result": json.dumps(dict(bridge_result), ensure_ascii=False),
                "error_code": error_code,
                "error_message": error_message[:2000],
                "finished_at": finished_at,
                "lease_until": lease_until,
            },
        )
        return bool(rows)

    async def mark_succeeded_plugin(self, plugin_name: str, spec_hash: str, at: datetime) -> None:
        await self._rows(
            """
            UPDATE acli_plugin
               SET current_spec_hash = :spec_hash,
                   last_deploy_status = 'succeeded',
                   last_deploy_at = :at,
                   updated_at = :at
             WHERE plugin_name = :plugin_name
            """,
            {"plugin_name": plugin_name, "spec_hash": spec_hash, "at": at},
        )


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes)):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise RelayDeployError("spec_unreadable", "spec_json 不是对象")


class AcliPluginDeployRelay:
    """周期认领下发任务；一次任务一条独立 bridge 连接。"""

    def __init__(
        self,
        *,
        store: AcliPluginStore,
        bridge_client: Any,
        default_bridge_url: str,
        credentials_json: str = "",
        credentials_file: str = "",
        lease_seconds: int = 180,
        max_attempts: int = 3,
        deploy_timeout: int = 180,
        heartbeat_interval: float = 30.0,
        owner: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._bridge = bridge_client
        self._default_bridge_url = default_bridge_url
        self._credentials_json = credentials_json
        self._credentials_file = credentials_file
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._deploy_timeout = deploy_timeout
        self._heartbeat_interval = heartbeat_interval
        self._owner = owner or f"case-service-{uuid.uuid4().hex[:8]}"
        self._now = now_fn or (lambda: datetime.now(UTC))

    async def reconcile_once(self) -> bool:
        """认领并执行一条任务；返回 False 表示当前没有可认领任务。"""
        deployment = await self._store.claim(
            owner=self._owner,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
            now=self._now(),
            exec_id=str(uuid.uuid4()),
            trace_id=str(uuid.uuid4()),
        )
        if deployment is None:
            return False
        await self._run(deployment)
        return True

    async def reconcile_loop(self, *, interval: float = 5.0) -> None:
        while True:
            try:
                # 单轮最多处理 20 条，避免大批量任务把事件循环长期占满。
                for _ in range(20):
                    if not await self.reconcile_once():
                        break
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 后台循环不能因单轮异常退出
                logger.exception("acli_plugin_reconcile_failed")
            await asyncio.sleep(interval)

    async def _run(self, deployment: Mapping[str, Any]) -> None:
        deploy_id = str(deployment["deploy_id"])
        owner = self._owner
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(deploy_id, owner), name=f"acli-plugin-heartbeat-{deploy_id}"
        )
        try:
            await self._deploy(deployment)
        except Exception as exc:  # noqa: BLE001 — 所有失败统一收敛成结构化回执
            code = getattr(exc, "code", None) or "acli_plugin_deploy_failed"
            await self._finish_failed(deployment, code=code, message=str(exc)[:2000])
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _finish_failed(
        self,
        deployment: Mapping[str, Any],
        *,
        code: str,
        message: str,
        verification: Mapping[str, Any] | None = None,
        bridge_result: Mapping[str, Any] | None = None,
    ) -> None:
        """写失败终态并按指数退避冷却；保留设备侧自检证据供页面定位。"""
        deploy_id = str(deployment["deploy_id"])
        attempt = int(deployment.get("attempt") or 1)
        retry_after = self._now() + timedelta(seconds=min(2**attempt, 60))
        logger.warning("acli_plugin_deploy_failed deploy_id=%s code=%s attempt=%s", deploy_id, code, attempt)
        await self._store.finish(
            deploy_id,
            owner=self._owner,
            status="failed",
            verification=dict(verification or {"ok": False}) | {"error_code": code},
            bridge_result=bridge_result or {},
            error_code=code,
            error_message=message[:2000],
            finished_at=self._now(),
            lease_until=retry_after,
        )

    async def _deploy(self, deployment: Mapping[str, Any]) -> None:
        deploy_id = str(deployment["deploy_id"])
        owner = self._owner
        running = await self._store.mark_running(
            deploy_id, owner, self._now() + timedelta(seconds=self._lease_seconds)
        )
        if not running:
            raise RelayDeployError("lease_lost", "租约已被其他实例接管")

        context = await self._store.get_context(deploy_id)
        if context is None:
            raise RelayDeployError("context_missing", "下发任务缺少规格或环境")
        node_ip = str(context.get("node_ip") or "")
        if not node_ip or context.get("enabled") is False:
            raise RelayDeployError("environment_missing", "目标环境未登记或已停用")

        spec = normalize_spec(_as_mapping(context.get("spec_json")))
        validation = validate_spec(spec)
        if not validation["ok"]:
            raise RelayDeployError("spec_invalid", "; ".join(validation["errors"]))
        descriptor = build_descriptor(spec, deploy_id=deploy_id)
        descriptor_check = validate_descriptor(descriptor)
        if not descriptor_check["ok"]:
            raise RelayDeployError("descriptor_invalid", "; ".join(descriptor_check["errors"]))

        credential = load_credentials(
            self._credentials_json,
            credential_ref=str(context.get("credential_ref") or ""),
            env_code=str(deployment.get("env_code") or ""),
            credentials_file=self._credentials_file,
        )
        bridge_url = resolve_bridge_url(str(context.get("bridge_url") or ""), self._default_bridge_url)
        result = await self._bridge.deploy(
            bridge_url=bridge_url,
            node_ip=node_ip,
            ssh_port=int(context.get("ssh_port") or 22),
            credential=credential,
            command=descriptor["deploy_command"],
            case_id=f"htp-acli-{deploy_id}",
            exec_id=str(deployment.get("exec_id") or ""),
            trace_id=str(deployment.get("trace_id") or ""),
            timeout=self._deploy_timeout,
        )

        verification = parse_deploy_result(result.get("stdout") or "", exit_code=int(result.get("exit_code", -1)))
        verification["spec_hash"] = descriptor["spec_hash"]
        verification["smoke_command"] = descriptor["smoke_command"]
        verification["exec_id"] = deployment.get("exec_id")
        verification["trace_id"] = deployment.get("trace_id")
        bridge_result = {
            "exit_code": result.get("exit_code"),
            "timed_out": result.get("timed_out"),
            "error_type": result.get("error_type"),
            "stderr_tail": str(result.get("stderr") or "")[-2000:],
        }
        expected_spec_hash = str(deployment.get("spec_hash") or "")
        if expected_spec_hash and verification.get("device_spec_hash") != expected_spec_hash:
            # 防错配：HCI 上跑的必须是本次任务生成的脚本，旧脚本的回执不能让新配置生效。
            await self._finish_failed(
                deployment,
                code="spec_hash_mismatch",
                message="设备侧回执的 spec_hash 与下发任务不一致",
                verification=verification,
                bridge_result=bridge_result,
            )
            return
        if not verification["ok"]:
            await self._finish_failed(
                deployment,
                code=verification.get("device_error") or "verification_failed",
                message="设备侧自检未通过（status/退出码/冒烟输出）",
                verification=verification,
                bridge_result=bridge_result,
            )
            return

        # 幂等收敛：只有仍持租约的实例能写终态；成功后才推进 current_spec_hash。
        await self._store.finish(
            deploy_id,
            owner=owner,
            status="succeeded",
            verification=verification,
            bridge_result=bridge_result,
            error_code="",
            error_message="",
            finished_at=self._now(),
            lease_until=None,
        )
        await self._store.mark_succeeded_plugin(
            str(deployment["plugin_name"]), descriptor["spec_hash"], self._now()
        )

    async def _heartbeat_loop(self, deploy_id: str, owner: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                alive = await self._store.heartbeat(
                    deploy_id, owner, self._now() + timedelta(seconds=self._lease_seconds)
                )
            except Exception:  # noqa: BLE001 — 心跳失败不阻断本次部署，由租约过期机制兜底
                return
            if not alive:
                logger.warning("acli_plugin_lease_lost deploy_id=%s", deploy_id)
                return


__all__ = [
    "AcliPluginDeployRelay",
    "AcliPluginStore",
    "BridgeDeployError",
    "RelayDeployError",
    "SqlAcliPluginStore",
]
