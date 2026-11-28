"""清理 2026-11-08 23:19-23:29 安全测试残留的假工单目录。

四重白名单（任一不满足即排除，默认 dry-run；加 --apply 才真正执行）：
  1) mtime 落在事件窗口内；
  2) 目录名形态为 Q + 13 位数字，或 Q + 13 位数字 + '.zip' + 换行（畸形名）；
  3) 目录内文件总体积 < 10 MiB（排除任何真实日志归档）；
  4) 目录名不符合平台发号规则（合法日历日期 + 年份 2000..当前+3）。
执行前先打包备份到 /acli/backups/，逐目录校验后再删除。
"""
import datetime
import json
import os
import re
import shutil
import sys
import tarfile

ROOT = '/sf/log'
BACKUP_DIR = '/acli/backups'
APPLY = '--apply' in sys.argv
LO, HI = (datetime.datetime(2026, 11, 8, 23, 19, 0).timestamp(),
          datetime.datetime(2026, 11, 9, 0, 0, 0).timestamp())
NAME_RE = re.compile(r'Q[0-9]{13}(\.zip\n)?\Z')
DATE_RE = re.compile(r'Q([0-9]{8})[0-9]{5}\Z')
MAX_BYTES = 10 * 1024 * 1024
MIN_YEAR = 2000
MAX_YEAR = datetime.datetime.now().year + 3


def real_case_id(name):
    """是否可能是平台真实工单号：形态 + 合法日历日期 + 合理年份。"""
    m = DATE_RE.fullmatch(name)
    if m is None:
        return False
    try:
        day = datetime.datetime.strptime(m.group(1), '%Y%m%d').date()
    except ValueError:
        return False
    return MIN_YEAR <= day.year <= MAX_YEAR


def dir_size(path):
    total = 0
    for entry in os.scandir(path):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            return -1
    return total


candidates = []
excluded = []
for name in os.listdir(ROOT):
    path = os.path.join(ROOT, name)
    if not os.path.isdir(path):
        continue
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        continue
    if not (LO <= mtime < HI):
        continue
    size = dir_size(path)
    reason = []
    if not NAME_RE.fullmatch(name):
        reason.append('名字形态不符')
    if size < 0 or size >= MAX_BYTES:
        reason.append('体积>=10MiB或不可读')
    if real_case_id(name):
        reason.append('疑似真实工单号')
    if reason:
        excluded.append((name, size, ';'.join(reason)))
    else:
        candidates.append((name, size, mtime))

candidates.sort()
print('事件窗口内目录总数 =', len(candidates) + len(excluded))
print('拟清理 =', len(candidates), ' 排除 =', len(excluded))
print('\n== 拟清理清单（逐条核对）==')
for name, size, mtime in candidates:
    stamp = datetime.datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M:%S')
    files = sorted(e.name for e in os.scandir(os.path.join(ROOT, name)))
    print(f'  {stamp} {size:9d}  {name!r:<28} 内容={files!r}')
print('\n== 排除清单（绝不删除）==')
for name, size, why in excluded:
    print(f'  {size:9d} {name!r:<28} {why}')
print('\n合计字节 =', sum(s for _, s, _ in candidates))

if not APPLY:
    print('\n[DRY-RUN] 未做任何改动。加 --apply 才会备份并删除。')
    sys.exit(0)

if not candidates:
    print('[SKIP] 无候选，退出')
    sys.exit(0)

os.makedirs(BACKUP_DIR, exist_ok=True)
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
tar_path = os.path.join(BACKUP_DIR, f'test-residue-20261108-{stamp}.tar.gz')
manifest = {
    'created_at': stamp,
    'window': ['2026-11-08 23:19:00', '2026-11-09 00:00:00'],
    'reason': '2026-11-08 安全测试/扫描产生的假工单目录，用户授权清理',
    'entries': [{'name': n, 'bytes': s,
                 'mtime': datetime.datetime.fromtimestamp(t).isoformat()}
                for n, s, t in candidates],
}
with tarfile.open(tar_path, 'w:gz') as tar:
    for name, _, _ in candidates:
        tar.add(os.path.join(ROOT, name), arcname=name)
manifest_path = tar_path.replace('.tar.gz', '.manifest.json')
with open(manifest_path, 'w', encoding='utf-8') as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print('\n备份完成:', tar_path, os.path.getsize(tar_path), 'bytes')
print('清单文件:', manifest_path)

removed = []
for name, _, _ in candidates:
    path = os.path.join(ROOT, name)
    if not os.path.isdir(path):
        continue
    # 删除前最后一次校验，防止备份与删除之间目录被换成真实归档
    if real_case_id(name) or dir_size(path) >= MAX_BYTES:
        print('[SKIP] 删除前校验未通过:', repr(name))
        continue
    shutil.rmtree(path)
    removed.append(name)
print('实际删除 =', len(removed))

left = [n for n, _, _ in candidates if os.path.isdir(os.path.join(ROOT, n))]
print('残留 =', left)
print('清理后 /sf/log 目录数 =',
      sum(1 for n in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, n))))
sys.exit(0 if not left else 1)
