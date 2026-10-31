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
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TICKET_DIR = re.compile(r"^Q\d{13}$")
_TRASH_DIR = re.compile(r"^Q\d{13}-\d{8}-\d{6}$")

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
) -> tuple[int, int]:
    """产物总量超过上限时淘汰，返回 (删除数, 释放字节)。

    排序规则：先按"有没有日志"（非日志载荷先删），再按同步时间最旧优先。
    只按最旧排序会让一个 65 GiB 的 dump 工单挤掉多个真正可用的日志工单。

    传了 store 走增量路径（对账后用 SQL 聚合）；没传则走纯扫描，
    单元测试与"DB 不可用"的降级场景都依赖后者。
    """
    if not root.is_dir():
        return 0, 0
    if store is not None:
        return _enforce_with_store(
            root, max_total_bytes, min_keep_days, now, active_ticket_ids, store, on_removed
        )
    return _enforce_by_scanning(root, max_total_bytes, min_keep_days, now, active_ticket_ids, on_removed)


def _enforce_with_store(
    root: Path,
    max_total_bytes: int,
    min_keep_days: int,
    now: datetime,
    active_ticket_ids: set[str] | frozenset[str],
    store: Any,
    on_removed: Callable[[str], None] | None,
) -> tuple[int, int]:
    """增量路径：先用目录 mtime 对账（只碰变化的工单），再按 SQL 结果淘汰。"""
    reconcile_artifacts(root, store, active_ticket_ids)

    total = store.artifact_total()
    if total <= max_total_bytes:
        return 0, 0

    cutoff = now.timestamp() - min_keep_days * 86400
    removed = 0
    freed = 0
    for row in store.eviction_order(cutoff):
        if total <= max_total_bytes:
            break
        ticket_id = str(row["ticket_id"])
        if ticket_id in active_ticket_ids:
            continue
        size = int(row["bytes"])
        entry = root / ticket_id
        try:
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
        total -= size
        removed += 1
        freed += size
        logger.warning(
            "容量淘汰 ticket=%s size=%.1fGiB 剩余总量=%.1fGiB 上限=%.1fGiB",
            ticket_id,
            size / 1024**3,
            total / 1024**3,
            max_total_bytes / 1024**3,
        )

    if total > max_total_bytes:
        logger.error(
            "容量淘汰后仍超限 total=%.1fGiB 上限=%.1fGiB 可用工单均未满 %d 天",
            total / 1024**3,
            max_total_bytes / 1024**3,
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
    if total <= max_total_bytes:
        return 0, 0

    removed = 0
    freed = 0
    for _, _, entry, size in sorted(candidates, key=lambda item: (item[0], item[1])):
        if total <= max_total_bytes:
            break
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            logger.error("容量淘汰删除失败 ticket=%s error=%s", entry.name, exc)
            continue
        if on_removed is not None:
            on_removed(entry.name)
        total -= size
        removed += 1
        freed += size
        logger.warning(
            "容量淘汰 ticket=%s size=%.1fGiB 剩余总量=%.1fGiB 上限=%.1fGiB",
            entry.name,
            size / 1024**3,
            total / 1024**3,
            max_total_bytes / 1024**3,
        )

    if total > max_total_bytes:
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


def _same_mtime(left: Any, right: float) -> bool:
    return isinstance(left, (int, float)) and abs(float(left) - right) < _MTIME_EPSILON


def cleanup_expired(
    root: Path,
    now: datetime,
    retention_days: int,
    active_ticket_ids: set[str] | frozenset[str] = frozenset(),
    on_removed: Callable[[str], None] | None = None,
) -> int:
    """删除超过保留期的已发布工单目录，返回删除数量。

    只处理符合工单号形态的直接子目录，以及服务生成的 `.trash` 旧版本目录。
    `.tmp` 和其他宿主机目录不会被触碰。正在同步的工单始终跳过，避免清理任务
    与发布流程互相踩踏。
    """
    cutoff = now.timestamp() - retention_days * 24 * 60 * 60
    removed = 0
    if not root.is_dir():
        return removed

    entries = [(entry, _TICKET_DIR) for entry in root.iterdir()]
    trash = root / ".trash"
    if trash.is_dir():
        entries.extend((entry, _TRASH_DIR) for entry in trash.iterdir())

    for entry, pattern in entries:
        if not entry.is_dir() or entry.is_symlink() or not pattern.fullmatch(entry.name):
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
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            logger.error("清理过期工单目录失败 ticket=%s error=%s", entry.name, exc)
            continue
        if on_removed is not None:
            on_removed(entry.name)
        removed += 1
        logger.info(
            "清理过期本地目录 path=%s age_days=%.1f",
            entry,
            (now.timestamp() - synced_at) / 86400,
        )
    return removed


def has_usable_logs(root: Path, max_depth: int = 2) -> bool:
    """产物里是否有可用于诊断的日志。

    判据对齐采集端约定：存在 `log_list.json` 或任意 `sp_*` 条目。
    允许下探 max_depth 层，因为实测产物会再套一层与工单号同名的目录
    （`Q2026102700323/Q2026102700323/1.rar`）；但只对工单号形态的目录下探，
    不进入 sp_* 子树——那是几十万文件。
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return False

    for entry in entries:
        name = entry.name.lower()
        if name == "log_list.json" or name.startswith("sp_"):
            return True

    if max_depth <= 0:
        return False
    for entry in entries:
        if entry.is_dir() and _TICKET_DIR.fullmatch(entry.name):
            if has_usable_logs(entry, max_depth - 1):
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
