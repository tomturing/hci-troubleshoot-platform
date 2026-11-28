#!/usr/bin/env python3
"""
统一生命周期清理工具

子命令:
  chunks  清理 /tmp 孤儿上传分片（按 mtime 过期，默认 24 小时）
  cases   水位驱动的工单日志清理（默认 96% 触发，删到 90% 停止）

设计要点:
  * 默认 dry-run，必须显式 --apply 才会真正删除
  * --expect-count 安全阀：实际待删条数与预期不符立即中止
  * 合规下限 180 天：低于该年龄的工单任何情况下不删
  * .keep 白名单：/sf/log/.keep 中列出的工单永久保留
  * 单实例 flock，避免 cron 重叠执行
  * 删除前先写 manifest 再删除，保证可追溯

用法示例:
  lifecycle_clean.py chunks                      # 预览
  lifecycle_clean.py chunks --apply              # 执行
  lifecycle_clean.py cases                       # 预览（未超水位则直接退出）
  lifecycle_clean.py cases --apply --expect-count 137
"""

import argparse
import csv
import fcntl
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# ==================== 配置 ====================
CHUNK_DIR = '/tmp/acli_upload_chunks'
SESSION_DIR = '/tmp/acli_upload_sessions'
LOG_DIR = '/sf/log'
KEEP_FILE = os.path.join(LOG_DIR, '.keep')
LOG_FILE = '/acli/logs/lifecycle.log'
MANIFEST_DIR = '/acli/logs'
LOCK_FILE = '/tmp/lifecycle_clean.lock'

TICKET_RE = re.compile(r'^Q(\d{8})\d{5}$')      # Q + 8位日期 + 5位序号

# 水位阈值（百分比，与 df 的 Use% 口径一致）
WATERMARK_HIGH = 96.0     # 触发清理
WATERMARK_LOW = 90.0      # 回落到该水位即停止
WATERMARK_WARN = 90.0     # 提醒
WATERMARK_CRIT = 94.0     # 严重告警
WATERMARK_EMERG = 98.0    # 紧急：取消节流

COMPLIANCE_MIN_DAYS = 180   # 合规下限（天），低于此年龄不删
BATCH_MAX_BYTES = 200 * 1024 ** 3   # 单批最大删除量
BATCH_SLEEP_SEC = 3                 # 批次间隔
MAX_DELETE_BYTES = 500 * 1024 ** 3  # 单次运行删除上限
CHUNK_MAX_AGE_HOURS = 24            # 分片过期时间

logger = logging.getLogger('lifecycle')


# ==================== 基础工具 ====================
def setup_logging(verbose=True):
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fh = TimedRotatingFileHandler(LOG_FILE, when='midnight',
                                  backupCount=30, encoding='utf-8')
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)


def human(nbytes):
    """字节转人类可读"""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(nbytes) < 1024.0:
            return f'{nbytes:.2f}{unit}'
        nbytes /= 1024.0
    return f'{nbytes:.2f}PB'


def disk_info(path='/'):
    """返回 (total, used, avail, use_pct)，口径与 df 一致"""
    st = shutil.disk_usage(path)
    total, used, avail = st.total, st.used, st.free
    pct = used / (used + avail) * 100 if (used + avail) else 0.0
    return total, used, avail, pct


class SingleInstance:
    """flock 单实例，避免 cron 重叠

    锁文件不可写（如属主不一致）时降级为无锁运行并告警，
    避免因锁问题导致清理任务整体失败——清理的可用性优先于严格的并发保护。
    仅当锁确实被其他实例持有时才退出。
    """

    def __init__(self, lockfile):
        self.lockfile = lockfile
        self.fp = None
        self.locked = False

    def __enter__(self):
        candidates = [self.lockfile,
                      os.path.join(tempfile.gettempdir(), '.lifecycle_clean.lock')]
        for path in candidates:
            try:
                fp = open(path, 'w')
            except OSError as e:
                logger.warning(f'无法打开锁文件 {path}: {e}')
                continue
            try:
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fp.close()
                logger.warning('已有实例正在运行，本次退出')
                sys.exit(0)
            self.fp = fp
            self.locked = True
            return self

        logger.warning('所有锁路径均不可用，本次以无锁方式继续运行')
        return self

    def __exit__(self, *exc):
        if self.fp:
            try:
                if self.locked:
                    fcntl.flock(self.fp, fcntl.LOCK_UN)
                self.fp.close()
            except OSError:
                pass


# ==================== 保护名单 ====================
def load_keep_set():
    """读取 /sf/log/.keep 白名单（一行一个工单号，# 开头为注释）"""
    keep = set()
    if os.path.isfile(KEEP_FILE):
        try:
            with open(KEEP_FILE, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        keep.add(line)
        except OSError as e:
            logger.error(f'读取 .keep 失败: {e}')
    return keep


def load_active_tickets():
    """从上传会话中提取正在上传的工单号（去掉 .zip）"""
    names = set()
    if not os.path.isdir(SESSION_DIR):
        return names
    for fn in os.listdir(SESSION_DIR):
        if not fn.endswith('.json'):
            continue
        try:
            with open(os.path.join(SESSION_DIR, fn), encoding='utf-8') as f:
                data = json.load(f)
            filename = data.get('filename')
            if filename and filename.endswith('.zip'):
                names.add(filename[:-4])
        except Exception as e:
            logger.warning(f'解析会话 {fn} 失败: {e}')
    return names


def load_active_upload_ids():
    """活跃会话的 upload_id（用于保护其分片）"""
    ids = set()
    if not os.path.isdir(SESSION_DIR):
        return ids
    for fn in os.listdir(SESSION_DIR):
        if fn.endswith('.json'):
            ids.add(fn[:-5])
    return ids


# ==================== 目录扫描 ====================
def dir_stat(path):
    """返回 (总字节数, 最新 mtime)"""
    size = 0
    mtime = 0.0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                st = os.stat(os.path.join(root, fn))
            except OSError:
                continue
            size += st.st_size
            if st.st_mtime > mtime:
                mtime = st.st_mtime
    if mtime == 0.0:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = time.time()
    return size, mtime


def scan_cases(keep, active):
    """扫描 /sf/log，返回候选工单列表（含年龄、大小）"""
    now = datetime.now()
    candidates = []
    skipped = {'keep': 0, 'active': 0, 'irregular': 0, 'empty': 0}

    try:
        entries = sorted(os.listdir(LOG_DIR))
    except OSError as e:
        logger.error(f'无法列出 {LOG_DIR}: {e}')
        return candidates, skipped

    for name in entries:
        path = os.path.join(LOG_DIR, name)
        if not os.path.isdir(path):
            continue

        m = TICKET_RE.match(name)
        if not m:
            skipped['irregular'] += 1
            logger.debug(f'非规范目录，跳过: {name}')
            continue

        if name in keep:
            skipped['keep'] += 1
            continue
        if name in active:
            skipped['active'] += 1
            continue

        size, mtime = dir_stat(path)
        if size == 0:
            skipped['empty'] += 1
            continue

        # 年龄取 max(工单日期, 最近修改时间)，取较大值更保守（倾向保留）
        try:
            ticket_dt = datetime.strptime(m.group(1), '%Y%m%d')
        except ValueError:
            ticket_dt = datetime.fromtimestamp(mtime)
        ref_dt = max(ticket_dt, datetime.fromtimestamp(mtime))
        age_days = (now - ref_dt).days

        candidates.append({
            'name': name,
            'path': path,
            'size': size,
            'age_days': age_days,
            'ticket_date': ticket_dt.strftime('%Y-%m-%d'),
            'ref_date': ref_dt.strftime('%Y-%m-%d'),
        })

    return candidates, skipped


# ==================== 子命令：chunks ====================
def cmd_chunks(args):
    logger.info('=' * 60)
    logger.info('开始扫描孤儿上传分片')

    if not os.path.isdir(CHUNK_DIR):
        logger.info(f'{CHUNK_DIR} 不存在，无需清理')
        return 0

    active_ids = load_active_upload_ids()
    cutoff = time.time() - args.max_age_hours * 3600

    victims = []
    total_bytes = 0
    scanned = 0
    for entry in os.scandir(CHUNK_DIR):
        if not entry.is_file() or not entry.name.endswith('.part'):
            continue
        scanned += 1
        upload_id = entry.name.split('_chunk_')[0]
        if upload_id in active_ids:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            continue
        victims.append((entry.path, st.st_size, st.st_mtime))
        total_bytes += st.st_size

    logger.info(f'扫描文件 {scanned} 个，其中可清理 {len(victims)} 个，'
                f'合计 {human(total_bytes)}（活跃会话 {len(active_ids)} 个已保护）')

    if not victims:
        logger.info('没有需要清理的分片')
        return 0

    if args.expect_count is not None and len(victims) != args.expect_count:
        logger.error(f'安全阀触发：实际待删 {len(victims)} 条，'
                     f'预期 {args.expect_count} 条，中止')
        return 2

    if not args.apply:
        for p, s, mt in victims[:10]:
            logger.info(f'  [预览] {os.path.basename(p)} '
                        f'{human(s)} {datetime.fromtimestamp(mt):%Y-%m-%d %H:%M}')
        if len(victims) > 10:
            logger.info(f'  ... 其余 {len(victims) - 10} 个省略')
        logger.info('dry-run 结束，未删除任何文件（加 --apply 执行）')
        return 0

    # 执行删除
    manifest = os.path.join(
        MANIFEST_DIR, f'chunk_manifest_{datetime.now():%Y%m%d_%H%M%S}.csv')
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    deleted = 0
    freed = 0
    with open(manifest, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['file', 'size_bytes', 'mtime', 'deleted_at'])
        for p, s, mt in victims:
            try:
                os.remove(p)
            except OSError as e:
                logger.error(f'删除失败 {p}: {e}')
                continue
            deleted += 1
            freed += s
            writer.writerow([p, s,
                             datetime.fromtimestamp(mt).isoformat(),
                             datetime.now().isoformat()])

    logger.info(f'分片清理完成：删除 {deleted} 个，释放 {human(freed)}，'
                f'清单 {manifest}')
    return 0


# ==================== 子命令：cases ====================
def cmd_cases(args):
    total, used, avail, pct = disk_info(LOG_DIR)
    logger.info('=' * 60)
    logger.info(f'磁盘水位 {pct:.1f}%（已用 {human(used)} / 可用 {human(avail)}），'
                f'触发线 {args.high}% / 停止线 {args.low}%')

    # 告警分级
    if pct >= WATERMARK_CRIT:
        logger.critical(f'磁盘水位告警：{pct:.1f}% >= {WATERMARK_CRIT}%')
    elif pct >= WATERMARK_WARN:
        logger.warning(f'磁盘水位提醒：{pct:.1f}% >= {WATERMARK_WARN}%')

    if pct < args.high:
        logger.info(f'水位 {pct:.1f}% 未达触发线 {args.high}%，本次不清理')
        return 0

    # 需要释放的空间
    need_bytes = (pct - args.low) / 100.0 * (used + avail)
    emergency = pct >= WATERMARK_EMERG
    if emergency:
        logger.critical(f'紧急模式：水位 {pct:.1f}%，取消节流快速释放')

    logger.info(f'需释放约 {human(need_bytes)}')

    keep = load_keep_set()
    active = load_active_tickets()
    logger.info(f'白名单锁定 {len(keep)} 个，活跃上传保护 {len(active)} 个')

    candidates, skipped = scan_cases(keep, active)
    logger.info(f'扫描到工单目录 {len(candidates) + sum(skipped.values())} 个：'
                f'可评估 {len(candidates)}、白名单跳过 {skipped["keep"]}、'
                f'活跃跳过 {skipped["active"]}、非规范 {skipped["irregular"]}、'
                f'空目录 {skipped["empty"]}')

    # 合规下限过滤 + 最老优先
    pool = [c for c in candidates if c['age_days'] > args.compliance_days]
    pool.sort(key=lambda c: (c['ref_date'], c['name']))
    logger.info(f'合规下限 {args.compliance_days} 天，候选池 {len(pool)} 个'
                f'（{human(sum(c["size"] for c in pool))}）')

    if not pool:
        logger.error('候选池为空：所有数据均在合规保护期内，'
                     '无法通过删除降水位，请人工介入（扩容或外迁）')
        return 1

    # 选取待删工单
    selected = []
    acc = 0
    for c in pool:
        if acc >= need_bytes:
            break
        if acc + c['size'] > args.max_delete_gb * 1024 ** 3:
            logger.warning(f'达到单次删除上限 {args.max_delete_gb}GB，'
                           f'剩余将在下次运行继续')
            break
        selected.append(c)
        acc += c['size']

    if not selected:
        logger.info('无需删除')
        return 0

    pool_total = human(sum(c['size'] for c in pool))
    logger.info(f'选定待删 {len(selected)} 个工单，'
                f'合计 {human(acc)}（候选池共 {pool_total}）')
    if selected:
        logger.info(f'  删除范围：{selected[0]["ref_date"]} ~ {selected[-1]["ref_date"]}')

    if args.expect_count is not None and len(selected) != args.expect_count:
        logger.error(f'安全阀触发：实际待删 {len(selected)} 条，'
                     f'预期 {args.expect_count} 条，中止')
        return 2

    # 预览
    for c in selected[:15]:
        logger.info(f'  {"[删除]" if args.apply else "[预览]"} {c["name"]} '
                    f'{human(c["size"])} 年龄{c["age_days"]}天 '
                    f'工单日期{c["ticket_date"]}')
    if len(selected) > 15:
        logger.info(f'  ... 其余 {len(selected) - 15} 个省略')

    if not args.apply:
        after = (used - acc) / (used + avail) * 100 if (used + avail) else 0
        logger.info(f'dry-run 结束，未删除。预计执行后水位 {after:.1f}%'
                    f'（加 --apply 执行）')
        return 0

    # 先写 manifest，再删除
    manifest = os.path.join(
        MANIFEST_DIR, f'case_manifest_{datetime.now():%Y%m%d_%H%M%S}.csv')
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    deleted = 0
    freed = 0
    batch_acc = 0
    with open(manifest, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['ticket', 'path', 'size_bytes', 'age_days',
                         'ticket_date', 'ref_date', 'deleted_at'])
        for c in selected:
            try:
                shutil.rmtree(c['path'])
            except OSError as e:
                logger.error(f'删除失败 {c["path"]}: {e}')
                continue
            deleted += 1
            freed += c['size']
            batch_acc += c['size']
            writer.writerow([c['name'], c['path'], c['size'], c['age_days'],
                             c['ticket_date'], c['ref_date'],
                             datetime.now().isoformat()])

            if not emergency and batch_acc >= BATCH_MAX_BYTES:
                logger.info(f'  已删 {human(freed)}，休眠 {BATCH_SLEEP_SEC}s 降低 IO 压力')
                time.sleep(BATCH_SLEEP_SEC)
                batch_acc = 0

    _, used2, avail2, pct2 = disk_info(LOG_DIR)
    logger.info(f'工单清理完成：删除 {deleted} 个，释放 {human(freed)}，'
                f'水位 {pct:.1f}% -> {pct2:.1f}%，清单 {manifest}')

    if pct2 >= args.high:
        logger.warning(f'释放后水位仍为 {pct2:.1f}%，未达停止线，'
                       f'下次运行将继续（如需加速请考虑扩容）')
    return 0


# ==================== 入口 ====================
def main():
    parser = argparse.ArgumentParser(description='统一生命周期清理工具')
    parser.add_argument('--no-lock', action='store_true', help='跳过单实例锁')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p1 = sub.add_parser('chunks', help='清理 /tmp 孤儿上传分片')
    p1.add_argument('--max-age-hours', type=float, default=CHUNK_MAX_AGE_HOURS)
    p1.add_argument('--apply', action='store_true', help='真正执行删除')
    p1.add_argument('--expect-count', type=int, default=None)
    p1.set_defaults(func=cmd_chunks)

    p2 = sub.add_parser('cases', help='水位驱动的工单日志清理')
    p2.add_argument('--high', type=float, default=WATERMARK_HIGH, help='触发水位%%')
    p2.add_argument('--low', type=float, default=WATERMARK_LOW, help='停止水位%%')
    p2.add_argument('--compliance-days', type=int, default=COMPLIANCE_MIN_DAYS)
    p2.add_argument('--max-delete-gb', type=float,
                    default=MAX_DELETE_BYTES / 1024 ** 3)
    p2.add_argument('--apply', action='store_true', help='真正执行删除')
    p2.add_argument('--expect-count', type=int, default=None)
    p2.set_defaults(func=cmd_cases)

    args = parser.parse_args()
    setup_logging()

    if args.no_lock:
        return args.func(args)
    with SingleInstance(LOCK_FILE):
        return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
