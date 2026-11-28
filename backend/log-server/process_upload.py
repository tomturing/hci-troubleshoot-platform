#!/usr/bin/env python3
"""独立上传处理进程：合片 -> MD5 -> 移动归档

由 app.py 在收齐所有分片后，以 subprocess.Popen(start_new_session=True) 启动，
运行在 gunicorn 进程之外。这样耗时的合片与 MD5 计算不会阻塞 gevent worker 的
事件循环，从而避免心跳超时（历史上 80GB 上传即因此被 master 杀掉 worker）。

用法: python3 process_upload.py <upload_id>
成功退出码 0；失败退出码 1；参数错误退出码 2。
"""
import fcntl
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

import archive_index

# ---- 配置（与 app.py 保持一致，支持环境变量覆盖以便测试隔离）----
UPLOAD_FOLDER = os.environ.get('ACLI_UPLOAD_FOLDER', '/sf/log')
TEMP_FOLDER = os.environ.get('ACLI_TEMP_FOLDER', '/tmp/acli_upload_chunks')
SESSION_STORE = os.environ.get('ACLI_SESSION_STORE', '/tmp/acli_upload_sessions')
MERGE_BUFFER = 1024 * 1024            # 1MB 缓冲（原 4KB，提速数倍）
LOG_DIR = os.environ.get('ACLI_LOG_DIR', '/acli/logs')
LOG_FILE = os.path.join(LOG_DIR, 'upload_process.log')
FAIL_LOG_FILE = os.path.join(LOG_DIR, 'upload_failed.log')
LOG_MAX = 10 * 1024 * 1024
FAIL_LOG_MAX = 50 * 1024 * 1024


def setup_logging():
    logger = logging.getLogger('process_upload')
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # 主日志：文件不可写（如运行用户无权限）时降级到 stderr，不阻断上传处理
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX, backupCount=5, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as e:
        sys.stderr.write(f'WARN: 无法写入处理日志 {LOG_FILE}: {e}\n')
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    # 失败日志单独写文件，便于监控告警（只记录 ERROR 级别）
    fail_logger = logging.getLogger('process_upload_fail')
    fail_logger.setLevel(logging.ERROR)
    try:
        ffh = RotatingFileHandler(FAIL_LOG_FILE, maxBytes=FAIL_LOG_MAX, backupCount=3, encoding='utf-8')
        ffh.setFormatter(fmt)
        fail_logger.addHandler(ffh)
    except OSError:
        pass

    return logger, fail_logger


log, fail_log = setup_logging()


def load_session(upload_id):
    path = os.path.join(SESSION_STORE, f"{upload_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.error(f"读取会话失败 {upload_id}: {e}")
        return None


def get_file_md5(filepath):
    hash_md5 = hashlib.md5()
    buffer_size = 4 * 1024 * 1024
    progress_interval = 200 * 1024 * 1024
    bytes_processed = 0
    last_log_time = time.time()
    file_size = os.path.getsize(filepath)
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(buffer_size), b""):
                hash_md5.update(chunk)
                bytes_processed += len(chunk)
                if bytes_processed % progress_interval == 0 or time.time() - last_log_time >= 10:
                    pct = (bytes_processed / file_size) * 100
                    log.info(f"MD5进度: {pct:.1f}% ({bytes_processed/1024/1024:.0f}MB/{file_size/1024/1024:.0f}MB) {os.path.basename(filepath)}")
                    last_log_time = time.time()
        log.info(f"MD5计算完成 {os.path.basename(filepath)}")
        return hash_md5.hexdigest()
    except Exception as e:
        log.error(f"MD5计算失败 {filepath}: {e}")
        return None


def merge_chunks(session, upload_id):
    """按分片序号顺序合并为单个文件，合片过程中即删除各分片"""
    temp_filepath = session['temp_filepath']
    standardized = {str(k): v for k, v in session['chunks'].items()}
    indices = sorted(int(k) for k in standardized)
    # 摘要与合片在同一趟读盘里完成：原本单独的 get_file_md5 会把整个包再读一遍，
    # 100GB 归档就是白白多读 100GB。sha256 与 md5 一起算，边际成本只有哈希本身。
    hasher_sha = hashlib.sha256()
    hasher_md5 = hashlib.md5()
    with open(temp_filepath, 'wb') as outfile:
        for i in indices:
            key = str(i)
            p = standardized[key]
            if not os.path.exists(p):
                raise FileNotFoundError(f"分片缺失: {p}")
            with open(p, 'rb') as infile:
                while True:
                    data = infile.read(MERGE_BUFFER)
                    if not data:
                        break
                    outfile.write(data)
                    hasher_sha.update(data)
                    hasher_md5.update(data)
            os.remove(p)
    return temp_filepath, hasher_sha.hexdigest(), hasher_md5.hexdigest()


def cleanup_chunks(upload_id):
    """失败时清理该次上传残留的临时文件"""
    try:
        for name in os.listdir(TEMP_FOLDER):
            if name.startswith(upload_id) and name.endswith('.part'):
                with suppress(OSError):
                    os.remove(os.path.join(TEMP_FOLDER, name))
    except Exception:
        pass


@contextmanager
def _archive_dir_lock(target_dir):
    """对工单目录加排他锁，串行化"改名 / 删旧包 / 写缓存"这一段。"""
    fd = os.open(target_dir, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _drop_identical_archives(target_dir, sha256_hex, cache):
    """删除与新包内容完全相同的归档，返回 (已删除文件名, [(失败文件名, 原因)])。

    只看整文件 sha256：体积相同或名字相近都不算证据——同一工单重复采集出的
    包体积常常很接近，误删的代价是客户日志凭空消失，宁可多算一次摘要。
    摘要算不出来的（读盘错误、权限）一律按"不确定"处理，既不删也不拦。
    """
    removed = []
    failed = []
    for name in archive_index.zip_names(target_dir):
        path = os.path.join(target_dir, name)
        try:
            digest = archive_index.sha256_of(path, cache)
        except OSError as e:
            failed.append((name, f"摘要计算失败: {e}"))
            continue
        if digest != sha256_hex:
            continue
        try:
            os.remove(path)
            removed.append(name)
        except OSError as e:
            failed.append((name, f"删除失败: {e}"))
    return removed, failed


def _stamp_existing_archive(path):
    """把已存在的归档件改名加时间戳，返回改名后的路径。

    时间戳取旧文件自身的 mtime（即它当时的归档时刻），而不是当前时间：
    重传可能在数小时后才完成，用当前时间会让文件名里的时间与内容新旧对不上。
    极端情况下同秒重复（重传被打断后立刻又传一次），追加 -1/-2 递增后缀。
    """
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(os.path.getmtime(path)))
    stem, ext = os.path.splitext(path)
    candidate = f"{stem}-{stamp}{ext}"
    index = 1
    while os.path.exists(candidate):
        candidate = f"{stem}-{stamp}-{index}{ext}"
        index += 1
    os.rename(path, candidate)
    return candidate


def process(upload_id):
    t0 = time.time()
    session = load_session(upload_id)
    if not session:
        log.error(f"会话不存在或已处理: {upload_id}")
        return False
    filename = session.get('filename')
    trace_id = session.get('trace_id') or '-'
    upload_source = session.get('upload_source') or 'unknown'
    task_id = session.get('task_id')
    try:
        log.info(
            f"开始处理 upload_id={upload_id} file={filename} source={upload_source} "
            f"task_id={task_id or '-'} trace={trace_id}"
        )
        # 1. 合片
        merged_path, sha256_hex, md5_hex = merge_chunks(session, upload_id)
        # 1.5 大小校验（2026-10-31）：合片结果必须等于 init 声明的 file_size。
        # 为什么必须在这里拦：/upload_chunk 不校验分片边界，客户端若按错误的分片方案
        # 切片（例如源包在传输期间被重新生成），合片会得到大小不符的文件，而后续流程
        # 只把它 move 进归档 —— 坏包会被当成成功归档，且平台侧无从发现。
        actual_size = os.path.getsize(merged_path)
        expected_size = int(session.get("file_size") or 0)
        if expected_size > 0 and actual_size != expected_size:
            raise ValueError(
                f"合片大小不符: 声明 {expected_size} 字节，实际 {actual_size} 字节"
            )
        # 2. 摘要已在合片时一并算出（见 merge_chunks），此处不再重复读盘
        # 3. 移动到归档目录（shutil.move 同分区为 rename，跨设备自动拷贝）
        base = filename[:-4] if filename.endswith('.zip') else filename
        target_dir = os.path.join(UPLOAD_FOLDER, base)
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, filename)

        # 先把新包挪进同目录的暂存名，再动旧包：任何时刻磁盘上都至少有一份完整
        # 归档。反过来做（先删旧再 move 新）在磁盘写满或 rename 失败时会让工单
        # 目录变空——删掉的还是客户唯一一份日志。
        staged_path = target_path + '.incoming'
        if os.path.exists(staged_path):
            os.remove(staged_path)
        shutil.move(merged_path, staged_path)

        finished_at_utc = datetime.now(UTC).isoformat()

        # 同目录归档必须串行：两个上传同时收尾时，一个在改名/删旧包，另一个在算
        # 摘要，会互相踩踏（删掉对方刚移动的文件、缓存写回覆盖）。flock 锁目录 fd，
        # 阻塞等待，跨进程有效；/sf/log 是本地盘，分布式锁语义不适用也不需要。
        with _archive_dir_lock(target_dir):
            cache = archive_index.load_cache(target_dir)
            archive_index.prune_cache(target_dir, cache)

            # 无条件判重：不看规范名在不在，因为历史副本可能只剩带戳名字
            # （人工改名、上一轮收尾异常都会造成这种目录）。
            removed, failed = _drop_identical_archives(target_dir, sha256_hex, cache)
            if removed:
                log.warning(
                    "归档内容重复（sha256=%s…），已删除旧包 %d 份: %s trace=%s",
                    sha256_hex[:12], len(removed), ", ".join(removed), trace_id,
                )
            for name, reason in failed:
                log.warning("旧包判重失败，保留 %s: %s trace=%s", name, reason, trace_id)

            if os.path.exists(target_path):
                # 走到这里说明规范名与新包内容不同（或删不掉）：规范名仍要指向
                # 最新包，所以把旧包改名加时间戳保留，并同步迁移缓存元数据。
                archived_path = _stamp_existing_archive(target_path)
                old_entry = cache.pop(filename, None)
                if isinstance(old_entry, dict):
                    cache[os.path.basename(archived_path)] = old_entry
                log.warning(
                    "归档已存在，旧包改存为 %s，新包占用 %s trace=%s",
                    archived_path, target_path, trace_id,
                )

            os.replace(staged_path, target_path)
            stat = os.stat(target_path)
            size = stat.st_size
            # 新包摘要与来源元数据顺手写进缓存：下次判重及 /case_status 查询 O(1) 读取。
            cache[filename] = archive_index.build_cache_entry(
                stat,
                sha256_hex,
                upload_source=upload_source,
                upload_id=upload_id,
                task_id=task_id,
                trace_id=trace_id if trace_id != '-' else None,
                finished_at=finished_at_utc,
            )
            archive_index.save_cache(target_dir, cache)
        # 4. 会话标记完成并回写摘要 —— **不再删除会话**
        # 平台要读 digest 才能校验"落盘字节 == 平台副本"；而旧的"404 即成功"判据
        # 只能证明处理进程跑完了。会话只有 2KB 左右，由 session_reaper.py 在 24h 后回收。
        session['status'] = 'done'
        session['digest'] = {'sha256': sha256_hex, 'md5': md5_hex, 'size': size}
        session['target_path'] = target_path
        session['finished_at'] = finished_at_utc
        session['last_updated'] = datetime.now().isoformat()
        try:
            tmp_path = os.path.join(SESSION_STORE, f"{upload_id}.json.tmp")
            with open(tmp_path, 'w') as f:
                json.dump(session, f)
            os.replace(tmp_path, os.path.join(SESSION_STORE, f"{upload_id}.json"))
        except Exception as we:
            log.error(f"写回完成状态出错 {upload_id}: {we} trace={trace_id}")
        cost = time.time() - t0
        log.info(
            f"处理完成 upload_id={upload_id} file={filename} source={upload_source} "
            f"task_id={task_id or '-'} size={size} sha256={sha256_hex} md5={md5_hex} "
            f"target={target_path} cost={cost:.1f}s trace={trace_id} "
            f"metric=upload_process_done_total"
        )
        return True
    except Exception as e:
        fail_log.error(
            f"处理失败 upload_id={upload_id} file={filename} source={upload_source} "
            f"trace={trace_id}: {e}",
            exc_info=True,
        )
        log.error(
            f"处理失败 upload_id={upload_id} file={filename} source={upload_source} "
            f"trace={trace_id} metric=upload_process_failed_total: {e}",
            exc_info=True,
        )
        # 失败原因写回会话（2026-10-31）：/upload_status 会把它透出，调用方据此立即
        # 判失败，而不是等到 archive_timeout 之后才知道。会话刻意保留 —— 删掉会话
        # 会让调用方读到 404 并把它当成"归档成功"，那是更坏的结果。
        try:
            session['status'] = 'failed'
            session['error'] = str(e)[:500]
            session['finished_at'] = datetime.now(UTC).isoformat()
            session['last_updated'] = datetime.now().isoformat()
            tmp_path = os.path.join(SESSION_STORE, f"{upload_id}.json.tmp")
            with open(tmp_path, 'w') as f:
                json.dump(session, f)
            os.replace(tmp_path, os.path.join(SESSION_STORE, f"{upload_id}.json"))
        except Exception as we:
            log.error(f"写回失败状态出错 {upload_id}: {we} trace={trace_id}")
        cleanup_chunks(upload_id)
        return False


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("usage: process_upload.py <upload_id>")
        sys.exit(2)
    uid = sys.argv[1]
    ok = process(uid)
    sys.exit(0 if ok else 1)
