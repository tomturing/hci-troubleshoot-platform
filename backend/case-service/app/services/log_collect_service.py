"""日志收集任务服务：状态机与业务规则。

状态机（与 acli 阶段一一对应）：

    pending → probing → installing → collecting → packaging → uploading → completed
       └───────────────┴──────────────┴────────────┴───────────┴──→ failed / cancelled

允许跳步（例如未安装 acli 时 probing → collecting、无上传时 packaging → completed），
但禁止从终态复活、禁止进度回退——回退通常意味着前端轮询发生了节点错配（progress
只反映发起节点，路由错了会读到旧任务或空任务）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from ..domain.log_collect_display import snapshot_artifact_evidence
from ..models.log_collect import LogCollectTask
from ..repositories.log_collect_repo import LogCollectTaskRepository
from ..schemas.log_collect import (
    TERMINAL_STATUSES,
    LogCollectTaskCreate,
    LogCollectTaskPatch,
)
from .log_collect_error_attribution import UNATTRIBUTED_CODE, derive_error_code

logger = logging.getLogger(__name__)

# probe 快照入库上限：探测失败原因里会夹 acli 原始日志行，不设上限会让 detail 无界增长。
_PROBE_SNAPSHOT_MAX_BYTES = 8192
_PROBE_NOTE_MAX_CHARS = 1024

# 公共 PATCH（浏览器可伪造）绝不能写这些字段：交付是否成立只能由服务端取证。
_PUBLIC_PATCH_RESERVED_FIELDS = (
    "delivery_status",
    "delivery_error",
    "delivery_finished_at",
    "relay_status",
)

# detail 是证据容器：这些键只能由探测/核验/订正等服务端动作写入。
# 放开它们等于允许前端 bug 伪造「产物已校验」「交付已核验」。
_PUBLIC_DETAIL_RESERVED_KEYS = frozenset(
    {
        "artifact_quality",
        "artifact_reason",
        "artifact_evidence",
        "display_audit",
        "delivery_evidence",
        "delivery_evidence_note",
        "p1_archive_check",
        "error_code_derivation",
        "baseline_report_hash",
        "write_audit",
    }
)


def _build_task_detail(payload: LogCollectTaskCreate) -> dict[str, Any]:
    """把"下发前的事实"固化进任务 detail。

    第一性原理：任务失败后能自证的前提，是创建那一刻的环境被记下来了。
    `baseline_report_hash` 只回答"报告是谁的"，回答不了"哪台机器、哪版 acli、
    节点清单为什么是空的、日志服务器通不通"——而这些恰是事后定因的唯一入口。
    """
    detail: dict[str, Any] = {}
    if payload.baseline_report_hash:
        detail["baseline_report_hash"] = payload.baseline_report_hash
    if payload.probe:
        probe = dict(payload.probe)
        for key in ("catalog_note", "note"):
            value = probe.get(key)
            if isinstance(value, str) and len(value) > _PROBE_NOTE_MAX_CHARS:
                probe[key] = value[:_PROBE_NOTE_MAX_CHARS] + "…（已截断，完整内容见 bridge 事件）"
        detail["probe"] = probe
        if len(json.dumps(detail, ensure_ascii=False, default=str)) > _PROBE_SNAPSHOT_MAX_BYTES:
            # 超限时保留可用于对账的最小集合，不让 detail 变成大对象存储。
            detail["probe"] = {
                "truncated": True,
                "acli_version": probe.get("acli_version"),
                "acli_build_time": probe.get("acli_build_time"),
                "architecture": probe.get("architecture"),
                "hosts_count": probe.get("hosts_count"),
                "catalog_source": probe.get("catalog_source"),
                "log_server_reachable": probe.get("log_server_reachable"),
                "http_code": probe.get("http_code"),
            }
    return detail


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"probing", "installing", "collecting", "failed", "cancelled"}),
    "probing": frozenset({"installing", "collecting", "failed", "cancelled"}),
    "installing": frozenset({"collecting", "failed", "cancelled"}),
    "collecting": frozenset({"packaging", "uploading", "completed", "failed", "cancelled"}),
    "packaging": frozenset({"uploading", "completed", "failed", "cancelled"}),
    "uploading": frozenset({"completed", "failed", "cancelled"}),
    # 纠正（correct）：平台已归档，但完成判定不可信（典型是同 ticket 的历史任务报告）。
    # 只在前端判定"完成态不可信"时，由用户显式点击触发，且会清空该任务的产物记录。
    "completed": frozenset({"collecting"}),
    # 接管（adopt）：平台已判失败/取消，但节点上任务仍在跑。
    # 节点是唯一真相源，允许把投影改回收集中，继续跟进；不做自动重试，避免重复采集。
    "failed": frozenset({"collecting"}),
    "cancelled": frozenset({"collecting"}),
}


class LogCollectError(Exception):
    """带规范错误码的业务异常基类（错误码见 log_collect_error_catalog.json）。"""

    code = "LC-PLATFORM-006"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class TaskNotFoundError(LogCollectError):
    """任务不存在（或不属于当前工单）。"""

    code = "LC-PLUGIN-008"


class TaskConflictError(LogCollectError):
    """同一工单已有活跃任务，或任务已结束无法操作。

    ``active_task`` 供路由层生成 409 摘要（状态/进度/是否本人），
    让前端直接展示"查看任务状态"而不必再查一次。
    """

    code = "LC-PLUGIN-007"

    def __init__(
        self,
        message: str,
        active_task: LogCollectTask | None = None,
        code: str | None = None,
    ):
        super().__init__(message, code=code)
        self.active_task = active_task


class InvalidTransitionError(LogCollectError):
    """非法状态转换或进度回退。"""

    code = "LC-PLATFORM-003"


class LogCollectService:
    def __init__(self, repo: LogCollectTaskRepository):
        self.repository = repo

    @staticmethod
    def validate_transition(current: str, target: str) -> None:
        if current == target:
            return
        if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise InvalidTransitionError(f"非法状态转换：{current} → {target}")

    async def create_task(
        self,
        payload: LogCollectTaskCreate,
        client_id: str,
        *,
        custom_scenario: tuple[UUID, int] | None = None,
        config_sha256: str | None = None,
        path_count: int | None = None,
    ) -> LogCollectTask:
        # 应用层先判一次（友好报错），数据库部分唯一索引兜底并发。
        active = await self.repository.get_active_by_case(payload.case_id)
        if active is not None:
            raise TaskConflictError(
                f"工单 {payload.case_id} 已有进行中的日志收集任务：{active.task_id}"
                "（若由其他客户端发起，请等待其结束后再试，勿重复下发）",
                active_task=active,
            )

        task = LogCollectTask(
            # 工单号只有一个：acli -s、产物名与任务归属全部使用 case_id
            case_id=payload.case_id,
            origin_node_ip=payload.origin_node_ip,
            modules=list(payload.modules),
            date_from=payload.date_from,
            date_to=payload.date_to,
            ips=list(payload.ips),
            extra_paths=list(payload.extra_paths) or None,
            # 自定义场景：id+revision 证明"用的哪一版"，sha256 证明"下发的就是这一版"。
            # config 原文刻意不落库：它只在创建响应里透传给 bridge 写节点，
            # 平台不留副本，避免同一份配置出现两个可漂移的真源。
            custom_scenario_id=custom_scenario[0] if custom_scenario else None,
            custom_scenario_revision=custom_scenario[1] if custom_scenario else None,
            config_sha256=config_sha256,
            path_count=path_count,
            remote_url=payload.remote_url,
            # 交付路径是用户的**决策**，必须在创建那一刻落库：
            # 只在交付完成时补写的话，采集中的任务与补交付失败的任务都读不到它，
            # 事后也无法区分"用户选了 P2"和"P1 失败后降级成 P2"。
            # 未传时按 remote_url 推断，兼容未升级的客户端（P1 必带地址，P2 必不带）。
            upload_mode=payload.upload_mode or ("acli_direct" if payload.remote_url else "bridge_relay"),
            # 探测事实随任务落库：失败任务必须能回答"用的哪版 acli / 什么架构"。
            # 只存前端内存时，关面板或换端后这条证据就没了（2026-10-27 实测 38/38 全空）。
            arch=payload.arch,
            acli_version=payload.acli_version,
            status="pending",
            progress=0,
            # 下发锚定的报告指纹与探测快照必须随任务落库：前端内存里的基线在关面板、
            # 换浏览器、重启后会丢失，届时就无法判断"节点报告属于哪一次下发"。
            detail=_build_task_detail(payload),
            created_by=client_id,
            # trace_id 由平台兜底生成：前端若未传，任务仍必须有一个可贯穿
            # 「平台 → bridge → HCI → 回采日志」的链路 ID，否则远端无法按链路定位。
            trace_id=payload.trace_id or uuid4().hex,
        )
        return await self.repository.create(task)

    async def get_task(self, task_id) -> LogCollectTask:
        task = await self.repository.get_by_id(task_id)
        if task is None:
            raise TaskNotFoundError(str(task_id))
        return task

    async def list_tasks(
        self,
        case_id: str,
        *,
        created_by: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ):
        return await self.repository.list_by_case(case_id, created_by=created_by, limit=limit, offset=offset)

    async def update_task(
        self,
        task_id,
        patch: LogCollectTaskPatch,
        *,
        server_authoritative: bool = False,
    ) -> LogCollectTask:
        task = await self.get_task(task_id)
        data = patch.model_dump(exclude_none=True)

        # ── 事实与结论分离：node_observed 是推荐入口 ────────────────────────
        # 客户端只提交观测事实；status/progress 为老前端保留，但要留审计，
        # 事后能区分「节点事实推进的」与「旧入参推进的」。
        observed = data.pop("node_observed", None)
        write_audit: dict[str, Any] = {}
        if observed:
            observed = {key: value for key, value in observed.items() if value is not None}
            legacy = {key: data.pop(key) for key in ("status", "progress") if key in data}
            if legacy:
                write_audit["legacy_fields"] = sorted(legacy)
            data.update(observed)
            write_audit["node_observed_keys"] = sorted(observed)
        else:
            legacy = {key: data[key] for key in ("status", "progress") if key in data}
        if legacy:
            # 兼容入参的痕迹必须落库：否则无法解释「这次状态是谁推进的」。
            write_audit["legacy"] = legacy

        # ── 单一权威终态（single-writer）────────────────────────────────────
        # 第一性原理：产物是否到达只能由服务端拿到的持久化事实证明；浏览器一次
        # WS/HTTP 请求失败只说明「结果未知」，不能据此写任务终态。历史事故
        # Q2026110601054：relay 已 success/delivered、包已落 5888，前端却因通道
        # 断开写了 status=failed，三份状态互相打脸且无人回写。
        # 采用"静默忽略 + 结构化审计"而非 422：老前端不会被新契约打断。
        ignored: list[str] = []
        if not server_authoritative:
            ignored = [key for key in _PUBLIC_PATCH_RESERVED_FIELDS if key in data]
            for key in ignored:
                data.pop(key, None)
            # detail 是证据容器：包质量、交付证据、审计记录都只能由服务端写。
            # 客户端若带这些键，静默丢弃并留痕，避免「前端 bug 就能伪造交付证据」。
            detail_patch = data.get("detail")
            if isinstance(detail_patch, dict):
                reserved = sorted(key for key in detail_patch if key in _PUBLIC_DETAIL_RESERVED_KEYS)
                if reserved:
                    cleaned = {
                        key: value for key, value in detail_patch.items() if key not in _PUBLIC_DETAIL_RESERVED_KEYS
                    }
                    if cleaned:
                        data["detail"] = cleaned
                    else:
                        data.pop("detail", None)
                    ignored.extend(f"detail.{key}" for key in reserved)
            if task.delivery_status == "delivered" and data.get("status") == "failed":
                # 已交付事实不可逆：误判失败只能由服务端专用终态函数纠正。
                for key in ("status", "error_code", "error_message"):
                    data.pop(key, None)
                ignored.append("status(delivered_not_reversible)")
            if ignored:
                logger.info(
                    "task.terminal_write_ignored task_id=%s trace_id=%s keys=%s",
                    task_id,
                    getattr(task, "trace_id", ""),
                    ",".join(ignored),
                )
                # 被拒写入同样要留痕：否则前端会以为"写成功了"，排查时两头对不上。
                write_audit["ignored"] = ignored

        target_status = data.get("status")

        if target_status is not None:
            self.validate_transition(task.status, target_status)

        new_progress = data.get("progress")
        if new_progress is not None and task.status not in TERMINAL_STATUSES and new_progress < task.progress:
            raise InvalidTransitionError(f"进度不能回退：{task.progress} → {new_progress}", code="LC-PLATFORM-004")

        # 必须先把旧状态存下来：下面的 setattr 会把 task.status 直接改成目标值，
        # 之后再用 task.status 判断"是不是从终态复活"永远为假，接管/纠正分支会变成死代码。
        previous_status = task.status

        # 兼容/事实入参的审计与 detail 合并一起落库：
        # 事后必须能回答「这次是谁推进的状态、有没有被服务端拒写」。
        if write_audit:
            write_audit["at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            write_audit["trace_id"] = getattr(task, "trace_id", None)
            with_audit = dict(data.get("detail") or {})
            with_audit["write_audit"] = write_audit
            data["detail"] = with_audit

        # detail 是审计容器：基线指纹、交付路径降级记录等由不同动作分别写入。
        # PATCH 若整块覆盖，写"降级原因"就会把 baseline_report_hash 冲掉，
        # 后续归属判定随之失效。因此这里按字典合并；显式传 null 表示删除该键
        # （例如降级记录被纠正后需要撤销）。
        if data.get("detail") is not None:
            merged = dict(task.detail or {})
            for key, value in dict(data["detail"]).items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            data["detail"] = merged

        for key, value in data.items():
            setattr(task, key, value)

        # P1 的"已交付"是插件自报：节点直传日志服务器，平台拿不到任何回执。
        # 不改状态机（避免破坏既有记录），但必须把**判定依据**写进 detail，
        # 否则事后无法区分"平台核验过"与"只有 acli 说成功"（2026-10-28 Q2026102800331）。
        if task.upload_mode == "acli_direct" and task.delivery_status == "delivered":
            evidence = dict(task.detail or {})
            # 已由 verify-archive 回查过就是更强的证据，不能被"自报"降级覆盖：
            # 否则核验通过的记录也会被写成"平台无独立回执"（2026-11-04 现场）。
            if evidence.get("delivery_evidence") not in ("archive_verified", "acli_self_reported"):
                evidence["delivery_evidence"] = "acli_self_reported"
                evidence["delivery_evidence_note"] = (
                    "P1 直传：平台未回查归档件，delivered 仅依据 acli 自报（upload_state 或压缩包自删）；"
                    "如需确认请调用 verify-archive 核验"
                )
                task.detail = evidence
                # 交付终态以 delivered 登记后，之前残留的失败/核验否定证据
                # （如 LC-TRANSFER-007 stale_archive）不再适用，必须清零，
                # 否则会出现 delivered 与失败码并存的矛盾记录
                # （2026-11-06 Q2026110666667 / b7fa86c5：归档核验曾判 stale_archive，
                #  后续节点真实直传成功补登 delivered，error_code 却留着未清）。
                if task.error_code is not None:
                    task.error_code = None
                    task.error_message = None

        if target_status in TERMINAL_STATUSES:
            task.finished_at = datetime.now(UTC)
            if target_status == "completed":
                # 完成即 100%：插件 final 阶段偶发未刷新 progress，平台侧做确定性收敛。
                task.progress = 100
        elif target_status == "collecting" and previous_status in TERMINAL_STATUSES:
            # 接管 / 纠正：终态是"平台投影"，节点仍在跑时可以被覆盖。
            if previous_status == "completed":
                # 原完成记录里的产物未必属于本次任务（同 ticket 重复 create 会拿到上一次的报告），
                # 继续挂着会变成"采集中却显示产物位置"的新误导。清空产物字段，原值留审计；
                # 注意：只改 DB 投影，节点与日志服务器上的压缩包一律不删。
                audit = (
                    f"原完成记录已按节点态纠正：archive_path={task.archive_path}, "
                    f"archive_size={task.archive_size}, archive_sha256={task.archive_sha256}, "
                    f"relay_status={task.relay_status}"
                )
                task.archive_path = None
                task.archive_size = None
                task.archive_sha256 = None
                task.relay_status = "none"
                task.relay_chunk_index = None
                task.relay_chunk_total = None
                task.relay_restarts = 0
                task.cleanup_state = "none"
                task.cleanup_reason = None
                task.error_code = "corrected_by_node"
                task.error_message = audit
            else:
                # 接管失败/取消的任务：清掉上次的失败痕迹，
                # 否则界面会一边显示"采集中"一边显示旧错误原因。
                task.error_code = None
                task.error_message = None
            task.finished_at = None

        # ── 失败必须可归因：error_code 的唯一收口 ────────────────────────────
        # 现场实测（2026-11-12 staging，近 30 天）：118 个失败任务里 76 个（64.4%）
        # error_code 为空，全部来自「只写了 error_message、没写 error_code」的调用路径
        # （主要是浏览器端 WS 失败兜底与 bridge 原始文本透传）。空码让面板只能显示
        # 一句人话，按错误码聚合、告警、重试分级全部失效。
        # 第一性原理：失败可归因是平台的责任，不是每个调用点的自觉。因此在保存
        # 边界做确定性补全——只补空、绝不覆盖上游更精确的包装码。
        self.attribute_failure(task)

        # ── 产物证据快照：终态必须回答「有没有可用日志」────────────────────
        # 与迁移 065 同一口径（无包/过小判 invalid），但作用在新产生的行上；
        # 已由内容探测写入的结论优先级更高，snapshot 内部不会覆盖。
        snapshot = snapshot_artifact_evidence(task)
        if snapshot is not None:
            task.detail = snapshot

        task.updated_at = datetime.now(UTC)

        await self.repository.flush()
        await self.repository.refresh(task)
        return task

    @staticmethod
    def attribute_failure(task) -> None:
        """失败终态兜底补码：只补空，不覆盖上游更精确的包装码。

        归因顺序遵守「谁失败谁出码」：采集失败看 ``error_message``，
        交付失败（采集可能已 completed）看 ``delivery_error``，两者皆失优先采集原因。
        补码来源会写进 ``detail.error_code_derivation``，便于事后区分
        「上游产生的码」与「平台按文案推导的码」——后者若是推导错了，能一眼看出来。
        """
        if task.error_code:
            return
        collect_failed = task.status == "failed"
        delivery_failed = task.delivery_status == "failed"
        if not (collect_failed or delivery_failed):
            return
        message = task.error_message if collect_failed else (task.delivery_error or task.error_message)
        code = derive_error_code(message)
        task.error_code = code
        detail = dict(task.detail or {})
        detail["error_code_derivation"] = {
            "code": code,
            "stage": "collect" if collect_failed else "delivery",
            "source": "derived_from_message",
            "matched": bool(message and code != UNATTRIBUTED_CODE),
        }
        task.detail = detail
        logger.info(
            "task.error_code_derived task_id=%s code=%s stage=%s",
            getattr(task, "task_id", ""),
            code,
            "collect" if collect_failed else "delivery",
        )

    async def begin_delivery(self, task_id) -> LogCollectTask:
        """登记一次交付尝试，**不动 status**。

        为什么必须只动 delivery_* ：``status`` 是采集结论（completed 表示 acli 采完了），
        ``delivery_status`` 是产物去向。补交付发生在采集之后，改 status 会把
        "采集已完成"这个真实结论抹掉——2026-10-27 现场正是因为没有这层区分，
        才出现 completed + 平台零产物，而界面无处表达"采完了但没传上去"。
        """
        task = await self.get_task(task_id)
        now = datetime.now(UTC)
        # 补交付在物理上只能是 P2：acli 的上传是 ``create -u`` 的同一次执行，
        # 没有独立的重传子命令。包一旦没走成 P1，唯一可重放的动作就是经平台中转。
        # 在这里登记实际路径，而不是让前端再发一次 PATCH——少一次往返，也少一次漏写。
        task.upload_mode = "bridge_relay"
        task.delivery_status = "queued"
        task.delivery_started_at = now
        task.delivery_finished_at = None
        task.delivery_error = None
        # 上一次交付的完整性结论对新一次交付不成立：包可能被重采/覆盖。
        # 不复位就会出现"这次没校验、却顶着上次的 verified 徽标"。
        task.integrity_state = "unverified"
        task.delivery_attempts = (task.delivery_attempts or 0) + 1
        task.updated_at = now
        await self.repository.flush()
        await self.repository.refresh(task)
        return task

    async def cancel_task(self, task_id, reason: str | None = None) -> LogCollectTask:
        task = await self.get_task(task_id)
        if task.status in TERMINAL_STATUSES:
            raise TaskConflictError(f"任务已结束（{task.status}），无法取消", code="LC-PLATFORM-003")
        task.status = "cancelled"
        task.error_code = "cancelled_by_user"
        task.error_message = reason or "用户取消"
        task.finished_at = datetime.now(UTC)
        task.updated_at = datetime.now(UTC)
        await self.repository.flush()
        await self.repository.refresh(task)
        return task
