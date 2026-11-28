#!/usr/bin/env python3
"""回收过期上传会话（及其残留标记文件）。

为什么必须有它：
  app.py 的会话过期是"访问时惰性删除" —— 只有被 /upload_status 或 /upload_chunk
  碰到的会话才会过期；没人访问的陈旧会话永不过期。而 lifecycle_clean.py chunks 会把
  "存在会话 JSON 的分片"当作活跃上传保护起来，于是陈旧会话把分片永久锁死。
  2026-10-31 现场：/tmp/acli_upload_chunks 累积 33G，`chunks` 子命令却报
  "可清理 0 个（145 个活跃会话已保护）"，根因即此。

策略（与 app.py 的 SESSION_EXPIRE_HOURS 保持一致，不引入第二套口径）：
  - 只处理 last_updated 早于阈值的会话；解析不了的 JSON 一律跳过并告警，不猜。
  - 默认 dry-run，必须 --apply 才真正删除。
  - 只删会话 JSON 与 .lock；分片交给 lifecycle_clean.py chunks 按年龄回收，
    避免两处各写一套删除逻辑。
"""
import argparse
import contextlib
import json
import os
import sys
import time

SESSION_STORE = os.environ.get('ACLI_SESSION_STORE', '/tmp/acli_upload_sessions')
EXPIRE_HOURS = int(os.environ.get('ACLI_SESSION_EXPIRE_HOURS', '24'))


def iter_sessions():
    for name in os.listdir(SESSION_STORE):
        if not name.endswith('.json'):
            continue
        path = os.path.join(SESSION_STORE, name)
        yield name, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真正删除（默认只预览）')
    args = ap.parse_args()

    now = time.time()
    limit = EXPIRE_HOURS * 3600
    candidates = []
    skipped = 0

    for name, path in iter_sessions():
        try:
            with open(path) as f:
                data = json.load(f)
            last = data.get('last_updated')
            age = now - os.path.getmtime(path)
            if last:
                # 用文件 mtime 与 last_updated 的较大者判断，避免时钟误差导致误删
                try:
                    import datetime
                    ts = datetime.datetime.fromisoformat(last).timestamp()
                    age = min(age, now - ts)
                except Exception:
                    pass
            if age > limit:
                candidates.append((name, age))
        except Exception as e:
            skipped += 1
            print(f"WARN 跳过无法解析的会话 {name}: {e}")

    total = 0
    for name, age in candidates:
        path = os.path.join(SESSION_STORE, name)
        lock = path[:-5] + '.lock'
        with contextlib.suppress(OSError):
            total += os.path.getsize(path)
        if args.apply:
            for p in (path, lock, path + '.tmp'):
                try:
                    os.path.exists(p) and os.remove(p)
                except OSError as e:
                    print(f"WARN 删除失败 {p}: {e}")
        print(f"{'删除' if args.apply else '待删'} {name} (age={age / 3600:.1f}h)")

    print(f"可回收会话 {len(candidates)} 个（阈值 {EXPIRE_HOURS}h），跳过 {skipped} 个，"
          f"会话文件合计 {total / 1024:.0f}KB，模式={'APPLY' if args.apply else 'DRY-RUN'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
