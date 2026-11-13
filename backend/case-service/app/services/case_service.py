"""
Case Service - 业务逻辑层
"""

import os
from datetime import UTC, datetime

from shared.models.schemas import (
    CaseCreate,
    CaseListResponse,
    CaseResponse,
    CaseStatsResponse,
    CaseUpdate,
    ClientInfo,
    ClientListResponse,
)
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from shared.security.case_access import resolve_data_access
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..models.case import Case, CaseStatus
from ..repositories.case_repo import CaseRepository

logger = get_logger("case-service")

# 日志收集入口自建的壳工单来源标记（写进 case.metadata.source）
_SHELL_CASE_SOURCE = "log_collect"
# 交付在途状态：搬运中转移归属会丢分片，认领必须等它结束
_ACTIVE_DELIVERY_STATUSES = frozenset({"pulling", "uploading", "relaying"})
# 原身份最后活动距今小于该秒数时禁止认领，避免两个浏览器互相踢（默认 10 分钟）
_CLAIM_IDLE_SECONDS = int(os.getenv("LOG_COLLECT_CLAIM_IDLE_SECONDS", "600"))

# 分类关键词映射表（用于自动推断工单分类）
_CATEGORY_KEYWORDS = {
    "vm": ["虚拟机", "VM", "vm", "开机", "关机", "迁移", "快照", "虚机"],
    "storage": ["存储", "磁盘", "卷", "NFS", "iSCSI", "ASAN", "硬盘"],
    "network": ["网络", "IP", "VLAN", "连通", "丢包", "网口", "网卡", "vxlan"],
    "cluster": ["集群", "节点", "宿主机", "host", "主机", "集群管理"],
    "backup": ["备份", "恢复", "快照", "克隆", "容灾"],
    "hardware": ["硬件", "IPMI", "风扇", "电源", "温度", "磁盘灯"],
}


def _infer_category(title: str, description: str | None) -> str | None:
    """
    基于标题和描述关键词推断工单分类。

    Args:
        title: 工单标题
        description: 工单描述（可选）

    Returns:
        推断的分类字符串，无法匹配时返回 None
    """
    text = (title or "") + " " + (description or "")
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None


class CaseService:
    """工单业务服务"""

    def __init__(self, repository: CaseRepository):
        self.repository = repository

    async def create_case(self, case_create: CaseCreate, *, metadata: dict | None = None) -> CaseResponse:
        """创建新工单。

        ``case_id`` 可显式指定（格式由 CaseCreate 校验）；不传则由
        ``generate_case_id()`` 生成（Q + 当天日期 + 当天自增序号）。
        ``metadata`` 用于标注建单来源（如日志收集入口 source=log_collect）。
        """
        case_id = case_create.case_id or await self.repository.generate_case_id()
        trace_id = get_current_trace_id()

        # 获取或创建用户
        from shared.models.user import User

        user = await self.repository.get_user_by_client_id(case_create.client_id)
        if not user:
            user = User(client_id=case_create.client_id, user_type="temporary", trace_id=trace_id)
            user = await self.repository.create_user(user)
            logger.info(f"Created new user for client_id: {case_create.client_id}")

        # 工单分类由 S0 意图识别流程自动写入（PR #88），此处 category 初始为 None
        # _infer_category 保留供后续兜底使用，暂不在创建工单时调用
        category = _infer_category(case_create.title, case_create.description)
        if category:
            logger.info(
                event="category_auto_inferred",
                case_id=case_id,
                inferred_category=category,
            )

        case = Case(
            case_id=case_id,
            user_id=user.user_id,
            client_id=case_create.client_id,
            title=case_create.title,
            description=case_create.description,
            status=CaseStatus.created,
            assistant_type=case_create.assistant_type or "htp-agent",
            category=category,
            trace_id=trace_id,
            # ORM 真实属性是 metadata_（metadata 是 SQLAlchemy 保留名，写 metadata= 只会
            # 生成实例影子属性，永远不落库），壳工单来源标记依赖这一列，勿改回。
            metadata_=metadata or {},
        )

        created_case = await self.repository.create(case)

        logger.info(
            event="case_created",
            message=f"Created case {case_id}",
            case_id=case_id,
            client_id=case_create.client_id,
            category=category,
        )

        return CaseResponse.model_validate(created_case)

    async def resolve_case_for_log_collect(self, case_id: str, client_id: str) -> tuple[str, bool, bool]:
        """日志收集专用：按工单号解析，不存在则用该号建档。

        语义（方案 A，允许跨客户端复用）：
        - 已存在：**不校验归属**，直接复用；返回 ``created=False``，并用 ``owned`` 标记
          该工单是否属于当前 ``client_id``；
        - 不存在：用该号创建一张仅承载日志任务的工单，返回 ``created=True``、``owned=True``；
        - 并发：两个请求同时 resolve 同一号时，主键冲突的一方视为"已存在"。

        为什么需要 ``owned``：日志与终端录像的写入都要求 ``case.client_id == 当前身份``，
        跨身份复用的工单号可以使采集本身成功，但回采/回放必然被归属校验拒绝。
        把归属信号交给调用方，才能在不破坏"用外部工单号补采"的前提下提前提示用户。

        注意：已存在时不返回工单内容（可能属于其他客户端的聊天工单），
        调用方只应拿到 ``case_id``、是否新建与归属标记。
        """
        existing = await self.repository.get_by_id(case_id)
        if existing is not None:
            return existing.case_id, False, existing.client_id == client_id

        try:
            await self.create_case(
                CaseCreate(
                    client_id=client_id,
                    title=f"日志收集任务 {case_id}",
                    description="由日志收集入口创建，仅承载日志采集任务",
                    assistant_type="htp-agent",
                    case_id=case_id,
                ),
                metadata={"source": "log_collect"},
            )
        except IntegrityError:
            # 并发窗口内另一方已创建同号工单：回滚后按"已存在"处理
            await self.repository.session.rollback()
            logger.info(event="case_resolve_race", case_id=case_id, client_id=client_id)
            # 对方建的同号工单可能属于其他客户端，重新确认归属而非默认本人
            raced = await self.repository.get_by_id(case_id)
            return case_id, False, raced is not None and raced.client_id == client_id
        return case_id, True, True

    async def _case_task_activity(self, case_id: str) -> list[dict]:
        """该工单下所有采集任务的归属与交付状态（用于认领门禁）。"""
        result = await self.repository.session.execute(
            text(
                "SELECT created_by, status, delivery_status, updated_at"
                " FROM log_collect_task WHERE case_id = :cid"
            ),
            {"cid": case_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def describe_case_access(self, case_id: str, client_id: str) -> dict:
        """判定当前身份对该工单数据的访问资格，并给出"能否认领"的结论。

        返回字段：
        - ``owned``：工单归属当前身份（BOLA 防护的既有口径）；
        - ``shell_case``：壳工单（可认领的必要条件之一）；
        - ``claimable``：当前是否允许认领；
        - ``blocked_reason``：不可认领的原因，供前端展示而不是静默失败；
        - ``owner_active_seconds``：原身份最后一次任务活动距今秒数。
        """
        # 归属与"壳工单 + 本人任务"的判定复用 shared 单一判定点，
# 保证 case-service / conversation-service / gateway 三处口径一致。
        access = await resolve_data_access(self.repository.session, case_id, client_id)
        if not access["exists"]:
            return {
                "case_id": case_id,
                "owned": False,
                "shell_case": False,
                "claimable": False,
                "blocked_reason": "case_not_found",
                "owner_active_seconds": None,
                "task_granted": False,
                "data_access": False,
            }

        if access["owned"]:
            return {
                "case_id": case_id,
                "owned": True,
                "shell_case": access["shell_case"],
                "claimable": False,
                "blocked_reason": "already_owned",
                "owner_active_seconds": 0,
                "task_granted": False,
                "data_access": True,
            }

        shell_case = access["shell_case"]
        tasks = await self._case_task_activity(case_id) if shell_case else []
        idle_seconds: int | None = None
        if tasks:
            latest = max((row["updated_at"] for row in tasks if row["updated_at"]), default=None)
            if latest is not None:
                idle_seconds = max(0, int((datetime.now(UTC) - latest).total_seconds()))

        if not shell_case:
            blocked_reason = "not_shell_case"
        elif any(row["delivery_status"] in _ACTIVE_DELIVERY_STATUSES for row in tasks):
            # 有交付正在进行时转移归属会让搬运中的分片丢失归属，必须先结束交付
            blocked_reason = "delivery_in_progress"
        elif idle_seconds is not None and idle_seconds < _CLAIM_IDLE_SECONDS:
            # 原身份可能仍在同一个浏览器里操作，直接转移会互相踢
            blocked_reason = "owner_active"
        else:
            blocked_reason = None

        return {
            "case_id": case_id,
            "owned": False,
            "shell_case": shell_case,
            "claimable": blocked_reason is None,
            "blocked_reason": blocked_reason,
            "owner_active_seconds": idle_seconds,
            "task_granted": access["task_granted"],
            "data_access": access["data_access"],
        }

    async def claim_shell_case(self, case_id: str, client_id: str) -> dict:
        """把壳工单（及其名下采集任务）认领到当前身份。

        为什么必须显式认领而不是自动接管：``client_id`` 是浏览器会话身份而非人，
        自动接管等于"知道工单号就能成为所有者"，既无审计也绕过 BOLA。认领是用户
        明确表达的意图，平台据此改归属并留痕。
        """
        access = await self.describe_case_access(case_id, client_id)
        if access["owned"]:
            return {**access, "claimed": False, "already_owned": True, "tasks_moved": 0}
        if not access["claimable"]:
            return {**access, "claimed": False, "already_owned": False, "tasks_moved": 0}

        case = await self.repository.get_by_id(case_id)
        if case is None:  # describe_case_access 已保证存在，仅防御并发删除
            raise LookupError(case_id)

        from shared.models.user import User

        trace_id = get_current_trace_id()
        user = await self.repository.get_user_by_client_id(client_id)
        if not user:
            user = User(client_id=client_id, user_type="temporary", trace_id=trace_id)
            user = await self.repository.create_user(user)

        previous_client_id = case.client_id
        case.client_id = client_id
        case.user_id = user.user_id
        metadata = dict(case.metadata_ or {})
        claims = list(metadata.get("claims") or [])
        claimed_at = datetime.now(UTC)
        claims.append(
            {
                "from": previous_client_id,
                "to": client_id,
                "at": claimed_at.isoformat(),
                "trace_id": trace_id,
            }
        )
        metadata["claims"] = claims[-5:]
        case.metadata_ = metadata

        # 产物与录像按任务维度授权，任务归属必须一起转移，否则认领完仍旧看不到旧包
        moved = await self.repository.session.execute(
            text(
                "UPDATE log_collect_task"
                " SET created_by = :new_client,"
                "     detail = coalesce(detail, '{}'::jsonb) || jsonb_build_object("
                "       'claim', jsonb_build_object('from', :old_client, 'to', :new_client,"
                "                                    'at', :claimed_at, 'trace_id', :trace_id))"
                " WHERE case_id = :case_id AND created_by = :old_client"
            ),
            {
                "new_client": client_id,
                "old_client": previous_client_id,
                "claimed_at": claimed_at.isoformat(),
                "trace_id": trace_id,
                "case_id": case_id,
            },
        )
        await self.repository.session.commit()

        logger.info(
            event="log_collect_case_claimed",
            message=f"Case {case_id} claimed by {client_id}",
            case_id=case_id,
            previous_client_id=previous_client_id,
            client_id=client_id,
            tasks_moved=moved.rowcount,
            trace_id=trace_id,
        )
        return {
            **access,
            "owned": True,
            "claimable": False,
            "blocked_reason": "already_owned",
            "task_granted": False,
            "data_access": True,
            "claimed": True,
            "already_owned": False,
            "tasks_moved": moved.rowcount,
        }

    async def get_case(self, case_id: str) -> CaseResponse | None:
        """获取工单详情"""
        case = await self.repository.get_by_id(case_id)
        if not case:
            return None
        return CaseResponse.model_validate(case)

    async def list_cases(self, client_id: str, limit: int = 50, offset: int = 0) -> list[CaseResponse]:
        """获取客户端的所有工单（支持分页）"""
        cases = await self.repository.get_by_client_id(client_id, limit=limit, offset=offset)
        return [CaseResponse.model_validate(case) for case in cases]

    async def confirm_case(self, case_id: str) -> CaseResponse | None:
        """确认工单"""
        case = await self.repository.update_status(case_id, CaseStatus.confirmed)
        if not case:
            return None

        logger.info(event="case_confirmed", message=f"Confirmed case {case_id}", case_id=case_id)

        return CaseResponse.model_validate(case)

    async def close_case(self, case_id: str) -> CaseResponse | None:
        """关闭工单"""
        case = await self.repository.update_status(case_id, CaseStatus.closed)
        if not case:
            return None

        logger.info(event="case_closed", message=f"Closed case {case_id}", case_id=case_id)

        return CaseResponse.model_validate(case)

    # ============ Admin API ============

    async def list_all_cases(
        self,
        skip: int = 0,
        limit: int = 20,
        status: str | None = None,
        client_id: str | None = None,
        case_id: str | None = None,
        title: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> CaseListResponse:
        """获取所有工单（Admin: 分页 + 筛选）"""
        items, total = await self.repository.get_all(
            skip=skip,
            limit=limit,
            status=status,
            client_id=client_id,
            case_id=case_id,
            title=title,
            start_time=start_time,
            end_time=end_time,
        )
        return CaseListResponse(
            items=[CaseResponse.model_validate(c) for c in items],
            total=total,
            skip=skip,
            limit=limit,
        )

    async def update_case(
        self,
        case_id: str,
        case_update: CaseUpdate,
    ) -> CaseResponse | None:
        """更新工单信息（Admin）"""
        updates = case_update.model_dump(exclude_unset=True)
        if not updates:
            return await self.get_case(case_id)

        updated_case = await self.repository.update(case_id, updates)
        if not updated_case:
            return None

        logger.info(
            event="case_updated_by_admin",
            message=f"Admin updated case {case_id}",
            case_id=case_id,
            updates=list(updates.keys()),
        )
        return CaseResponse.model_validate(updated_case)

    async def get_case_stats(self) -> CaseStatsResponse:
        """获取工单统计（Admin）"""
        by_status = await self.repository.count_by_status()
        total = sum(by_status.values())
        return CaseStatsResponse(total=total, by_status=by_status)

    async def escalate_to_human(
        self,
        case_id: str,
        close_reason: str = "s0_classification_failed",
    ) -> CaseResponse | None:
        """
        将工单直接跳转到 in_progress 状态，移交人工处理。

        适用于以下场景：
          - S0 意图识别彻底失败（close_reason="s0_classification_failed"）
          - S6 用户选 C 升级人工（close_reason="escalated"，由 handle_s6_resolution_choice 调用）

        与 confirm_case() 不同，此方法允许 created → in_progress 直接跳转，
        跳过 confirmed 中间态（S0 失败时 AI 未完成分类，不应写 confirmed）。

        Args:
            case_id: 工单ID
            close_reason: 关闭原因，默认 s0_classification_failed

        Returns:
            更新后的 CaseResponse，或 None（工单不存在）
        """
        case = await self.repository.update_status(
            case_id,
            CaseStatus.in_progress,
            close_reason=close_reason,
        )
        if not case:
            return None

        logger.info(
            event="case_escalated_to_human",
            message=f"工单 {case_id} 已移交人工（{close_reason}）",
            case_id=case_id,
            close_reason=close_reason,
        )
        return CaseResponse.model_validate(case)

    async def get_client_list(self) -> ClientListResponse:
        """获取客户端列表（Admin）"""
        rows = await self.repository.get_client_stats()
        items = [ClientInfo(**r) for r in rows]
        return ClientListResponse(items=items, total=len(items))
