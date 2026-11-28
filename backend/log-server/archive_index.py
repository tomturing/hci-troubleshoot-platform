#!/usr/bin/env python3
"""归档目录的摘要索引：判断"这个包是不是已经有一份了"。

为什么要单独一层：判重既要给上传前的 /precheck 用（只读、要快），也要给归档
进程用（可删），两边必须用**同一套**摘要来源，否则"预检说没有、归档时又发现有"
就是自相矛盾的提示。

缓存文件 `.digests.json` 放在工单目录里，键是文件名，值是
`{size, mtime_ns, sha256}`。用 `mtime_ns` 而不是 `mtime`：同一秒内重传很常见
（重传被打断后立刻再传），秒级精度会让旧摘要被错误复用，从而漏删/误删。
文件一旦被替换（size 或 mtime_ns 变），缓存条目自动失效、重新计算。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os

DIGEST_CACHE_NAME = ".digests.json"
# 读盘缓冲：与合片一致，1MB。大包（最大几十 GB）靠它把 syscall 次数压下来。
_READ_BUFFER = 1024 * 1024


def zip_names(target_dir: str, exclude: str | None = None) -> list[str]:
    """列出目录内的归档 zip（不含摘要缓存等隐藏文件）。"""
    try:
        names = os.listdir(target_dir)
    except OSError:
        return []
    result = []
    for name in sorted(names):
        if name == exclude:
            continue
        if not name.lower().endswith(".zip"):
            continue
        if not os.path.isfile(os.path.join(target_dir, name)):
            continue
        result.append(name)
    return result


def load_cache(target_dir: str) -> dict:
    """读摘要缓存；损坏或不可读时当作空缓存，绝不因此阻断归档。"""
    path = os.path.join(target_dir, DIGEST_CACHE_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # 过滤掉结构不对的条目：手工改坏一个键不该让整份缓存失效
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_cache(target_dir: str, cache: dict) -> None:
    """原子写缓存。写失败只记日志不抛：缓存是加速手段，不是正确性依赖。"""
    path = os.path.join(target_dir, DIGEST_CACHE_NAME)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def build_cache_entry(
    stat: os.stat_result,
    sha256: str,
    *,
    upload_source: str | None = None,
    upload_id: str | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
    finished_at: str | None = None,
) -> dict:
    """构造写入 .digests.json 的归档元数据条目（兼容旧版仅含摘要三字段的结构）。"""
    entry: dict = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256,
    }
    if upload_source:
        entry["upload_source"] = upload_source
    if upload_id:
        entry["upload_id"] = upload_id
    if task_id:
        entry["task_id"] = task_id
    if trace_id:
        entry["trace_id"] = trace_id
    if finished_at:
        entry["finished_at"] = finished_at
    return entry


def read_cached_entry(cache: dict, name: str, stat: os.stat_result) -> dict | None:
    """O(1) 读取缓存条目；仅当 size 与 mtime_ns 同时精确匹配时返回，绝不触发读盘哈希。"""
    entry = cache.get(name)
    if not isinstance(entry, dict):
        return None
    if entry.get("size") != stat.st_size or entry.get("mtime_ns") != stat.st_mtime_ns:
        return None
    return entry


def archive_bind_key(stat: os.stat_result, sha256: str | None = None) -> str:
    """生成跨改名稳定的归档物理绑定键。

    当新归档落盘导致旧 Qxxx.zip 被改名为 Qxxx-YYYYMMDDHHMMSS.zip 时，
    文件名会变化，但 sha256（优先）以及 (st_size, st_mtime_ns) 保持严格不变，
    从而保证平台侧对账不会将同一份历史归档重复建任务。
    """
    if sha256:
        return f"sha256:{sha256}"
    return f"stat:{stat.st_size}:{stat.st_mtime_ns}"


def sha256_of(path: str, cache: dict | None = None) -> str:
    """返回文件的 sha256，优先复用缓存条目。"""
    stat = os.stat(path)
    name = os.path.basename(path)
    if cache is not None:
        entry = read_cached_entry(cache, name, stat)
        if entry and entry.get("sha256"):
            return entry["sha256"]

    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_READ_BUFFER), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if cache is not None:
        existing = cache.get(name) if isinstance(cache.get(name), dict) else {}
        cache[name] = build_cache_entry(
            stat,
            digest,
            upload_source=existing.get("upload_source"),
            upload_id=existing.get("upload_id"),
            task_id=existing.get("task_id"),
            trace_id=existing.get("trace_id"),
            finished_at=existing.get("finished_at"),
        )
    return digest


def prune_cache(target_dir: str, cache: dict) -> None:
    """删掉已不存在文件的条目，避免缓存文件随历史清理无限膨胀。"""
    present = set(zip_names(target_dir))
    for name in list(cache):
        if name not in present:
            cache.pop(name, None)


def find_duplicate(target_dir: str, sha256_hex: str) -> dict | None:
    """精确命中：目录内存在内容完全相同的包，返回它的落盘事实。

    摘要计算失败的文件直接跳过——宁可让调用方以为"没有重复"（结果是多传
    一次），也不能因为读盘错误误判成重复、让客户以为日志已经传上来了。
    """
    cache = load_cache(target_dir)
    before = json.dumps(cache, sort_keys=True)
    hit = None
    for name in zip_names(target_dir):
        path = os.path.join(target_dir, name)
        try:
            digest = sha256_of(path, cache)
        except OSError:
            continue
        if digest == sha256_hex:
            hit = name
            break
    if json.dumps(cache, sort_keys=True) != before:
        save_cache(target_dir, cache)
    return describe(target_dir, hit) if hit else None


def find_by_size(target_dir: str, size: int) -> list[dict]:
    """只按体积命中，返回候选列表（用于客户端给不出 sha256 时的软提示）。"""
    result = []
    for name in zip_names(target_dir):
        try:
            info = os.stat(os.path.join(target_dir, name))
        except OSError:
            continue
        if info.st_size == size:
            result.append(describe(target_dir, name))
    return result


def describe(target_dir: str, name: str) -> dict:
    """把一份归档描述成可直接返回给前端的结构（不含绝对路径）。"""
    info = os.stat(os.path.join(target_dir, name))
    return {
        "stored_name": name,
        "size": info.st_size,
        "mtime": int(info.st_mtime),
    }
