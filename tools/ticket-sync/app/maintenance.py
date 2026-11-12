"""ticket-sync 的本地目录保留期与容量维护。

设计取舍（2026-10-31 重构，第二版）：
- 「同步完成时间」与「产物体积」在发布时就已经是确定事实，因此写进
  `<工单目录>/.sync-meta.json`，同时镜像到 SQLite 的 artifacts 表。
- 目录 mtime 不能当"同步时间"（会被 DSH 写报告、人工 touch 改写），
  但可以作为"自上次记账后目录有没有被写过"的信号——增量对账就靠它。
- os.walk + lstat 在冷缓存下极慢：实测 550 GiB / 44 万文件约 3.5 分钟，
  而这段曾是同步代码，会把整个事件循环钉死（同期 HTTP 请求全部排队）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .publisher import cleanup_stale_workspaces, make_removable, purge_trash
from .store import (
    CLEANUP_KIND_CAPACITY,
    CLEANUP_KIND_RECONCILE,
    CLEANUP_KIND_RETENTION,
    CLEANUP_KIND_STALE_TMP,
    CLEANUP_KIND_TRASH,
)

logger = logging.getLogger(__name__)

_TICKET_DIR = re.compile(r"^Q\d{13}$")

# 发布时写入的同步元数据。文件名以点开头，DSH 的文件选择器不会展示它。
SYNC_META_NAME = ".sync-meta.json"

# 容量淘汰优先级：数值越小越先删。
# 非日志载荷（只有转储、没有 log_list.json）留着没有任何诊断价值，
# 却往往是 60 GiB 量级的大块头，必须排在正常日志工单之前被淘汰。
_RANK_NO_LOGS = 0
_RANK_HAS_LOGS = 1

# mtime 比较的容差：不同文件系统的时间戳精度不同，1ms 足以区分"改过/没改过"。
_MTIME_EPSILON = 0.001

# completedAt 的"未来"判定容差：NTP 校时可能让本机时间小幅回拨，
# 不该因此把已经固化好的完成时间整批作废、退回会被污染的 mtime。
_FUTURE_TOLERANCE_SECONDS = 300


def read_sync_meta(ticket_dir: Path) -> dict[str, Any]:
    """读取同步元数据；不存在或损坏一律返回空 dict（调用方按旧逻辑回退）。"""
    try:
        raw = (ticket_dir / SYNC_META_NAME).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_sync_meta(ticket_dir: Path, **fields: Any) -> None:
    """合并写入同步元数据（原子替换，避免半截 JSON）。

    只覆盖传入的键，保留已有内容——发布后补写尺寸时不会冲掉 completedAt。
    """
    data = read_sync_meta(ticket_dir)
    data.update(fields)
    target = ticket_dir / SYNC_META_NAME
    tmp = target.with_name(f"{SYNC_META_NAME}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        # 元数据只影响清理精度，写失败不能连累同步结果。
        logger.warning("写同步元数据失败 path=%s error=%s", target, exc)
        tmp.unlink(missing_ok=True)


def enforce_capacity(
    root: Path,
    max_total_bytes: int,
    min_keep_days: int,
    now: datetime,
    active_ticket_ids: set[str] | frozenset[str] = frozenset(),
    store: Any | None = None,
    on_removed: Callable[[str], None] | None = None,
    *,
    evict_free_bytes: int | None = None,
    on_event: Callable[..., None] | None = None,
    reconcile: bool = True,
) -> tuple[int, int]:
    """产物总量或磁盘余量越过阈值时淘汰，返回 (删除数, 释放字节)。

    两个触发条件**取或**，任一满足就开始删：
    - `total > max_total_bytes`：已用量上限（代理指标，第三重保险）；
    - `free < evict_free_bytes`：磁盘余量低于软阈值（唯一真值，主控）。

    为什么必须有第二个条件：已用量是记账出来的数，记账会漂移（实测 DB 说
    1467 GiB、磁盘只有 889 GiB，41 行幽灵记账合计 577 GiB），而且 `.tmp` 里
    在途的归档与解压产物根本不在记账范围内。只有 `disk_usage().free`
    能回答"这台机器还写不写得下"。

    排序规则：先按"有没有日志"（非日志载荷先删），再按同步时间最旧优先。
    只按最旧排序会让一个 65 GiB 的 dump 工单挤掉多个真正可用的日志工单。

    传了 store 走增量路径（对账后用 SQL 聚合）；没传则走纯扫描，
    单元测试与"DB 不可用"的降级场景都依赖后者。
    """
    if not root.is_dir():
        return 0, 0
    if store is not None:
        return _enforce_with_store(
            root,
            max_total_bytes,
            min_keep_days,
            now,
            active_ticket_ids,
            store,
            on_removed,
            evict_free_bytes=evict_free_bytes,
            on_event=on_event,
            reconcile=reconcile,
        )
    return _enforce_by_scanning(
        root,
        max_total_bytes,
        min_keep_days,
        now,
        active_ticket_ids,
        on_removed,
        evict_free_bytes=evict_free_bytes,
        on_event=on_event,
    )


def _enforce_with_store(
    root: Path,
    max_total_bytes: int,
    min_keep_days: int,
    now: datetime,
    active_ticket_ids: set[str] | frozenset[str],
    store: Any,
    on_removed: Callable[[str], None] | None,
    *,
    evict_free_bytes: int | None = None,
    on_event: Callable[..., None] | None = None,
    reconcile: bool = True,
) -> tuple[int, int]:
    """增量路径：先对账（只碰变化的工单），再按 SQL 结果淘汰。

    `reconcile=False` 时跳过重量对账，由调用方负责已做轻量对账（`reconcile_missing`）。
    半分钟级的维护循环走这条路：体积重算在冷缓存下可达数分钟，不能每 30 秒做一次。
    """
    if reconcile:
        reconcile_artifacts(root, store, active_ticket_ids)

    total = store.artifact_total()
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = 0

    over_total = total > max_total_bytes
    over_free = evict_free_bytes is not None and free < evict_free_bytes
    if not over_total and not over_free:
        return 0, 0

    cutoff = now.timestamp() - min_keep_days * 86400
    removed = 0
    freed = 0
    for row in store.eviction_order(cutoff):
        # 两个条件必须**同时**满足才停：只满足其一说明还差一半。
        # 用 free + freed 而不是反复 statvfs：本轮释放的字节必然回到同一文件系统。
        if total <= max_total_bytes and (
            evict_free_bytes is None or free + freed >= evict_free_bytes
        ):
            break
        ticket_id = str(row["ticket_id"])
        if ticket_id in active_ticket_ids:
            continue
        size = int(row["bytes"])
        entry = root / ticket_id
        try:
            # 补权限再删：产物里可能存在 000 权限的条目，直接 rmtree 会中途失败。
            make_removable(entry)
            shutil.rmtree(entry)
        except FileNotFoundError:
            # 目录已不在：记账行是脏的，删掉即可，不算淘汰。
            store.delete_artifact(ticket_id)
            continue
        except OSError as exc:
            # 删不掉就保留记账行，否则总量会算少、这批数据永远轮不到再删。
            logger.error("容量淘汰删除失败 ticket=%s error=%s", ticket_id, exc)
            continue
        store.delete_artifact(ticket_id)
        if on_removed is not None:
            on_removed(ticket_id)
        if on_event is not None:
            on_event(
                ticket_id=ticket_id,
                bytes_=size,
                free_bytes=free + freed,
                detail=f"total={total} limit={max_total_bytes} free_trigger={evict_free_bytes}",
            )
        total -= size
        removed += 1
        freed += size
        logger.warning(
            "容量淘汰 ticket=%s size=%.1fGiB 剩余总量=%.1fGiB 上限=%.1fGiB 磁盘余量=%.1fGiB 软阈值=%.1fGiB",
            ticket_id,
            size / 1024**3,
            total / 1024**3,
            max_total_bytes / 1024**3,
            (free + freed) / 1024**3,
            (evict_free_bytes or 0) / 1024**3,
        )

    if over_total and total > max_total_bytes:
        logger.error(
            "容量淘汰后仍超限 total=%.1fGiB 上限=%.1fGiB 可用工单均未满 %d 天",
            total / 1024**3,
            max_total_bytes / 1024**3,
            min_keep_days,
        )
    if evict_free_bytes is not None and free + freed < evict_free_bytes:
        logger.error(
            "容量淘汰后磁盘余量仍低于软阈值 free=%.1fGiB 软阈值=%.1fGiB"
            "（可用工单均未满 %d 天，或非本服务占用过大）",
            (free + freed) / 1024**3,
            evict_free_bytes / 1024**3,
            min_keep_days,
        )
    return removed, freed


def _enforce_by_scanning(
    root: Path,
    max_total_bytes: int,
    min_keep_days: int,
    now: datetime,
    active_ticket_ids: set[str] | frozenset[str],
    on_removed: Callable[[str], None] | None,
    *,
    evict_free_bytes: int | None = None,
    on_event: Callable[..., None] | None = None,
) -> tuple[int, int]:
    candidates: list[tuple[int, float, Path, int]] = []
    # total 统计磁盘上真实占用（含活跃与受保护工单），否则会低估压力、
    # 把"删无可删"的告警吞掉。
    total = 0
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or _TICKET_DIR.fullmatch(entry.name) is None:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        size = _cached_bytes(entry)
        total += size
        if entry.name in active_ticket_ids:
            continue
        synced_at = _synced_at(entry, mtime)
        age_days = (now.timestamp() - synced_at) / 86400
        if age_days < min_keep_days:
            # 刚同步进来的工单不参与淘汰，宁可超限告警也不误删用户在用的数据。
            continue
        candidates.append((_payload_rank(entry), synced_at, entry, size))
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = 0
    over_total = total > max_total_bytes
    over_free = evict_free_bytes is not None and free < evict_free_bytes
    if not over_total and not over_free:
        return 0, 0

    removed = 0
    freed = 0
    for _, _, entry, size in sorted(candidates, key=lambda item: (item[0], item[1])):
        if total <= max_total_bytes and (
            evict_free_bytes is None or free + freed >= evict_free_bytes
        ):
            break
        try:
            make_removable(entry)
            shutil.rmtree(entry)
        except OSError as exc:
            logger.error("容量淘汰删除失败 ticket=%s error=%s", entry.name, exc)
            continue
        if on_removed is not None:
            on_removed(entry.name)
        if on_event is not None:
            on_event(
                ticket_id=entry.name,
                bytes_=size,
                free_bytes=free + freed,
                detail=f"total={total} limit={max_total_bytes} free_trigger={evict_free_bytes}",
            )
        total -= size
        removed += 1
        freed += size
        logger.warning(
            "容量淘汰 ticket=%s size=%.1fGiB 剩余总量=%.1fGiB 上限=%.1fGiB 磁盘余量=%.1fGiB",
            entry.name,
            size / 1024**3,
            total / 1024**3,
            max_total_bytes / 1024**3,
            (free + freed) / 1024**3,
        )

    if over_total and total > max_total_bytes:
        logger.error(
            "容量淘汰后仍超限 total=%.1fGiB 上限=%.1fGiB 可用工单均未满 %d 天",
            total / 1024**3,
            max_total_bytes / 1024**3,
            min_keep_days,
        )
    return removed, freed


def reconcile_artifacts(
    root: Path,
    store: Any,
    active_ticket_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[int, int, int]:
    """把磁盘现状与 artifacts 表对齐，返回 (新增, 更新, 删除) 条数。

    用目录 mtime 作为"自上次记账后有没有被写过"的信号：未变化且**各项判据
    都已固化**才跳过。跳过前仍要读一次 meta（几十字节的小文件，代价可忽略），
    因为"完成时间有没有固化"也是判据之一；真正昂贵的是扫体积，那部分照旧只在
    mtime 变化时才做。重新发布一定会改变 mtime，因此不会漏记。

    `active_ticket_ids` 里的工单正在同步，目录内容与 mtime 都是中途值：
    既不能拿它固化完成时间，也不能因为本轮"先跳过"而把记账行删掉。
    """
    try:
        known = store.artifact_map()
    except Exception as exc:  # noqa: BLE001 - 存储异常不能中断维护
        logger.warning("读取产物记账失败 error=%s", exc)
        return 0, 0, 0

    added = 0
    updated = 0
    seen: set[str] = set()
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or _TICKET_DIR.fullmatch(entry.name) is None:
            continue
        seen.add(entry.name)
        if entry.name in active_ticket_ids:
            # 必须留在 seen 里：本轮跳过不等于"目录已消失"，
            # 否则末尾的 known - seen 会把正在同步工单的记账行一并删掉。
            continue
        try:
            info = entry.stat()
        except OSError:
            continue
        row = known.get(entry.name)
        meta = read_sync_meta(entry)
        if (
            row is not None
            and _same_mtime(row.get("dir_mtime"), info.st_mtime)
            and row.get("has_logs") is not None
            and _valid_timestamp(meta.get("completedAt"))
        ):
            # 跳过条件必须覆盖"所有判据都已确定"，其中包含完成时间。
            # 漏掉这一项，存量目录会永远走不到下面的固化分支：它们的 mtime
            # 从上次对账起就没再变过，"未变化就跳过"把固化逻辑挡在门外，
            # 保留期依旧退化成会被外部写入污染的 mtime。实测第一版补丁
            # 部署后 37 个目录里 0 个被固化，`removed` 依然恒为 0。
            # has_logs 为空说明是上一版留下的半截记账，同样必须补判一次。
            continue

        # 历史目录的 meta 可能只有 bytes（上一轮补写尺寸时留下的），缺 hasLogs。
        # 缺了就现算一次并补齐，否则这批存量永远拿不到"非日志载荷优先淘汰"的待遇。
        extra: dict[str, Any] = {}
        size = meta.get("bytes")
        if not isinstance(size, int) or size < 0:
            size = _tree_bytes(entry)
            extra["bytes"] = size
        has_logs = meta.get("hasLogs")
        if not isinstance(has_logs, bool):
            has_logs = has_usable_logs(entry)
            extra["hasLogs"] = has_logs
        # 历史目录（本改造之前发布的）meta 里没有 completedAt，保留期只能回退
        # 目录 mtime；而 mtime 会被外部写入刷新（DSH 往产物里写报告、人工 touch），
        # 于是一份早已过期的产物每天都被算成"刚同步完"，永远不满足保留期。
        # 首次对账时把当时的 mtime 固化成显式事实，此后不再随目录写入漂移。
        if not _valid_timestamp(meta.get("completedAt")):
            extra["completedAt"] = _clamp_timestamp(info.st_mtime)
        if extra:
            write_sync_meta(entry, **extra)
            # 补写 meta 会刷新目录 mtime，必须还原，否则下次又判定为"变化"。
            try:
                os.utime(entry, (info.st_atime, info.st_mtime))
            except OSError:
                logger.warning("恢复目录 mtime 失败 path=%s", entry)

        files = meta.get("files")
        store.upsert_artifact(
            entry.name,
            size,
            int(files) if isinstance(files, int) else 0,
            has_logs if isinstance(has_logs, bool) else None,
            _synced_at(entry, info.st_mtime),
            info.st_mtime,
        )
        if row is None:
            added += 1
        else:
            updated += 1

    removed = 0
    for ticket_id in set(known) - seen:
        store.delete_artifact(ticket_id)
        removed += 1
    return added, updated, removed


def reconcile_missing(
    root: Path,
    store: Any,
    active_ticket_ids: set[str] | frozenset[str] = frozenset(),
) -> list[tuple[str, int]]:
    """删除"记账有行、磁盘无目录"的幽灵行，返回 [(工单号, 记账字节)]。

    每轮维护都跑：只对每个工单号做一次 `is_dir()`，n 是工单数（实测 100 量级），
    成本可以忽略。为什么必须每轮跑：容量淘汰的判据是 artifacts 汇总，而行会
    因为**程序外**的删除变成幽灵——2026-11-12 现场为了腾空间人工删了 41 个目录、
    合计 577 GiB，程序完全不知道，DB 总数虚高到 1467 GiB（磁盘只有 889 GiB）。
    若不清掉，下一轮淘汰会按虚高的总量**多删**真实产物，把"救磁盘"变成"删数据"。

    与 `reconcile_artifacts` 的分工：这里只判断存在性（cheap，可每轮跑），
    体积重算与 meta 补写仍由前者负责（expensive，低频跑）。
    """
    try:
        known = store.artifact_map()
    except Exception as exc:  # noqa: BLE001 - 存储异常不能中断维护
        logger.warning("读取产物记账失败 error=%s", exc)
        return []

    missing: list[tuple[str, int]] = []
    for ticket_id, row in known.items():
        if ticket_id in active_ticket_ids:
            continue
        if (root / ticket_id).is_dir():
            continue
        size = int(row.get("bytes") or 0)
        store.delete_artifact(ticket_id)
        missing.append((ticket_id, size))
    if missing:
        logger.warning(
            "记账对账：清理 %d 条目录已不存在的幽灵行 合计=%.1fGiB",
            len(missing),
            sum(size for _, size in missing) / 1024**3,
        )
    return missing


@dataclass
class CleanupReport:
    """一轮清理的结果快照。全部字段都会进日志与 /metrics，不做无声动作。"""

    retention_removed: int = 0
    retention_freed: int = 0
    capacity_removed: int = 0
    capacity_freed: int = 0
    orphan_rows: int = 0
    orphan_bytes: int = 0
    stale_tmp_removed: int = 0
    trash_purged: int = 0
    free_before: int = 0
    free_after: int = 0
    elapsed_ms: float = 0.0
    retention_days: int = 0
    evict_free_bytes: int = 0
    min_free_bytes: int = 0
    # 收尾仍不达标：超限或低于软阈值。必须显式暴露，否则"删了但没用"会被静默。
    over_limit_after: bool = False
    below_evict_after: bool = False
    below_waterline_after: bool = False

    @property
    def touched(self) -> bool:
        """本轮是否真的动了磁盘。False 时调用方可以省略日志噪音。"""
        return bool(
            self.retention_removed
            or self.capacity_removed
            or self.orphan_rows
            or self.stale_tmp_removed
            or self.trash_purged
        )


def run_cleanup_cycle(
    *,
    root: Path,
    tmp_root: Path,
    trash_dir: Path,
    now: datetime,
    retention_days: int,
    min_keep_days: int,
    max_total_bytes: int,
    evict_free_bytes: int,
    min_free_bytes: int,
    stale_tmp_seconds: float,
    active_ticket_ids: set[str] | frozenset[str],
    store: Any,
    on_removed: Callable[[str], None] | None = None,
) -> CleanupReport:
    """执行一轮完整清理，供每 30 秒的维护循环直接调用。

    顺序不是随意的，每一步都在为下一步准备前提：
    1. 幽灵行对账——先让"记账总量"回到与磁盘一致，否则后面的淘汰会误判；
    2. 保留期删除——无论磁盘余量如何，超过保留期的产物一律删（判据是同步时刻）；
    3. 容量/水位淘汰——只在超限或余量低于软阈值时动手，删到两个条件都满足；
    4. 回收站清空——历史遗留，无保留价值；
    5. `.tmp` 陈旧回收——失败残留是续传本钱，但超过 2 小时就不值得留。

    全部删除动作都通过 `on_event` 落库（`cleanup_events`），
    这是唯一会无声删除用户数据的路径，必须可回溯。
    """
    report = CleanupReport(
        retention_days=retention_days,
        evict_free_bytes=evict_free_bytes,
        min_free_bytes=min_free_bytes,
    )
    started = time.monotonic()
    try:
        report.free_before = shutil.disk_usage(root).free
    except OSError:
        report.free_before = 0

    def record(kind: str):
        """构造一个把动作写进 store 的回调；写失败不影响删除本身。"""

        def _cb(*, ticket_id: str | None, bytes_: int, free_bytes: int | None, detail: str) -> None:
            store.add_cleanup_event(
                kind,
                now.timestamp(),
                ticket_id=ticket_id,
                bytes_=bytes_,
                free_bytes=free_bytes,
                detail=detail,
            )

        return _cb

    # 1. 幽灵行
    missing = reconcile_missing(root, store, active_ticket_ids)
    if missing:
        report.orphan_rows = len(missing)
        report.orphan_bytes = sum(size for _, size in missing)
        record_reconcile = record(CLEANUP_KIND_RECONCILE)
        for ticket_id, size in missing:
            record_reconcile(
                ticket_id=ticket_id,
                bytes_=size,
                free_bytes=report.free_before,
                detail="记账行存在但目录已不存在（程序外删除）",
            )

    # 2. 回收站（先清，避免它被算进 3、4 步的释放量）
    if trash_dir.is_dir():
        report.trash_purged = purge_trash(trash_dir)
        if report.trash_purged:
            record(CLEANUP_KIND_TRASH)(
                ticket_id=None,
                bytes_=0,
                free_bytes=report.free_before,
                detail=f"purged={report.trash_purged}（回收站不再保留旧版本）",
            )

    # 3. 保留期
    record_retention = record(CLEANUP_KIND_RETENTION)
    before = report.free_before
    report.retention_removed = cleanup_expired(
        root,
        now,
        retention_days,
        active_ticket_ids,
        on_removed,
        on_event=record_retention,
    )
    try:
        report.retention_freed = max(0, shutil.disk_usage(root).free - before)
    except OSError:
        report.retention_freed = 0

    # 4. 容量 / 水位淘汰。跳过重量对账：第 1 步已保证记账与磁盘一致，
    #    而体积重算在冷缓存下可达数分钟，不能挂在 30 秒的循环上。
    record_capacity = record(CLEANUP_KIND_CAPACITY)
    report.capacity_removed, report.capacity_freed = enforce_capacity(
        root,
        max_total_bytes,
        min_keep_days,
        now,
        active_ticket_ids,
        store,
        on_removed,
        evict_free_bytes=evict_free_bytes,
        on_event=record_capacity,
        reconcile=False,
    )

    # 5. `.tmp` 陈旧残留
    if tmp_root.is_dir():
        report.stale_tmp_removed = cleanup_stale_workspaces(tmp_root, stale_tmp_seconds)
        if report.stale_tmp_removed:
            record(CLEANUP_KIND_STALE_TMP)(
                ticket_id=None,
                bytes_=0,
                free_bytes=report.free_before,
                detail=f"count={report.stale_tmp_removed} stale_seconds={stale_tmp_seconds:.0f}",
            )

    try:
        report.free_after = shutil.disk_usage(root).free
    except OSError:
        report.free_after = 0
    report.over_limit_after = store.artifact_total() > max_total_bytes
    report.below_evict_after = report.free_after < evict_free_bytes
    report.below_waterline_after = report.free_after < min_free_bytes
    report.elapsed_ms = (time.monotonic() - started) * 1000
    return report


def _known_bytes(ticket_dir: Path) -> int:
    """只读已固化的产物体积；缺失时返回 0，绝不为此触发全盘扫描。"""
    value = read_sync_meta(ticket_dir).get("bytes")
    return value if isinstance(value, int) and value >= 0 else 0


def _same_mtime(left: Any, right: float) -> bool:
    return isinstance(left, (int, float)) and abs(float(left) - right) < _MTIME_EPSILON


def cleanup_expired(
    root: Path,
    now: datetime,
    retention_days: int,
    active_ticket_ids: set[str] | frozenset[str] = frozenset(),
    on_removed: Callable[[str], None] | None = None,
    *,
    on_event: Callable[..., None] | None = None,
) -> int:
    """删除超过保留期的已发布工单目录，返回删除数量。

    只处理符合工单号形态的直接子目录。`.tmp` 和其他宿主机目录不会被触碰。
    正在同步的工单始终跳过，避免清理任务与发布流程互相踩踏。

    判据是 `.sync-meta.json` 里的 `completedAt`（同步完成时刻），**不是**工单号
    里的日期：客户可能把历史工单隔几天再上传，按工单号日期算会把它判成早已过期。
    """
    cutoff = now.timestamp() - retention_days * 24 * 60 * 60
    removed = 0
    if not root.is_dir():
        return removed

    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = 0

    # 回收站不再保留任何内容：旧版本产物一律删除。这里的清理是给"历史遗留"
    # 兜底的——升级前 rename 进 `.trash` 的目录既不在 artifacts 记账里，
    # 也没有任何保留价值（原件在日志源服务器上）。
    trash = root / ".trash"
    if trash.is_dir():
        purged = purge_trash(trash)
        if purged:
            removed += purged
            if on_event is not None:
                on_event(ticket_id=None, bytes_=0, free_bytes=free, detail=f"purged={purged}")

    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or not _TICKET_DIR.fullmatch(entry.name):
            continue
        if entry.name in active_ticket_ids:
            logger.info("保留正在同步的工单目录 ticket=%s", entry.name)
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError as exc:
            logger.warning("读取工单目录时间失败 path=%s error=%s", entry, exc)
            continue
        synced_at = _synced_at(entry, mtime)
        if synced_at >= cutoff:
            continue
        # 只读已固化的体积，不在删除前做全盘扫描：冷缓存下 44 万文件的目录
        # 统计要数分钟，而这次统计的唯一用途是给流水记一个数字。
        size = _known_bytes(entry)
        try:
            make_removable(entry)
            shutil.rmtree(entry)
        except OSError as exc:
            logger.error("清理过期工单目录失败 ticket=%s error=%s", entry.name, exc)
            continue
        if on_removed is not None:
            on_removed(entry.name)
        if on_event is not None:
            on_event(
                ticket_id=entry.name,
                bytes_=size,
                free_bytes=free,
                detail=f"age_days={(now.timestamp() - synced_at) / 86400:.2f}"
                f" retention_days={retention_days}",
            )
        removed += 1
        free += size
        logger.info(
            "清理过期本地目录 path=%s age_days=%.1f size=%.1fGiB",
            entry,
            (now.timestamp() - synced_at) / 86400,
            size / 1024**3,
        )
    return removed


# 探测日志时的目录访问预算。判据是"有没有 sf/log"，遍历深度与目录数都必须有上限：
# 产物动辄 5 万个文件、30 GiB，每日对账都会调用一次，不能变成全树扫描。
LOG_PROBE_MAX_DIRS = 200


def has_usable_logs(root: Path, max_depth: int = 3, budget: list[int] | None = None) -> bool:
    """产物里是否有可用于诊断的日志。

    三条判据，按代价从低到高：
    1. 存在 `log_list.json` 或任意 `sp_*` 条目——采集端约定的日志清单形态；
    2. 任意层级存在 `<节点目录>/sf/log/`——HCI 采集包的实际结构，实测有
       `Q.../vs-*/<IP>-<时间>/sf/log/...` 这类三层嵌套（Q2026102700874 就是）；
    3. 工单号同名目录继续下探（`Q.../Q.../1.rar`）。

    遍历只下探目录，且受 `LOG_PROBE_MAX_DIRS` 预算约束；命中 `sp_*` 立即返回，
    因此不会进入几十万文件的 sp_* 子树。
    """
    if budget is None:
        budget = [LOG_PROBE_MAX_DIRS]

    try:
        entries = list(root.iterdir())
    except OSError:
        return False

    for entry in entries:
        name = entry.name.lower()
        if name == "log_list.json" or name.startswith("sp_"):
            return True
        if entry.is_dir() and (entry / "sf" / "log").is_dir():
            return True

    if max_depth <= 0:
        return False
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        if has_usable_logs(entry, max_depth - 1, budget):
            return True
    return False


def _valid_timestamp(value: Any) -> bool:
    """completedAt 是否可信：必须是正数且不在未来。

    bool 是 int 的子类，`{"completedAt": true}` 会被当成 1（1970 年），
    让产物立刻满足保留期而被删掉；未来时间戳则相反，永远不满足。
    两种都必须按"字段不存在"处理。
    未来判定留 5 分钟容差：NTP 校时的小幅回拨不该让整批产物退回 mtime。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 < float(value) <= time.time() + _FUTURE_TOLERANCE_SECONDS


def _clamp_timestamp(value: float) -> float:
    """把待固化的时间戳夹进 (0, now]，避免写入非正数或未来时间。"""
    now = time.time()
    return now if value <= 0 else min(float(value), now)


def _synced_at(ticket_dir: Path, fallback_mtime: float) -> float:
    """产物何时同步完成：优先显式标记，回退目录 mtime。

    显式标记可能是历史脏数据（0、布尔、未来时间），一律按"不存在"回退。
    回退值只会让产物活得更久，不会导致误删，这个方向的降级是安全的。
    """
    value = read_sync_meta(ticket_dir).get("completedAt")
    if _valid_timestamp(value):
        return float(value)
    return fallback_mtime


def _payload_rank(ticket_dir: Path) -> int:
    """淘汰优先级。历史目录无 meta 时按"有日志"对待，避免误删可用产物。"""
    return _RANK_NO_LOGS if read_sync_meta(ticket_dir).get("hasLogs") is False else _RANK_HAS_LOGS


def _cached_bytes(ticket_dir: Path) -> int:
    """产物字节数：优先读 meta，缺失时扫一次并回填。

    历史目录（本次改造之前发布的）没有 meta，不补写的话每天容量检查都要
    重新走一遍全盘遍历。

    注意回填后必须把目录 mtime 恢复原值：新增 `.sync-meta.json` 会把目录
    mtime 刷成当前时间，而历史目录没有 completedAt、保留期仍回退用 mtime，
    刷掉就等于给它无限续命，永远清不掉。
    """
    value = read_sync_meta(ticket_dir).get("bytes")
    if isinstance(value, int) and value >= 0:
        return value
    size = _tree_bytes(ticket_dir)
    try:
        info = ticket_dir.stat()
    except OSError:
        info = None
    write_sync_meta(ticket_dir, bytes=size)
    if info is not None:
        try:
            os.utime(ticket_dir, (info.st_atime, info.st_mtime))
        except OSError:
            logger.warning("恢复目录 mtime 失败 path=%s", ticket_dir)
    return size


def _tree_bytes(root: Path) -> int:
    """统计目录真实占用字节数，不跟随符号链接。

    用 os.scandir 而非 os.walk + os.lstat：scandir 复用目录项里的类型与尺寸
    信息，44 万文件时省掉一次多余的系统调用。
    """
    total = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total
