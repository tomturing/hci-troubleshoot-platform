"""同一工单多次采集包的合并。

产品语义（2026-11-12 明确）：
- 设备上该工单目录必须是**所有采集内容的并集**，而不是"最后一次采集"；
- 同一个相对路径在多个包里出现时，以**最新**那份为准（覆盖旧内容）；
- 不重复的路径全部保留，最终 `/sf/tickets/<工单号>/` 是文件唯一的目录结构。

实现取舍：
- 不做"重新下载全部包再重建"：单包可达几十 GiB，新增一次采集就全量重拉不可接受。
  因此以已发布目录为基线（硬链接克隆，不复制字节），只拉取/解压尚未合并的新包。
- 硬链接克隆出的树里，已存在的文件与**当前已发布产物**共享 inode。合并时
  **只通过 rename/unlink 替换目录项**，绝不原地写文件内容——发布是最后一步
  rename，在此之前原地覆盖会直接改掉用户正在看的日志。
- 升级兼容：改造前发布的目录没有 `mergedArchives` 记录，无法判断历史包是否
  已合并。自动任务只合并"改造后新增"的包，历史包记为 baseline 不自动回填；
  手动同步才会回填 baseline（用户显式要求）。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fetcher import RemoteArchive, archive_sort_key, is_newer_archive

logger = logging.getLogger(__name__)

ORIGIN_AUTO = "auto"
ORIGIN_MANUAL = "manual"

# 元数据字段：真正合并进本地产物的包 / 升级时有意不回填的历史包。
MERGED_FIELD = "mergedArchives"
BASELINE_FIELD = "baselineArchives"

ArchiveKey = tuple[float, int]


def archive_key(archive: RemoteArchive) -> ArchiveKey:
    """包的身份：(mtime, size)。

    为什么不用文件名：5888 重传时会把旧规范名**改名**成带时间戳的名字（保留
    原 mtime），同一份字节换了个名字。按名字比会把它当成新包再拉一遍。
    mtime 由 rsync -t 原样保留，size 独立兜底，两者组合足以识别"同一版"。
    """
    return (archive.mtime, archive.size)


def _record_key(record: Any) -> ArchiveKey | None:
    if not isinstance(record, dict):
        return None
    mtime = record.get("mtime")
    size = record.get("size")
    if isinstance(mtime, bool) or not isinstance(mtime, int | float) or mtime <= 0:
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return (float(mtime), size)


def record_archive(archive: RemoteArchive) -> dict[str, Any]:
    """把远端包固化成 meta 里的一条记录（保留 path 便于排障）。"""
    return {"mtime": archive.mtime, "size": archive.size, "path": archive.path}


def _key_set(meta: dict[str, Any], field_name: str) -> set[ArchiveKey]:
    records = meta.get(field_name)
    if not isinstance(records, list):
        return set()
    keys: set[ArchiveKey] = set()
    for record in records:
        key = _record_key(record)
        if key is not None:
            keys.add(key)
    return keys


def merged_keys(meta: dict[str, Any]) -> set[ArchiveKey] | None:
    """已合并包的集合；目录是改造前发布的则返回 None（无法判断）。

    区分"空集合"与"字段缺失"：前者是 v2 真实状态（还没合并过任何包），
    后者是升级兼容分支，两者行为不同，不能用 `or set()` 抹平。
    """
    if not isinstance(meta.get(MERGED_FIELD), list):
        return None
    return _key_set(meta, MERGED_FIELD)


def baseline_keys(meta: dict[str, Any]) -> set[ArchiveKey]:
    """升级时有意跳过、但仍可在手动同步时回填的历史包。"""
    return _key_set(meta, BASELINE_FIELD)


def recorded_identity(ticket_id: str, meta: dict[str, Any]) -> RemoteArchive | None:
    """旧版 meta 里记录的"上次拉到的是哪一版"。

    只用于升级兼容：没有 merged 集合时，以它为界把远端包分成"历史 baseline"
    与"改造后新增"两段，避免升级首轮把几十个存量历史包全部重拉
    （2026-11-01 已有 57 个工单、766 GiB 的教训）。
    """
    mtime = meta.get("remoteMtime")
    size = meta.get("remoteSize")
    if isinstance(mtime, bool) or not isinstance(mtime, int | float) or mtime <= 0:
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    # path 只影响"同 mtime 时规范名优先"的平局判定；旧版记录的就是当时解析出的
    # 最新包，这里按规范名处理，与那套语义一致。
    canonical = f"/sf/log/{ticket_id}/{ticket_id}.zip"
    return RemoteArchive(ticket_id, canonical, size, float(mtime))


def plan_pending(
    ticket_id: str,
    archives: list[RemoteArchive],
    meta: dict[str, Any],
    origin: str,
    *,
    has_final: bool,
    backfill_legacy: bool = False,
) -> list[RemoteArchive]:
    """算出这次任务需要拉取合并的包，按"旧→新"升序返回。

    规则：
    - 本地还没有产物：全部包都合并（首次同步也要拿到全部采集内容）；
    - v2 meta（有 mergedArchives）：所有未合并的包都是 pending；自动任务额外
      跳过 baseline（升级前历史包），手动任务连 baseline 一起回填；
    - legacy meta：自动任务只合并"比上次记录更新"的包，并把其余包全部记为
      baseline；手动任务全量回填。运维显式打开 `backfill_legacy` 时，自动任务
      也按手动口径全量回填（盘量代价由运维在窗口期决策）。
    """
    ordered = sorted(archives, key=archive_sort_key)
    if not has_final:
        return ordered

    full_backfill = origin == ORIGIN_MANUAL or backfill_legacy
    merged = merged_keys(meta)
    if merged is not None:
        skip = merged if full_backfill else merged | baseline_keys(meta)
        pending = [archive for archive in ordered if archive_key(archive) not in skip]
        return pending

    # legacy：没有 merged 字段，无法知道历史包是否已合并。
    if full_backfill:
        return ordered

    recorded = recorded_identity(ticket_id, meta)
    if recorded is None:
        # 连"上次是哪一版"都没有：只合并最新一份，行为与改造前一致。
        return ordered[-1:]
    return [archive for archive in ordered if is_newer_archive(archive, recorded)]


def build_merged_records(
    meta: dict[str, Any],
    processed: list[RemoteArchive],
    ignored: list[RemoteArchive] | None = None,
) -> list[dict[str, Any]]:
    """在旧记录基础上追加本次处理过的包，按 mtime 升序去重。

    ignored（判定为转储包而跳过）也记入：它们已经被"处理过"，不记的话下一轮
    会重新探测/重新判定，永远反复。
    """
    records: list[dict[str, Any]] = []
    seen: set[ArchiveKey] = set()
    existing = meta.get(MERGED_FIELD)
    if isinstance(existing, list):
        for record in existing:
            key = _record_key(record)
            if key is None or key in seen:
                continue
            seen.add(key)
            records.append(record)
    for archive in list(processed) + list(ignored or []):
        key = archive_key(archive)
        if key in seen:
            continue
        seen.add(key)
        records.append(record_archive(archive))
    records.sort(key=lambda item: float(item.get("mtime") or 0))
    return records


def build_baseline_records(
    archives: list[RemoteArchive],
    meta: dict[str, Any],
    origin: str,
    processed_keys: set[ArchiveKey],
    *,
    has_final: bool,
) -> list[dict[str, Any]]:
    """算出合并成功后的 baseline 记录。

    - 首次同步 / 手动同步：没有需要跳过的东西，清空 baseline；
    - v2 自动任务：只保留仍在远端、且确实未合并的旧 baseline；
    - legacy 自动任务：所有未合并的远端包都记为 baseline（历史内容不回填）。
    """
    if not has_final or origin == ORIGIN_MANUAL:
        return []
    merged = merged_keys(meta)
    if merged is None:
        processed = processed_keys
        return [
            record_archive(archive)
            for archive in sorted(archives, key=archive_sort_key)
            if archive_key(archive) not in processed
        ]
    baseline = baseline_keys(meta)
    return [
        record_archive(archive)
        for archive in sorted(archives, key=archive_sort_key)
        if archive_key(archive) in baseline and archive_key(archive) not in processed_keys
    ]


def max_merged_archive(ticket_id: str, meta: dict[str, Any]) -> RemoteArchive | None:
    """已合并包里的最高优先级，用于判断新包能否覆盖旧文件。

    必须带上工单号：`is_canonical_archive` 按 `/sf/log/<工单号>/<工单号>.zip`
    判定规范名，用空工单号会让所有记录都"不是规范名"，同 mtime 时平局判定失效。
    """
    records = meta.get(MERGED_FIELD)
    if not isinstance(records, list):
        return None
    best: RemoteArchive | None = None
    for record in records:
        key = _record_key(record)
        if key is None:
            continue
        path = record.get("path")
        archive = RemoteArchive(
            ticket_id,
            path if isinstance(path, str) else f"/sf/log/{ticket_id}/{ticket_id}.zip",
            key[1],
            key[0],
        )
        if best is None or is_newer_archive(archive, best):
            best = archive
    return best


@dataclass
class MergeStats:
    """一次目录合并的结果，只用于日志与测试断言。"""

    added: int = 0
    overwritten: int = 0
    skipped_conflicts: int = 0

    def add(self, other: MergeStats) -> None:
        self.added += other.added
        self.overwritten += other.overwritten
        self.skipped_conflicts += other.skipped_conflicts


def clone_tree(src: Path, dst: Path) -> None:
    """把已发布产物克隆成待发布的新树：文件用硬链接，不复制字节。

    约束：src 与 dst 必须在同一文件系统（都在 `<root>` 下，服务已有同样约束）。
    硬链接让 65 GiB 的产物也能在秒级完成"复制"；后续合并只替换目录项，
    不会写坏共享 inode。
    """
    if not src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        return
    dst.mkdir(parents=True, exist_ok=True)
    _clone_dir(src, dst)
    _copy_dir_stat(src, dst)


def _clone_dir(src: Path, dst: Path) -> None:
    with os.scandir(src) as iterator:
        entries = list(iterator)
    for entry in entries:
        source = Path(entry.path)
        target = dst / entry.name
        try:
            if entry.is_symlink():
                os.symlink(os.readlink(source), target)
            elif entry.is_dir(follow_symlinks=False):
                target.mkdir()
                _clone_dir(source, target)
                _copy_dir_stat(source, target)
            elif entry.is_file(follow_symlinks=False):
                try:
                    os.link(source, target)
                except OSError:
                    # 跨设备/文件系统不支持硬链接：退化为真实复制，正确性优先。
                    shutil.copy2(source, target)
        except FileNotFoundError:
            # 源端在克隆过程中被外部删掉（维护协程会跳过活跃工单，理论不发生），
            # 少一个文件不应让整次同步失败。
            logger.warning("克隆产物时源文件消失 path=%s", source)


def _copy_dir_stat(src: Path, dst: Path) -> None:
    try:
        info = src.stat()
        os.chmod(dst, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
        os.utime(dst, (info.st_atime, info.st_mtime))
    except OSError:
        # 元数据复制失败不影响可读性；权限兜底由发布前的归一化负责。
        return


def merge_into(src: Path, dst: Path, *, overwrite: bool) -> MergeStats:
    """把 src 目录树合并进 dst。

    `overwrite=True`：同路径以 src 为准（src 代表更新的采集包）；
    `overwrite=False`：只补 dst 不存在的路径，冲突一律保留 dst。

    后一种模式用于"乱序包"兜底：如果某个待合并包的优先级其实低于已合并内容
    （例如服务端人工恢复了旧包），只加不覆盖才保证"最新包赢"不被破坏。
    """
    stats = MergeStats()
    if not src.is_dir():
        return stats
    _merge_dir(src, dst, overwrite, stats)
    return stats


def _merge_dir(src: Path, dst: Path, overwrite: bool, stats: MergeStats) -> None:
    with os.scandir(src) as iterator:
        entries = list(iterator)
    for entry in entries:
        source = Path(entry.path)
        target = dst / entry.name
        if entry.is_symlink():
            _merge_leaf(source, target, overwrite, stats, symlink=True)
        elif entry.is_dir(follow_symlinks=False):
            if target.exists() and not target.is_symlink() and target.is_dir():
                _merge_dir(source, target, overwrite, stats)
                continue
            if os.path.lexists(target):
                if not overwrite:
                    stats.skipped_conflicts += 1
                    continue
                _remove_path(target)
                stats.overwritten += 1
            target.mkdir(parents=True, exist_ok=True)
            _copy_dir_stat(source, target)
            _merge_dir(source, target, overwrite, stats)
        elif entry.is_file(follow_symlinks=False):
            _merge_leaf(source, target, overwrite, stats, symlink=False)


def _merge_leaf(
    source: Path,
    target: Path,
    overwrite: bool,
    stats: MergeStats,
    *,
    symlink: bool,
) -> None:
    exists = os.path.lexists(target)
    if exists:
        if not overwrite:
            stats.skipped_conflicts += 1
            return
        _remove_path(target)
        stats.overwritten += 1
    else:
        stats.added += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    if symlink:
        os.symlink(os.readlink(source), target)
        source.unlink(missing_ok=True)
        return
    try:
        os.replace(source, target)
    except OSError:
        # 同一根目录下正常不会触发；跨设备时退化为复制 + 删除。
        shutil.copy2(source, target)
        source.unlink(missing_ok=True)


def _remove_path(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            _rmtree(path)
    except FileNotFoundError:
        return


def _rmtree(root: Path) -> None:
    """删除可能带 000 权限的目录树；失败必须抛出，不能静默留下半棵旧树。"""
    _make_removable(root)
    shutil.rmtree(root)


def _make_removable(root: Path) -> None:
    """给属主补读写执行位，使 rmtree 能真正删掉（与 publisher 同款兜底）。"""
    want_dir = stat.S_IRWXU
    want_file = stat.S_IRUSR | stat.S_IWUSR
    uid = os.getuid()

    def widen(path: Path, want: int) -> None:
        try:
            info = os.lstat(path)
        except OSError:
            return
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return
        if info.st_uid != uid:
            return
        mode = stat.S_IMODE(info.st_mode)
        if mode & want == want:
            return
        try:
            os.chmod(path, mode | want)
        except OSError:
            return

    widen(root, want_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            _make_removable(entry)
        else:
            widen(entry, want_file)


@dataclass
class TreeStats:
    bytes: int = 0
    files: int = 0
    # 目录数只用于日志；清理与容量判断不依赖它。
    directories: int = 0
    excluded: int = field(default=0)


def tree_stats(root: Path, exclude_names: frozenset[str] | set[str] = frozenset()) -> TreeStats:
    """统计产物树的真实文件字节与数量（不跟随符号链接）。

    与 extractor 的 `_tree_stats` 不同，这里作用于"已合并的最终树"：必须排除
    `.sync-meta.json`，它是同步元数据而不是采集内容，否则每次合并都会把它算进
    bytes/files，容量与展示都会缓慢漂移。
    """
    result = TreeStats()
    if not root.is_dir():
        return result
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                entries = list(iterator)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    result.directories += 1
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.name in exclude_names:
                    result.excluded += 1
                    continue
                result.bytes += entry.stat(follow_symlinks=False).st_size
                result.files += 1
            except OSError:
                continue
    return result
