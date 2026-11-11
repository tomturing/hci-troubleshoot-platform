"""日志收集任务 API。

鉴权与 BOLA 防护与 environment 路由一致：网关签名身份 + 工单归属校验，
不存在/非归属统一 404，避免泄露资源存在性。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from shared.database.postgres import DatabaseManager
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..repositories.log_collect_repo import LogCollectTaskRepository
from ..schemas.log_collect import (
    DeliverRequest,
    DeliverResponse,
    LogCollectTaskCreate,
    LogCollectTaskListResponse,
    LogCollectTaskPatch,
    LogCollectTaskResponse,
    P1ArchiveVerifyRequest,
    P1ArchiveVerifyResponse,
    P1DeliveryAttestRequest,
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadPartResponse,
    UploadStatusResponse,
)
from ..schemas.log_collect_errors import build_problem
from ..security.auth import get_client_id, verify_case_exists
from ..services.log_collect_custom_scenario_service import (
    CustomScenarioNotFoundError,
    LogCollectCustomScenarioService,
    expand_scenario_paths,
    serialize_config,
    serialize_spc,
)
from ..services.log_collect_default_scenario_service import LogCollectDefaultScenarioService
from ..services.log_collect_node_path import expected_node_archive_path
from ..services.log_collect_relay_service import LogCollectRelayService, SangforRelayClient
from ..services.log_collect_service import (
    InvalidTransitionError,
    LogCollectService,
    TaskConflictError,
    TaskNotFoundError,
)
from ..services.log_collect_storage import (
    LocalLogCollectStorage,
    LogCollectStorageUnavailableError,
)
from ..services.log_collect_upload_service import LogCollectUploadError, LogCollectUploadService
from ..services.operation_audit import write_operation_audit
from ..services.p1_ticket import P1TicketError, P1TicketService, archive_filename_for

logger = logging.getLogger(__name__)


def _problem(code: str, detail: str, **extra) -> HTTPException:
    """把业务异常转成 RFC 7807 子集（status 由错误码目录决定）。

    ``extra`` 用于 409 这类需要携带结构化上下文（active_task）的场景。
    """
    problem = build_problem(code, detail)
    problem.update(extra)
    return HTTPException(status_code=int(problem["status"]), detail=problem)


def _active_task_summary(task, client_id: str) -> dict | None:
    """409 附带的活跃任务摘要（跨客户端脱敏）。

    只暴露"有任务、进行到哪、是不是本人"，不暴露 origin_node_ip / ips /
    archive_path：这些是采集目标与制品位置，跨客户端可见会构成信息泄露。
    """
    if task is None:
        return None
    return {
        "task_id": str(task.task_id),
        "status": task.status,
        "progress": task.progress,
        "created_by_me": task.created_by == client_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


router = APIRouter(prefix="/api/log-collect/tasks", tags=["log-collect"])

database_manager: DatabaseManager | None = None
upload_storage: LocalLogCollectStorage | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    """设置数据库管理器（由 main.py 调用）。"""
    global database_manager
    database_manager = db_manager


def get_upload_storage() -> LocalLogCollectStorage:
    global upload_storage
    if upload_storage is None:
        try:
            upload_storage = LocalLogCollectStorage(settings.LOG_COLLECT_STORAGE_ROOT)
        except LogCollectStorageUnavailableError as exc:
            # 存储不可用是平台侧环境事实。不捕获的话它会冒泡成裸 500
            # （现场只看到 "Request failed with status code 500"），
            # 这里转成 LC-PLATFORM-007，详情里带目录/用户/原因。
            raise _problem("LC-PLATFORM-007", str(exc)) from exc
    return upload_storage


async def get_log_collect_service() -> LogCollectService:
    if not database_manager:
        raise _problem("LC-PLATFORM-006", "Database not initialized")
    async for session in database_manager.get_session():
        yield LogCollectService(LogCollectTaskRepository(session))


async def get_upload_service(
    service: LogCollectService = Depends(get_log_collect_service),
) -> LogCollectUploadService:
    return LogCollectUploadService(service.repository, get_upload_storage())


def _upload_problem(exc: LogCollectUploadError) -> HTTPException:
    """上传类异常沿用 RFC 7807 结构，但以异常自身的 HTTP 状态为准。"""
    problem = build_problem(exc.code, str(exc))
    problem["status"] = exc.status
    return HTTPException(status_code=exc.status, detail=problem)


def _build_relay_service() -> LogCollectRelayService:
    """代传服务依赖：长任务自行开 session，因此注入 session 工厂而不是单个 session。"""
    if not database_manager:
        raise _problem("LC-PLATFORM-006", "Database not initialized")
    if not settings.LOG_COLLECT_RELAY_ENABLED:
        raise _problem("LC-TRANSFER-003", "日志服务器代传功能已禁用")
    return LogCollectRelayService(
        database_manager.async_session_factory,
        get_upload_storage(),
        SangforRelayClient(
            settings.LOG_COLLECT_RELAY_URL,
            token=settings.LOG_COLLECT_RELAY_TOKEN,
        ),
    )


async def get_relay_service() -> LogCollectRelayService:
    return _build_relay_service()


def get_p1_ticket_service() -> P1TicketService:
    """P1 直传票据服务。

    刻意不做进程级缓存：构造只是两次字符串处理的开销，而缓存会让"配置改了
    但进程不重启"变成一类难以复现的现场问题（密钥轮换时必须立刻生效）。
    """
    return P1TicketService(
        settings.LOG_COLLECT_P1_TICKET_SECRET,
        ttl_seconds=settings.LOG_COLLECT_P1_TICKET_TTL_HOURS * 3600,
    )


# task → task_id：优雅退出时必须知道该把哪些「在飞」代传交还给下一个实例。
_running_relays: dict[asyncio.Task, UUID] = {}


def _on_relay_done(task: asyncio.Task) -> None:
    """后台任务必须取走异常，否则 Python 只打印 'never retrieved' 且无业务上下文。"""
    _running_relays.pop(task, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("日志包代传失败 task=%s error=%s", task.get_name(), exc)


# 代传并发闸门（进程级单例）。
#
# 为什么必须挂在模块级而不是服务实例上：`get_relay_service()` 每个请求都新建一个
# LogCollectRelayService，挂在实例上的信号量等于每个请求各拿一把锁，闸门直接失效。
_relay_gate: asyncio.Semaphore | None = None


def _relay_concurrency() -> int:
    return max(1, int(settings.LOG_COLLECT_RELAY_MAX_CONCURRENCY))


def _relay_semaphore() -> asyncio.Semaphore:
    global _relay_gate
    if _relay_gate is None:
        _relay_gate = asyncio.Semaphore(_relay_concurrency())
    return _relay_gate


async def _run_relay_gated(
    relay_service: LogCollectRelayService,
    task_id: UUID,
    *,
    queue_timeout: float | None = None,
) -> None:
    """在并发闸门内执行代传，排队超时则交还租约。

    为什么要给排队设上限而不是一直等：`trigger()` 已经把任务标成 running，而
    running 只靠心跳协程续租；排队中的协程还没跑起来、自然没有心跳，等超过
    `stale_after/2` 就会被别的实例判成「执行者已死」并抢占，同一个包变成两个
    进程同时上传。交还成 pending 交给下一轮 reconcile 接管，是唯一不会双重
    执行的出口（代价是延后最多一个 reconcile 周期）。
    """
    semaphore = _relay_semaphore()
    # 默认取租约的一半；显式入参只为让单测把 5 秒压到毫秒级，生产调用一律走默认。
    if queue_timeout is None:
        queue_timeout = max(5.0, float(settings.LOG_COLLECT_RELAY_STALE_SECONDS) / 2)
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=queue_timeout)
    except TimeoutError:
        logger.warning(
            "日志包代传排队超时，交还租约等待下一轮接管 task=%s concurrency=%s",
            task_id,
            _relay_concurrency(),
        )
        await relay_service.release([task_id])
        return
    try:
        await relay_service.run(task_id)
    finally:
        semaphore.release()


def _spawn_relay(relay_service: LogCollectRelayService, task_id: UUID) -> None:
    background = asyncio.create_task(_run_relay_gated(relay_service, task_id))
    _running_relays[background] = task_id
    background.add_done_callback(_on_relay_done)


async def reconcile_relays_once() -> int:
    """接管一轮，返回本轮真正拉起的任务数。

    抽成单轮是为了让「启动接管」与「周期接管」共用同一判据，
    测试也能只跑一轮而不必等真实 sleep。
    """
    if database_manager is None or not settings.LOG_COLLECT_RELAY_ENABLED:
        return 0
    try:
        relay_service = _build_relay_service()
        # 先订正存量终态再抢占：已交付却仍标 running 的历史任务，光靠
        # claim_stale 排除是不会被拉起，但状态会永久错着，界面继续转圈。
        settled = await relay_service.settle_delivered_relays()
        if settled:
            logger.info("日志包代传终态订正 count=%s", settled)
        task_ids = await relay_service.claim_stale()
        for task_id in task_ids:
            _spawn_relay(relay_service, task_id)
        if task_ids:
            logger.info("日志包代传接管 count=%s tasks=%s", len(task_ids), task_ids)
        return len(task_ids)
    except Exception:  # noqa: BLE001 - 接管失败不能阻断服务
        logger.exception("日志包代传接管失败")
        return 0


async def reconcile_relays_on_startup() -> None:
    """启动即接管：上一个进程（发布/重启/OOM）留下的代传不会自己跑完。

    两类都要收：租约过期仍标 running 的（进程被 SIGKILL），以及优雅退出时
    交还成 pending 的。没这一步，「发布」就是代传状态永久卡 running 的触发器。
    """
    await reconcile_relays_once()


async def reconcile_relays_loop(*, interval: float = 60.0) -> None:
    """周期接管：只在启动跑一次是不够的。

    滚动发布的实际顺序是「新 Pod 先起来 → 旧 Pod 在 grace period 内把 running
    交还成 pending」。启动那一刻新实例看到的还是旧实例持有的 running（租约未过期，
    不会抢），而旧实例稍后才写 pending —— 此后没有任何代码再看一眼，
    任务就永久停在 pending：界面一直转圈、前端只轮询 relay_status，
    既不报错也不重试（2026-11-04 Q2026110400024 卡在 228/723 共 43 分钟，
    正是当天 case-service 发布造成的）。
    """
    while True:
        await reconcile_relays_once()
        await asyncio.sleep(interval)


async def shutdown_relays() -> None:
    """优雅退出：取消在飞代传并把租约交还成 pending（必须在关闭 DB 之前调用）。"""
    pairs = list(_running_relays.items())
    if not pairs:
        return
    for task, _task_id in pairs:
        task.cancel()
    await asyncio.gather(*[task for task, _ in pairs], return_exceptions=True)
    # 不依赖 done-callback 的时机：退出路径必须自己把登记表清干净，
    # 否则下一次 shutdown 会重复交还已经不属于本进程的任务。
    for task, _task_id in pairs:
        _running_relays.pop(task, None)
    if database_manager is None:
        return
    try:
        released = await _build_relay_service().release([task_id for _, task_id in pairs])
        logger.info("日志包代传退出交还 count=%s", released)
    except Exception:  # noqa: BLE001 - 退出路径不能抛，否则 DB 关不掉
        logger.exception("日志包代传退出交还失败")


@router.post("", response_model=LogCollectTaskResponse, status_code=201)
async def create_task(
    payload: LogCollectTaskCreate,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    tickets: P1TicketService = Depends(get_p1_ticket_service),
):
    """创建日志收集任务（同一工单只允许一个活跃任务）。

    只校验工单存在：允许跨客户端复用同一工单号（方案 A），任务按 created_by 隔离。
    """
    await verify_case_exists(payload.case_id, service.repository.session)

    # 自定义场景（acli -c）：平台是配置的唯一生成方。
    # 前端只传 scenario_id，拿到的 config_json 只负责透传给 bridge 写节点，
    # 既不能自己拼，也不展示为可编辑项。
    custom_scenario: tuple[UUID, int] | None = None
    config_plan: tuple[str, str, int] | None = None
    if payload.custom_scenario_id is not None:
        scenario_service = LogCollectCustomScenarioService(service.repository.session)
        try:
            scenario = await scenario_service.get_latest_by_id(str(payload.custom_scenario_id))
        except CustomScenarioNotFoundError as exc:
            raise HTTPException(status_code=422, detail="自定义场景不存在或已被删除") from exc
        if not scenario.enabled:
            raise HTTPException(status_code=422, detail="该自定义场景已停用，请重新选择")
        items = [item.model_dump() for item in scenario.items]
        if scenario.source == "spc":
            # spc 不做 {date} 展开：插件按 date_placeholders 与 -d 自行处理。
            # extra_paths 在这里转成 copy 条目，与清单一起下发（不能再走 -p）。
            for path in payload.extra_paths or []:
                items.append({"action": "copy", "path": path, "category": "file", "note": None})
            spc_text, spc_sha, _size = serialize_spc(items)
            custom_scenario = (scenario.scenario_id, scenario.revision)
            config_plan = (spc_text, spc_sha, len(items))
            config_kind = "spc"
            # 槽位固定 core：白名单内、原清单最轻，且已进 catalog 可勾回。
            payload.modules = ["core"]
        else:
            try:
                paths = expand_scenario_paths(
                    items,
                    payload.date_from,
                    payload.date_to,
                    extra_paths=payload.extra_paths,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            config_text, sha256, _size = serialize_config([{"path": path} for path in paths])
            custom_scenario = (scenario.scenario_id, scenario.revision)
            config_plan = (config_text, sha256, len(paths))
            config_kind = "config"

    try:
        task = await service.create_task(
            payload,
            client_id,
            custom_scenario=custom_scenario,
            config_sha256=config_plan[1] if config_plan else None,
            path_count=config_plan[2] if config_plan else None,
        )
    except TaskConflictError as exc:
        raise _problem(
            exc.code,
            str(exc),
            active_task=_active_task_summary(exc.active_task, client_id),
        ) from exc
    except IntegrityError as exc:
        # 并发窗口内两个请求同时通过应用层检查：数据库部分唯一索引兜底。
        # 这里不查活跃任务（session 已失效），前端可用"查看任务状态"重新拉取。
        raise _problem("LC-PLUGIN-007", "该工单已有进行中的日志收集任务") from exc

    # 请求级事实必须留痕：任务表存的是服务端归一化后的结果，回答不了
    # "用户当时选的是 P1 还是 P2、探测到了什么"。审计写失败不影响建任务。
    if database_manager is not None:
        await write_operation_audit(
            database_manager.async_session_factory,
            action="log_collect.create",
            resource_type="log_collect_task",
            resource_id=str(task.task_id),
            details={
                "case_id": payload.case_id,
                "origin_node_ip": payload.origin_node_ip,
                "upload_mode": task.upload_mode,
                "remote_url_set": bool(payload.remote_url),
                "modules": list(payload.modules),
                "date_from": str(payload.date_from),
                "date_to": str(payload.date_to),
                "ips": payload.ips,
                "acli_version": payload.acli_version,
                "arch": payload.arch,
                "probe": payload.probe,
            },
            trace_id=task.trace_id,
        )

    # 默认场景：任一勾选模块存在清单覆写 → 为所有勾选模块签发平台清单。
    # 放在建任务成功之后：冲突/校验失败不该多打一次库；清单服务异常只降级为
    # 原生默认场景（零行为变化），不能把采集整体挡住。
    spc_files: list[dict[str, str]] = []
    if payload.custom_scenario_id is None:
        try:
            manifest_service = LogCollectDefaultScenarioService(service.repository.session)
            spc_files = await manifest_service.build_spc_files(payload.modules)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "log_collect.manifest_lookup_degraded case_id=%s err=%s", payload.case_id, exc,
            )
            spc_files = []

    response = LogCollectTaskResponse.model_validate(task)
    if spc_files:
        slots = [str(item["slot"]) for item in spc_files]
        evidence = {
            "mode": "platform_manifest",
            "slots": slots,
            "sha256": {str(item["slot"]): str(item["sha256"]) for item in spc_files},
            "trace_id": task.trace_id,
        }
        # 证据必须落库：换端/关页面后仍能回答"这次按哪份清单收的"。
        task.detail = {**(task.detail or {}), "manifest": evidence}
        await service.repository.session.commit()
        response.detail = task.detail
        response.spc_files = spc_files
        response.spc_slots = slots
        logging.getLogger(__name__).info(
            "log_collect.manifest_selected case_id=%s task_id=%s trace_id=%s slots=%s",
            payload.case_id, task.task_id, task.trace_id, ",".join(slots),
        )
    if config_plan:
        # 只在创建响应里出现：bridge 需要原文写节点，平台不留副本。
        if config_kind == "spc":
            response.spc_json = config_plan[0]
            response.spc_sha256 = config_plan[1]
            response.spc_slot = "core"
        else:
            response.config_json = config_plan[0]
            response.config_sha256 = config_plan[1]
        response.path_count = config_plan[2]
    _attach_p1_ticket(response, tickets)
    return response


def _attach_p1_ticket(response: LogCollectTaskResponse, tickets: P1TicketService) -> None:
    """给 P1 任务附上直传票据；不满足条件时留空，由调用方决定是否降级。

    为什么不在缺票据时直接改 ``upload_mode``：路径是用户在创建那一刻做出的
    决策，落库后就不再改写（现有 P1→P2 降级也只在真的失败时发生）。这里只
    回答"这次能不能带凭据出门"，把降级交给真正会发包的一方。

    票据不进库：它 12 小时就过期，存下来只会让后续读到一张废票。
    """
    if response.upload_mode != "acli_direct":
        return
    filename = archive_filename_for(response.case_id)
    try:
        response.p1_ticket = tickets.issue(response.case_id, filename)
        response.p1_ticket_expires_at = tickets.verify(response.p1_ticket, filename=filename).expires_at
    except P1TicketError as exc:
        # 未配置密钥 / 输入异常都不阻断建任务：任务仍可走 P2。
        # 但必须在日志里说清是哪一类，否则 P1 全量 401 时会显得毫无征兆。
        logger.warning(
            "P1 票据签发失败 case=%s reason=%s（本次直传将不可用，应走 P2）",
            response.case_id,
            exc.code,
        )
        response.p1_ticket = None
        response.p1_ticket_expires_at = None


# 归档件的 mtime 允许比任务创建时间早多久：5888 与平台未必严格对时，
# 而"包在任务创建之前就存在"才是要拦的错配（历史同名包被当成本次结果）。
P1_ARCHIVE_LOOKBACK_SECONDS = 300


def _judge_p1_archive(
    task,
    archive: dict,
    expected_size: int | None,
) -> tuple[bool, str, int | None, datetime | None]:
    """判定 P1 归档是否可信，返回 (是否通过, 原因, 实际大小, 归档时间)。

    判据刻意只用 O(1) 可得的落盘事实：P1 链路上平台没有源包副本，重算 sha256
    既拿不到源端基准、又会让 5888 在读大包时卡住别的上传。因此用
    「存在 + 体积不小于记录值 + 不是历史遗留」三条，足以拦住截断上传与错包；
    更弱的完整性只能靠 P2（或重新采集）。

    为什么不是「大小必须相等」：expected_size 是**平台侧观测到的峰值**（P1 成功后
    插件会自删压缩包，落盘后就没有可信的源包体积了），它可能停在 zip 收尾前一刻。
    用这个估计值去要求字节级相等，会把一份完整归档判成 size_mismatch——2026-11-10
    Q2026111000243 现场就是这样把已上传成功的包记成了「交付失败」。
    截断只会让体积**变小**，因此只拦"比记录值小"，并在核验快照里同时留下两个数，
    让人工可以直接对照。
    """
    if not archive.get("found"):
        return False, "archive_missing", None, None

    archived_size = archive.get("size")
    if not isinstance(archived_size, int) or archived_size <= 0:
        return False, "archive_size_invalid", None, None
    if expected_size is not None and archived_size < expected_size:
        return False, "size_mismatch", archived_size, None

    mtime = archive.get("mtime")
    archived_at: datetime | None = None
    if isinstance(mtime, (int, float)):
        archived_at = datetime.fromtimestamp(int(mtime), tz=UTC)

    created_at = task.created_at
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if archived_at is not None and archived_at < created_at - timedelta(seconds=P1_ARCHIVE_LOOKBACK_SECONDS):
            return False, "stale_archive", archived_size, archived_at

    return True, "verified", archived_size, archived_at


def _p1_verify_patch(
    task,
    *,
    verified: bool,
    check: dict,
    checked_at: datetime,
) -> LogCollectTaskPatch:
    """把一次归档回查翻译成任务 patch：**只有核验通过才写交付终态**。

    否定结论一律不写 ``delivery_status``：
      - ``archive_missing``：5888 要等整包收完才发布正式文件名（上传中写暂存名），
        所以"查不到"最常见的原因是上传还没结束，而不是没传——acli 的状态序列是
        Success → Sending → Success，采集报完成时上传往往才刚开始；
      - ``size_mismatch`` / ``stale_archive``：同工单目录里可能还留着上一轮的历史包，
        一次探测分不清"这是这次的包"还是"上次没清掉的包"。

    把这两类瞬时/歧义观测写成 failed，就会留下"字节已经到了日志服务器、平台却红着
    说交付失败"的记录，而且此后没有任何写入者会纠正它
    （2026-11-10 Q2026111000243：包已在 /sf/log，界面写「交付失败」）。
    因此否定分支只落核验快照（何时、谁、看到了什么），交付状态保持原样，
    由前端如实展示"未核验到归档件"，并把「重新核验」留给用户重跑。
    """
    detail: dict = {"p1_archive_check": check}
    if verified:
        detail["delivery_evidence"] = "archive_verified"
        detail["delivery_evidence_note"] = "P1 直传：已由日志服务器按产物名回查归档件，体积不小于任务记录"
        return LogCollectTaskPatch(
            delivery_status="delivered",
            delivery_error=None,
            delivery_finished_at=checked_at,
            detail=detail,
        )

    # 已 delivered 的记录不被一次否定观测改写：交付事实不可逆，后续探测失败只当
    # 本次观测留档（可能是 5888 抖动，或归档件被人工挪走）。
    if task.delivery_status != "delivered":
        detail["delivery_evidence"] = "archive_unverified"
        detail["delivery_evidence_note"] = (
            f"P1 直传：本次回查未核验到归档件（{check.get('reason')}）；这只代表本次观测，"
            "不代表上传失败——上传可能仍在进行，节点侧插件成功后自删压缩包，平台无独立回执"
        )
    return LogCollectTaskPatch(detail=detail)


_UPLOAD_STATE_FAILED_TOKENS = (
    "失败",
    "错误",
    "异常",
    "fail",
    "error",
    "unauthorized",
    "timeout",
    "abnormal",
)
_UPLOAD_STATE_DELIVERED_TOKENS = (
    "成功",
    "success",
    "succeed",
    "complete",
    "finished",
    "done",
)


def _classify_upload_state(raw: str | None) -> str:
    """把 acli 的 ``upload_state`` 原文归为 delivered / failed / unknown。

    与前端 ``logCollectSync.classifyUploadState`` 同口径（先判失败再判成功、中英双语）。
    为什么必须两端逐字对齐：判据一漂就会出现"界面说成功、平台说失败"的双源矛盾
    （2026-11-06 英文环境 ``Status: Success`` 判不出已交付的现场）。
    这里只区分"是否含成功语义"与"是否明确失败"，不引入第三套判定。
    """
    text = (raw or "").strip()
    if not text:
        return "unknown"
    lower = text.lower()
    if any(token in text or token in lower for token in _UPLOAD_STATE_FAILED_TOKENS):
        return "failed"
    if any(token in lower for token in _UPLOAD_STATE_DELIVERED_TOKENS):
        return "delivered"
    return "unknown"


@router.post("/{task_id}/p1-delivered", response_model=LogCollectTaskResponse)
async def attest_p1_delivered(
    task_id: UUID,
    payload: P1DeliveryAttestRequest,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """登记「节点报告 P1 直传成功」，由服务端写入交付终态（弱证据入口）。

    第一性原理：交付终态必须由唯一写入者推进，而那唯一写入者是**服务端**，
    不是"唯一证据源是平台回查"。两条通道各有其位：

    - ``verify-archive``：服务端主动回查日志服务器，强证据；
    - 本接口：平台够不着节点、票据密钥又可能未配置，此时 acli 的自述是唯一可用证据。

    对抗性审查过的失败条件与处置：
    - 前端随意声称成功 → 请求只收 ``upload_state`` 原文，由服务端判定语义，
      判不出成功直接 409，不写任何终态；
    - 非 P1 任务（P2 已有 sha256 全链路校验）→ 409，不允许用弱证据覆盖强结论；
    - 重复调用 → 已 delivered 直接幂等返回，不刷新时间戳；
    - 事后无法区分"核验过"与"自报的" → ``log_collect_service.update_task`` 会写入
      ``delivery_evidence=acli_self_reported`` 与说明文案，且不覆盖 archive_verified。
    """
    task = await _owned_task(task_id, client_id, service)

    if task.upload_mode != "acli_direct":
        raise _problem(
            "LC-TRANSFER-003",
            "只有 P1 直传任务接受节点自报交付：P2 在合并阶段已有 sha256 校验",
        )
    if task.delivery_status == "delivered":
        # 幂等：重复自报不刷新 delivery_finished_at，时间戳必须与首次结论同源。
        return LogCollectTaskResponse.model_validate(task)

    phase = _classify_upload_state(payload.upload_state)
    if phase != "delivered":
        raise _problem(
            "LC-TRANSFER-007",
            f"节点报告未包含发送成功语义（判定结果：{phase}），不能据此登记已交付",
        )

    attested_at = datetime.now(UTC)
    patch = LogCollectTaskPatch(
        delivery_status="delivered",
        delivery_finished_at=attested_at,
        archive_size=payload.archive_size if task.archive_size is None else None,
        detail={
            "p1_self_report": {
                "upload_state": payload.upload_state,
                "archive_size": payload.archive_size,
                "attested_at": attested_at.isoformat(),
            }
        },
    )
    # 弱证据也必须经服务端权威入口落库：公共 PATCH 的 delivery_status 会被静默忽略，
    # 这也是"前端补登 delivered 却永远写不进去"那类回归的根因。
    updated = await service.update_task(task_id, patch, server_authoritative=True)
    logger.info(
        "P1 节点自报交付 task=%s trace_id=%s upload_state=%r evidence=acli_self_reported",
        task_id,
        getattr(updated, "trace_id", ""),
        payload.upload_state,
    )
    return LogCollectTaskResponse.model_validate(updated)


@router.post("/{task_id}/verify-archive", response_model=P1ArchiveVerifyResponse)
async def verify_p1_archive(
    task_id: UUID,
    payload: P1ArchiveVerifyRequest,
    service: LogCollectService = Depends(get_log_collect_service),
    tickets: P1TicketService = Depends(get_p1_ticket_service),
) -> P1ArchiveVerifyResponse:
    """核验 P1 直传是否真的落到了日志服务器。

    为什么必须存在：P1 的成功判据此前完全由 acli 单方面宣告
    （``upload_state=成功``），而平台连包都不留。一旦节点侧判错，就会出现
    "平台显示已交付、日志服务器上没有包，且节点已自动删包"这种不可恢复状态。

    这里只对 ``acli_direct`` 生效：P2 在分片合并阶段已有 sha256 全链路校验，
    用更弱的判据覆盖更强的结论只会制造假阳性。

    写入契约（2026-11-10 修正）：**只有核验通过才写 delivered，否定结论不写终态**。
    否定只落 ``detail.p1_archive_check`` 快照，交付状态保持原样。理由见下方实现注释：
    单次探测分不清"还没传完"与"传失败了"，而 failed 一旦写下就没有第二个写入者纠正。
    """
    try:
        task = await service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _problem(TaskNotFoundError.code, "Task not found") from exc

    if task.upload_mode != "acli_direct":
        raise _problem(
            "LC-TRANSFER-003",
            "只有 P1 直传任务需要归档核验：P2 在合并阶段已做过 sha256 校验",
        )
    if not tickets.enabled:
        raise _problem(
            "LC-TRANSFER-007",
            "平台未配置 P1 票据密钥，无法向日志服务器核验归档",
        )

    filename = archive_filename_for(task.case_id)
    expected_size = payload.archive_size if payload.archive_size is not None else task.archive_size

    client = SangforRelayClient(
        settings.LOG_COLLECT_RELAY_URL,
        token=settings.LOG_COLLECT_RELAY_TOKEN,
    )
    # 复核用的票据现签现用：不复用建任务时下发的那张，避免"前端拿到的票过期
    # 导致核验也一并失败"这种把两个环节耦合起来的故障模式。
    ticket = tickets.issue(task.case_id, filename)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as http:
            archive = await client.archive_status(http, filename, ticket=ticket)
    except httpx.HTTPError as exc:
        logger.warning("P1 归档查询失败 task=%s: %s", task_id, exc)
        raise _problem("LC-TRANSFER-007", f"查询日志服务器归档失败：{exc}") from exc

    verified, reason, archived_size, archived_at = _judge_p1_archive(task, archive, expected_size)

    checked_at = datetime.now(UTC)
    check = {
        "verified": verified,
        "reason": reason,
        "filename": filename,
        "stored_name": archive.get("stored_name"),
        "expected_size": expected_size,
        "archived_size": archived_size,
        "checked_at": checked_at.isoformat(),
    }
    patch = _p1_verify_patch(task, verified=verified, check=check, checked_at=checked_at)

    # 交付终态只能由这个内部入口写：这是"经过独立事实核验"的权威调用，
    # 必须绕过公共 PATCH 的客户端写入拦截（single-writer）。
    await service.update_task(task_id, patch, server_authoritative=True)

    logger.info(
        "P1 归档核验 task=%s verified=%s reason=%s expected=%s archived=%s",
        task_id,
        verified,
        reason,
        expected_size,
        archived_size,
    )

    return P1ArchiveVerifyResponse(
        task_id=task_id,
        verified=verified,
        reason=reason,
        filename=filename,
        expected_size=expected_size,
        archived_size=archived_size,
        archived_at=archived_at,
        target_path=archive.get("target_path"),
        # 响应回带本次核验快照。原实现引用了未定义的 `detail`（NameError → 500），
        # 「重新核验归档」按钮因此一直失败；前端会重新拉任务，但端点本身不该炸。
        detail=check,
    )


@router.get("/artifacts")
async def list_artifacts(
    case_id: str = Query(..., min_length=1, max_length=20, description="工单号"),
    limit: int = Query(20, ge=1, le=100),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
) -> dict:
    """平台侧产物索引：回答"这单平台有没有留包、包多大、sha 是多少"。

    第一性原理：P1 是节点直传日志服务器，平台**不留副本**，所以 object_* 为 NULL；
    P2 才会有对象 key/size/sha。此前只能进容器 `ls`，导致"有没有包"无法产品化回答。
    """
    await verify_case_exists(case_id, service.repository.session)
    items, _ = await service.list_tasks(case_id, created_by=client_id, limit=limit)
    return {
        "items": [
            {
                "task_id": str(task.task_id),
                "upload_mode": task.upload_mode,
                "object_key": task.object_key,
                "object_size": task.object_size,
                "object_sha256": task.object_sha256,
                "archive_size": task.archive_size,
                "delivery_status": task.delivery_status,
                "created_at": task.created_at,
            }
            for task in items
        ]
    }


@router.get("/probes")
async def list_probes(
    case_id: str = Query(..., min_length=1, max_length=20, description="工单号"),
    limit: int = Query(20, ge=1, le=100),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
) -> dict:
    """探测历史：把"节点清单为什么空、日志服务器为什么不可达"变成可回看的事实。

    数据源是 bridge 事件表（jsonb extra），只取下发前探测与下发两条事件。
    """
    await verify_case_exists(case_id, service.repository.session)
    rows = (
        (
            await service.repository.session.execute(
                text(
                    "SELECT created_at, event, level, extra "
                    "FROM bridge_execution_logs "
                    "WHERE case_id = :case_id "
                    "AND event IN ('log_collect.probe', 'log_collect.create') "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"case_id": case_id, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return {"items": [dict(row) for row in rows]}


# 节点清理路径的证据查询：只认 bridge 写下的原始 progress 报告，且路径必须与
# expected_node_archive_path(case_id) 逐字节相等（SQL 先过滤，避免把任意字符串
# 拉进 Python 再判断）。
_NODE_ARCHIVE_PATH_SQL = text(
    """
    SELECT 1
    FROM bridge_execution_logs
    WHERE case_id = :case_id
      AND event = 'log_collect.progress'
      AND extra->>'archive_path' = :expected_path
    LIMIT 1
    """
)


async def _attach_node_archive_path(
    responses: list[LogCollectTaskResponse],
    session,
    case_id: str,
) -> None:
    """仅当有节点侧证据时，给响应附上可展示的节点绝对路径。

    查不到证据就保持 None，前端会明确「不提供路径」。绝不退回用
    archive_path / object_key 去拼路径——那正是会误导用户的根源
    （2026-11-06 Q2026110600940）。证据查询失败也不能让任务列表整体 500。
    """
    if not responses:
        return
    expected = expected_node_archive_path(case_id)
    if expected is None:
        return
    try:
        row = (
            await session.execute(
                _NODE_ARCHIVE_PATH_SQL,
                {"case_id": case_id, "expected_path": expected},
            )
        ).first()
    except Exception:  # noqa: BLE001 - 只读旁证，不能影响主查询
        logger.exception("节点清理路径证据查询失败 case=%s", case_id)
        return
    if row is None:
        return
    for response in responses:
        response.node_archive_path = expected


@router.get("", response_model=LogCollectTaskListResponse)
async def list_tasks(
    case_id: str = Query(..., min_length=1, max_length=20, description="工单号"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """按工单查询任务列表（倒序），只返回当前身份创建的任务。"""
    await verify_case_exists(case_id, service.repository.session)
    items, total = await service.list_tasks(case_id, created_by=client_id, limit=limit, offset=offset)
    responses = [LogCollectTaskResponse.model_validate(item) for item in items]
    await _attach_node_archive_path(responses, service.repository.session, case_id)
    return {"total": total, "items": responses}


@router.get("/{task_id}", response_model=LogCollectTaskResponse)
async def get_task(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """查询单个任务（仅任务创建者可见）。"""
    task = await _owned_task(task_id, client_id, service)
    response = LogCollectTaskResponse.model_validate(task)
    await _attach_node_archive_path([response], service.repository.session, task.case_id)
    return response


@router.patch("/{task_id}", response_model=LogCollectTaskResponse)
async def update_task(
    task_id: UUID,
    patch: LogCollectTaskPatch,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """回写状态/进度/制品元数据（由前端从 bridge 轮询结果上报）。"""
    await _owned_task(task_id, client_id, service)
    try:
        return await service.update_task(task_id, patch)
    except (InvalidTransitionError, TaskConflictError) as exc:
        raise _problem(exc.code, str(exc)) from exc


@router.post("/{task_id}/cancel", response_model=LogCollectTaskResponse)
async def cancel_task(
    task_id: UUID,
    reason: str | None = Query(None, max_length=200),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """标记任务取消。

    注意：本接口只改平台状态；真正终止采集必须由前端经 bridge 执行
    ``acli log_collect cancel``（会 kill 集群采集进程并删除已收集的包）。
    """
    await _owned_task(task_id, client_id, service)
    try:
        return await service.cancel_task(task_id, reason)
    except TaskConflictError as exc:
        raise _problem(exc.code, str(exc)) from exc


# ── P2 段②：分块上传（控制面，经 API Gateway）────────────────────────────────


async def _owned_task(task_id: UUID, client_id: str, service: LogCollectService):
    """任务按创建者鉴权：跨客户端复用同一工单号时，彼此不可见/不可操作。

    与工单归属解耦后，同一 case_id 下可以同时存在多个客户端各自的任务；
    非创建者统一 404，避免泄露任务存在性。
    """
    try:
        task = await service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _problem(TaskNotFoundError.code, "Task not found") from exc
    if task.created_by != client_id:
        raise _problem(TaskNotFoundError.code, "Task not found")
    return task


@router.post("/{task_id}/deliver", response_model=DeliverResponse)
async def deliver_task(
    task_id: UUID,
    payload: DeliverRequest,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """补交付：为节点上**已有的**包签发上传会话，不重新采集、不改采集状态。

    为什么需要独立端点
    ------------------
    交付的物理前提只有两条：节点上有完整包、平台有上传会话。
    它和"采集走到哪一步"没有因果关系，因此必须能与采集状态机解耦：
    用户真正遇到的场景是"包早就采好了，只是没传上平台"，
    而 completed 是终态且纠正会清空产物，原设计下这种任务没有任何上传入口。

    幂等与并发
    ----------
    - 已 delivered 且未 ``force``：直接回现状，不重复开会话（避免误报"又传了一次"）；
    - 正在 pulling/uploading/relaying：拒绝，避免两个页面同时点导致互相 abort；
    - 会话大小不一致（LC-TRANSFER-004）：**自动 abort 后重建**。
      原实现把这个 409 抛给前端且前端从不 abort，会话就此死锁、只能重启整轮。
      重建是安全的：本端点代表用户的明确交付意图，此刻不存在"另一个上传正在进行"。
    """
    task = await _owned_task(task_id, client_id, service)

    if task.delivery_status == "delivered" and not payload.force:
        upload = await upload_service.status(task_id)
        return DeliverResponse(
            already_delivered=True,
            delivery_status=task.delivery_status,
            delivery_attempts=task.delivery_attempts or 0,
            # 已交付时故意不签发令牌：这是幂等短路，不该再给出"可以继续传"的凭据。
            # 用 dict 覆盖而不是同名关键字，避免 status() 也返回 token 后触发
            # "got multiple values for keyword argument"。
            upload=UploadInitResponse(**{**upload, "upload_token": ""}),
            source_path=task.archive_path or payload.source_path,
        )

    if task.delivery_status in {"pulling", "uploading", "relaying"}:
        raise _problem(
            "LC-PLATFORM-009",
            f"该任务正在交付中（{task.delivery_status}），请等待当前进度结束",
        )

    # 会话代际：服务端单调递增，作为 fencing token 编入上传令牌。
    # 用户每次明确表达交付意图（含重新采集后重签）都会推进代际，旧作业的
    # 令牌与分片在新会话里一律失效，不会混进新包的字节序列。
    epoch = int(task.delivery_attempts or 0) + 1
    init_kwargs = dict(
        total_size=payload.total_size,
        expected_sha256=payload.expected_sha256,
        archive_fingerprint=payload.archive_fingerprint,
        delivery_epoch=epoch,
    )
    try:
        upload = await upload_service.init(task, **init_kwargs)
    except LogCollectUploadError as exc:
        if getattr(exc, "status", None) != 409:
            raise _upload_problem(exc) from exc
        # 409 = 源包内容/大小/代际已变化。自动作废旧会话并按新基线重建，
        # 不把死锁抛给用户；分片几何在 init 时冻结，旧分片继续传必然每片失败。
        logger.warning(
            "delivery.session_rebuilt task_id=%s trace_id=%s epoch=%s detail=%s",
            task_id,
            task.trace_id,
            epoch,
            exc,
        )
        await upload_service.abort(task_id)
        try:
            upload = await upload_service.init(task, **init_kwargs)
        except LogCollectUploadError as retry_exc:
            raise _upload_problem(retry_exc) from retry_exc

    task = await service.begin_delivery(task_id)
    return DeliverResponse(
        already_delivered=False,
        delivery_status=task.delivery_status,
        delivery_attempts=task.delivery_attempts or 0,
        upload=UploadInitResponse(**upload),
        source_path=payload.source_path or task.archive_path,
    )


@router.post("/{task_id}/uploads", response_model=UploadInitResponse)
async def init_upload(
    task_id: UUID,
    payload: UploadInitRequest,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """初始化上传会话。

    幂等：同一任务重复调用返回既有会话与已传分片，客户端据此跳过已传部分；
    若声明的总大小与既有会话不一致则 409，要求显式 abort 后重建，避免续传出错包。
    """
    task = await _owned_task(task_id, client_id, service)
    try:
        return await upload_service.init(
            task,
            total_size=payload.total_size,
            expected_sha256=payload.expected_sha256,
            archive_fingerprint=payload.archive_fingerprint,
        )
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc


@router.get("/{task_id}/uploads", response_model=UploadStatusResponse)
async def get_upload_status(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """查询已上传分片，供断点续传使用。"""
    await _owned_task(task_id, client_id, service)
    try:
        return await upload_service.status(task_id)
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc


@router.post("/{task_id}/uploads/complete", response_model=UploadCompleteResponse)
async def complete_upload(
    task_id: UUID,
    payload: UploadCompleteRequest,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """流式合并分片并落库制品元数据。

    只更新 ``archive_path/archive_size/archive_sha256``，不改变任务状态：
    状态跃迁由前端在 P2 各阶段显式 PATCH，避免与状态机互相覆盖。
    """
    task = await _owned_task(task_id, client_id, service)
    try:
        return await upload_service.complete(
            task,
            parts=payload.parts,
            declared_sha256=payload.sha256,
            declared_size=payload.size,
        )
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc


@router.post("/{task_id}/uploads/abort", status_code=204)
async def abort_upload(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
) -> None:
    """中止上传并清理临时分片（不触碰已合并的成品）。"""
    await _owned_task(task_id, client_id, service)
    await upload_service.abort(task_id)


# ── P2 段③：代传 5888 日志服务器（异步长任务）─────────────────────────────────


@router.post("/{task_id}/relay", status_code=202)
async def start_relay(
    task_id: UUID,
    force: bool = Query(False, description="仅当上一进程崩溃导致 relay_status 卡在 running 时使用"),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    relay_service: LogCollectRelayService = Depends(get_relay_service),
):
    """启动代传。

    幂等：只有把 ``relay_status`` 从非 running 改成 running 的调用者才会真正执行，
    因此重复点击或前端重试不会产生两个并发上传（20 GiB 重传代价过高）。
    """
    task = await _owned_task(task_id, client_id, service)
    if not task.archive_path:
        raise _problem("LC-TRANSFER-001", "平台尚无制品，无法代传")

    started = await relay_service.trigger(task_id, force=force)
    if started:
        _spawn_relay(relay_service, task_id)
    return {
        "task_id": str(task_id),
        "started": started,
        "relay_status": "running" if started else task.relay_status,
    }


@router.get("/{task_id}/relay")
async def get_relay_status(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    relay_service: LogCollectRelayService = Depends(get_relay_service),
):
    """查询代传状态与断点位置（供前端展示与人工重试判断）。"""
    await _owned_task(task_id, client_id, service)
    snapshot = await relay_service.snapshot(task_id)
    if snapshot is None:
        raise _problem(TaskNotFoundError.code, "Task not found")
    return snapshot


# ── 对象数据面：直连 case-service，绕过 API Gateway 的体积限制 ────────────────

upload_router = APIRouter(prefix="/api/direct/log-collect-uploads", tags=["log-collect-upload"])


@upload_router.put("/{task_id}/parts/{part_number}", response_model=UploadPartResponse)
async def upload_part_direct(
    task_id: UUID,
    part_number: int,
    request: Request,
    upload_token: str = Header(alias="X-Upload-Token", min_length=8, max_length=256),
    part_sha256: str | None = Header(default=None, alias="X-Part-SHA256", pattern=r"^[0-9a-f]{64}$"),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """流式写入单个分片。

    该路径**不得**配置到 API Gateway：网关默认 1 MiB、豁免 10 MiB，
    而分片为 8 MiB（LOG_COLLECT_CHUNK_SIZE_BYTES，已在 10 MiB 豁免内，但仍应
    直连 case-service 以避免豁免额度被其它路由挤占）。鉴权改用 init 签发的
    短期 token（绑定 task_id）。
    """
    if not upload_service.verify_token(str(task_id), upload_token):
        raise HTTPException(
            status_code=401,
            detail=build_problem("LC-TRANSFER-004", "上传 token 无效或已过期"),
        )
    try:
        return await upload_service.record_part(
            task_id,
            part_number=part_number,
            chunks=request.stream(),
            claimed_sha256=part_sha256,
        )
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc


@upload_router.post("/{task_id}/complete", response_model=UploadCompleteResponse)
async def complete_upload_direct(
    task_id: UUID,
    payload: UploadCompleteRequest,
    upload_token: str = Header(alias="X-Upload-Token", min_length=8, max_length=256),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
    relay_service: LogCollectRelayService = Depends(get_relay_service),
):
    """数据面收尾：bridge 用上传令牌合并分片，并自动触发代传。

    为什么必须由 bridge 收尾：交付作业化后，前端可能早已关闭页面；
    「合并 + 代传」不能再是整条链里唯一依赖浏览器的一步。

    鉴权：令牌在 init 时已绑定 task_id（见 verify_token），因此不需要客户身份，
    也不能跨任务使用——这比带客户 Cookie 更安全，且能在浏览器缺席时工作。
    """
    if not upload_service.verify_token(str(task_id), upload_token):
        raise HTTPException(
            status_code=401,
            detail=build_problem("LC-TRANSFER-004", "上传 token 无效或已过期"),
        )
    try:
        task = await service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _problem(TaskNotFoundError.code, "Task not found") from exc
    try:
        result = await upload_service.complete(
            task,
            parts=payload.parts,
            declared_sha256=payload.sha256,
            declared_size=payload.size,
        )
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc

    # 合并成功即启动代传：trigger 内部幂等（只有把 relay_status 从非 running 改成
    # running 的调用者才真正执行），因此重复 complete 不会重复代传。
    # 前置条件：complete 内部必须已把 archive_* **提交**（不能只 flush）——
    # trigger 是另一个 session 对同一行的 UPDATE，本事务若仍持行锁，
    # 两者互等成死锁，30s 后被 idle_in_transaction_session_timeout 强杀，
    # 表现为"元数据回滚 + relay 读到空 archive_path"（Q2026101500828）。
    started = await relay_service.trigger(task_id)
    if started:
        _spawn_relay(relay_service, task_id)
    return result
