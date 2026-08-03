"""KBD 轻治理 revision 服务。

只负责 Proposal/Expert 快照的规范化、幂等创建和 head 指针维护。运行时发布仍由
``DynamicResourcePublisher`` 负责，避免把审核工作版本与 Agent active 混在一起。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kbd_entry import KbdEntry
from app.models.kbd_revision import KbdRevision

RevisionType = Literal["proposal", "expert"]
ActorType = Literal["llm", "expert", "migration", "system"]

KBD_PAYLOAD_FIELDS = (
    "support_id",
    "title",
    "problem_description",
    "alert_info",
    "steps_text",
    "root_cause",
    "solution",
    "operational_impact",
    "is_temporary",
    "recommendations",
    "signals_json",
    "images_json",
    "content_md",
    "content_raw",
    "category_id",
    "ai_category_id",
    "ai_category_conf",
    "ai_category_reason",
)


def build_kbd_revision_payload(kbd: KbdEntry) -> dict[str, Any]:
    """从当前兼容主记录构建稳定、可 Diff 的知识 payload。"""

    payload = {field: getattr(kbd, field) for field in KBD_PAYLOAD_FIELDS}
    payload["metadata"] = kbd.entry_metadata or {}
    payload["payload_schema_version"] = 1
    return payload


def payload_checksum(payload: dict[str, Any]) -> str:
    """计算与键顺序、中文转义无关的稳定 SHA-256。"""

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_pointer_part(value: Any) -> str:
    """按 RFC 6901 转义路径片段，供 Diff 稳定定位字段。"""

    return str(value).replace("~", "~0").replace("/", "~1")


def _stable_list_map(value: list[Any]) -> dict[str, Any] | None:
    """当列表元素具有稳定 id/seq 时转为映射；展示下标不作为跨版本身份。"""

    if not value:
        return {}
    result: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict):
            return None
        stable_id = item.get("id")
        if stable_id is None:
            stable_id = item.get("seq")
        if stable_id is None:
            return None
        key = str(stable_id)
        if key in result:
            return None
        result[key] = item
    return result


def diff_revision_payloads(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """计算可用于评估/训练筛选的结构化知识 Diff。

    dict 按字段递归；带稳定 ``id``/``seq`` 的列表按业务身份递归；其他列表整体
    replace，避免把会随排序变化的数组下标误当成跨 revision 身份。
    """

    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}/{_json_pointer_part(key)}"
            if key not in before:
                changes.append({"operation": "add", "path": child_path, "before": None, "after": after[key]})
            elif key not in after:
                changes.append(
                    {"operation": "delete", "path": child_path, "before": before[key], "after": None}
                )
            else:
                changes.extend(diff_revision_payloads(before[key], after[key], child_path))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        before_map = _stable_list_map(before)
        after_map = _stable_list_map(after)
        if before_map is not None and after_map is not None:
            return diff_revision_payloads(before_map, after_map, path)
    return [{"operation": "replace", "path": path or "/", "before": before, "after": after}]


def summarize_expert_signal_changes(
    proposal_payload: Any,
    expert_payload: Any | None,
    *,
    proposal_revision_id: int | None,
    expert_revision_id: int | None,
) -> dict[str, Any]:
    """返回面向审核 UI 的专家关键信号修改摘要。

    ``diff_revision_payloads`` 是包含 Prompt 指纹、正文和任意字段的通用审计
    Diff，不能直接解释为“专家修改”。本摘要只比较 AI Proposal 与当前专家稿中
    ``signals_json.signals`` 的稳定 signal id，确保一次重抽的 AI→AI 变化和运行时
    元数据永远不会计入专家修改数。
    """

    empty = {
        "status": "no_expert_draft",
        "proposal_revision_id": proposal_revision_id,
        "expert_revision_id": expert_revision_id,
        "changed_signal_count": 0,
        "added_signal_ids": [],
        "removed_signal_ids": [],
        "modified_signal_ids": [],
    }
    if not isinstance(expert_payload, dict):
        return empty

    def signal_map(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        signals_json = payload.get("signals_json")
        signals = signals_json.get("signals") if isinstance(signals_json, dict) else None
        if not isinstance(signals, list):
            return {}
        return {
            str(signal["id"]): signal
            for signal in signals
            if isinstance(signal, dict) and signal.get("id") is not None
        }

    proposal_signals = signal_map(proposal_payload)
    expert_signals = signal_map(expert_payload)
    added = sorted(set(expert_signals) - set(proposal_signals))
    removed = sorted(set(proposal_signals) - set(expert_signals))
    modified = sorted(
        signal_id
        for signal_id in set(proposal_signals) & set(expert_signals)
        if proposal_signals[signal_id] != expert_signals[signal_id]
    )
    changed = len(added) + len(removed) + len(modified)
    return {
        "status": "modified" if changed else "unchanged",
        "proposal_revision_id": proposal_revision_id,
        "expert_revision_id": expert_revision_id,
        "changed_signal_count": changed,
        "added_signal_ids": added,
        "removed_signal_ids": removed,
        "modified_signal_ids": modified,
    }


def resolve_proposal_baseline(
    revision: KbdRevision | None,
    revisions_by_id: dict[int, KbdRevision],
) -> KbdRevision | None:
    """解析 Expert 明确绑定的 Proposal，并兼容迁移前的 parent 链。

    新版本以 ``baseline_proposal_revision_id`` 为权威；历史版本尚未回填或测试夹具
    缺少该属性时，才沿不可变 parent 链回溯。不得使用 history 第一项、最后一项或
    当前 latest proposal 猜测，否则重抽后会把 AI→AI 变化错误归到专家名下。
    """

    if revision is None:
        return None
    baseline_id = getattr(revision, "baseline_proposal_revision_id", None)
    if baseline_id is not None:
        baseline = revisions_by_id.get(int(baseline_id))
        if baseline is not None and baseline.revision_type == "proposal":
            return baseline

    current = revision
    visited: set[int] = set()
    while current is not None and current.id not in visited:
        visited.add(current.id)
        if current.revision_type == "proposal":
            return current
        if current.parent_revision_id is None:
            break
        current = revisions_by_id.get(int(current.parent_revision_id))
    return None


def is_evaluation_candidate(revision: KbdRevision) -> bool:
    """仅把已完成审核/发布的 Expert 快照作为默认评估候选。

    working/saved/opened 是可追溯的编辑血缘，不是稳定目标答案；保留它们不等于允许
    直接进入训练或评估集。兼容旧数据时允许由稳定的发布 origin 判定 approved。
    """

    if revision.revision_type != "expert":
        return False
    review_state = str((revision.review_metadata or {}).get("review_state") or "")
    origin = str((revision.generation_metadata or {}).get("origin") or "")
    return review_state == "approved" or origin in {"admin_review", "admin_maintenance_publish"}


def select_current_expert_pair(
    revisions: list[KbdRevision],
    *,
    working_revision_id: int | None,
    latest_proposal_revision_id: int | None,
) -> tuple[KbdRevision | None, KbdRevision | None]:
    """选择页面当前 Expert 及其 Proposal，不让旧审核稿跨重抽冒充当前稿。

    有 working head 时它是当前编辑态；发布后 working 会被清空，此时选择仍绑定当前
    latest Proposal 的最新 approved/published Expert。若已经重抽出新 Proposal，而历史
    Expert 只审核过旧基线，则返回空 Expert，页面显示 0。
    """

    revisions_by_id = {int(item.id): item for item in revisions}
    if working_revision_id is not None:
        working = revisions_by_id.get(int(working_revision_id))
        if working is not None and working.revision_type == "expert":
            return working, resolve_proposal_baseline(working, revisions_by_id)

    for expert in sorted(revisions, key=lambda item: item.revision_no, reverse=True):
        if not is_evaluation_candidate(expert):
            continue
        baseline = resolve_proposal_baseline(expert, revisions_by_id)
        if (
            baseline is not None
            and latest_proposal_revision_id is not None
            and int(baseline.id) == int(latest_proposal_revision_id)
        ):
            return expert, baseline
    return None, None


async def _resolve_baseline_proposal_id(
    session: AsyncSession,
    *,
    kbd: KbdEntry,
    parent_revision_id: int | None,
) -> int | None:
    """在创建 Expert 快照时冻结其 Proposal 基线，避免后续重抽改变配对。"""

    current_id = parent_revision_id
    visited: set[int] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        current = await session.get(KbdRevision, current_id)
        if current is None:
            break
        if current.revision_type == "proposal":
            return int(current.id)
        baseline_id = getattr(current, "baseline_proposal_revision_id", None)
        if baseline_id is not None:
            return int(baseline_id)
        current_id = current.parent_revision_id
    return int(kbd.latest_proposal_revision_id) if kbd.latest_proposal_revision_id is not None else None


async def ensure_kbd_revision(
    session: AsyncSession,
    *,
    kbd: KbdEntry,
    revision_type: RevisionType,
    actor_type: ActorType,
    actor_id: str | None = None,
    parent_revision_id: int | None = None,
    baseline_proposal_revision_id: int | None = None,
    generation_metadata: dict[str, Any] | None = None,
    validation_summary: dict[str, Any] | None = None,
    review_metadata: dict[str, Any] | None = None,
    trace_id: str | None = None,
    reuse_existing: bool = True,
) -> KbdRevision:
    """幂等创建不可变 revision，并更新对应 head 指针。

    默认在同一 KBD/type/payload checksum 已存在时复用旧行，避免自动保存制造无意义版本。
    正式批准必须传 ``reuse_existing=False``，即使 payload 未变也冻结独立批准快照，
    从而保留审核备注和通过门禁的事实。
    调用方应在同一事务内持有 ``kbd_entry`` 行锁；本函数额外使用 advisory lock，兼容
    Pipeline/审核 API 从不同入口并发创建 revision。
    """

    payload = build_kbd_revision_payload(kbd)
    return await ensure_kbd_revision_payload(
        session,
        kbd=kbd,
        payload=payload,
        revision_type=revision_type,
        actor_type=actor_type,
        actor_id=actor_id,
        parent_revision_id=parent_revision_id,
        baseline_proposal_revision_id=baseline_proposal_revision_id,
        generation_metadata=generation_metadata,
        validation_summary=validation_summary,
        review_metadata=review_metadata,
        trace_id=trace_id,
        reuse_existing=reuse_existing,
    )


async def ensure_kbd_revision_payload(
    session: AsyncSession,
    *,
    kbd: KbdEntry,
    payload: dict[str, Any],
    revision_type: RevisionType,
    actor_type: ActorType,
    actor_id: str | None = None,
    parent_revision_id: int | None = None,
    baseline_proposal_revision_id: int | None = None,
    generation_metadata: dict[str, Any] | None = None,
    validation_summary: dict[str, Any] | None = None,
    review_metadata: dict[str, Any] | None = None,
    trace_id: str | None = None,
    reuse_existing: bool = True,
) -> KbdRevision:
    """基于显式 payload 创建 revision。

    已发布维护时 ``kbd_entry`` 必须继续代表当前生效内容，因此工作稿不能先覆盖主记录；
    该入口允许直接冻结独立 payload，并沿用同一套 checksum、head 和历史规则。
    """

    await session.execute(select(func.pg_advisory_xact_lock(kbd.id)))
    checksum = payload_checksum(payload)
    if revision_type == "expert" and baseline_proposal_revision_id is None:
        baseline_proposal_revision_id = await _resolve_baseline_proposal_id(
            session,
            kbd=kbd,
            parent_revision_id=parent_revision_id,
        )

    revision = None
    if reuse_existing:
        reuse_filters = [
            KbdRevision.kbd_entry_id == kbd.id,
            KbdRevision.revision_type == revision_type,
            KbdRevision.checksum == checksum,
        ]
        if revision_type == "expert":
            # 相同正文基于不同 Proposal 仍是不同监督样本，不能跨基线复用。
            reuse_filters.append(
                KbdRevision.baseline_proposal_revision_id == baseline_proposal_revision_id
            )
        existing_result = await session.execute(
            select(KbdRevision)
            .where(*reuse_filters)
            .order_by(KbdRevision.revision_no.desc())
            .limit(1)
        )
        revision = existing_result.scalar_one_or_none()
    if revision is None:
        next_no_result = await session.execute(
            select(func.coalesce(func.max(KbdRevision.revision_no), 0) + 1).where(
                KbdRevision.kbd_entry_id == kbd.id
            )
        )
        revision = KbdRevision(
            kbd_entry_id=kbd.id,
            revision_no=int(next_no_result.scalar_one()),
            revision_type=revision_type,
            parent_revision_id=parent_revision_id,
            baseline_proposal_revision_id=baseline_proposal_revision_id,
            payload_json=payload,
            checksum=checksum,
            generation_metadata=generation_metadata or {},
            validation_summary=validation_summary or {},
            review_metadata=review_metadata or {},
            actor_id=actor_id,
            actor_type=actor_type,
            trace_id=trace_id,
        )
        session.add(revision)
        await session.flush()

    if revision_type == "proposal":
        kbd.latest_proposal_revision_id = revision.id
    else:
        kbd.working_revision_id = revision.id
    await session.flush()
    return revision


def apply_kbd_revision_payload(kbd: KbdEntry, payload: dict[str, Any]) -> None:
    """将已通过校验的 revision payload 应用到兼容主记录。"""

    for field in KBD_PAYLOAD_FIELDS:
        if field in payload:
            setattr(kbd, field, payload[field])
    kbd.entry_metadata = payload.get("metadata") or {}


def revision_metadata(revision: KbdRevision | None) -> dict[str, Any] | None:
    """生成前端需要的轻量 revision 元数据，不重复返回大 payload。"""

    if revision is None:
        return None
    return {
        "id": revision.id,
        "revision_no": revision.revision_no,
        "revision_type": revision.revision_type,
        "parent_revision_id": revision.parent_revision_id,
        "baseline_proposal_revision_id": getattr(revision, "baseline_proposal_revision_id", None),
        "checksum": revision.checksum,
        "actor_id": revision.actor_id,
        "actor_type": revision.actor_type,
        "validation_summary": revision.validation_summary or {},
        "review_metadata": getattr(revision, "review_metadata", None) or {},
        "created_at": revision.created_at.isoformat() if revision.created_at else None,
    }
