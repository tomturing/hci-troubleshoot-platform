#!/usr/bin/env python3
"""归档命名策略单测（纯标准库，运行：/acli/.venv/bin/python /acli/tests/test_archive_naming.py）

覆盖 2026-11-06 的修订："规范名始终指向最新包"。
  A1 `_stamp_existing_archive` 用旧文件 mtime 生成时间戳，且原文件被移走
  A2 同一工单重传：规范名内容 = 新包，历史包带时间戳保留（旧行为会反过来）
  A3 时间戳撞车（同秒重传）追加 -1 后缀，不覆盖已有历史
  A4 合片大小不符时不夺走规范名（坏包不能把旧包挤成时间戳名）
"""
import logging
import os
import sys
import tempfile
import time

BASE = tempfile.mkdtemp(prefix="acli_arch_")
UP = os.path.join(BASE, "upload")
TMP = os.path.join(BASE, "chunks")
SES = os.path.join(BASE, "sessions")
for d in (UP, TMP, SES):
    os.makedirs(d, exist_ok=True)

os.environ["ACLI_UPLOAD_FOLDER"] = UP
os.environ["ACLI_SESSION_STORE"] = SES
os.environ["ACLI_TEMP_FOLDER"] = TMP

sys.path.insert(0, "/acli")
import process_upload as pu  # noqa: E402

# A4 会故意触发一次失败日志，静音后输出更干净（不影响断言）
logging.disable(logging.CRITICAL)

CASE = "Q2026110600493"
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS:", msg)
    else:
        failed += 1
        print("  FAIL:", msg)


def write(path, payload):
    with open(path, "wb") as f:
        f.write(payload)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def run_process(uid, declare_size, payload):
    """直接调 process()，用假的会话与合片结果，绕开 HTTP 层。"""
    merged = os.path.join(TMP, uid + ".merged")
    write(merged, payload)
    session = {
        "filename": CASE + ".zip",
        "file_size": declare_size,
        "chunks": {},
        "status": "pending",
    }
    pu.load_session = lambda _uid: dict(session)
    pu.merge_chunks = lambda _s, _u: (merged, "sha-fake", "md5-fake")
    return pu.process(uid)


def case_dir():
    return os.path.join(UP, CASE)


def entries():
    """只看归档 zip：.digests.json 是摘要缓存，不参与断言。"""
    return sorted(n for n in os.listdir(case_dir()) if n.lower().endswith(".zip"))


# ---- A1 ----
print("A1 旧包改名用自身 mtime")
p = os.path.join(BASE, "a1")
os.makedirs(p)
old = os.path.join(p, CASE + ".zip")
write(old, b"old")
stamp_src = time.mktime(time.strptime("20261106090405", "%Y%m%d%H%M%S"))
os.utime(old, (stamp_src, stamp_src))
moved = pu._stamp_existing_archive(old)
check(not os.path.exists(old), "原规范名文件已被移走")
check(moved.endswith("-20261106090405.zip"), f"时间戳来自旧文件 mtime：{os.path.basename(moved)}")
check(read(moved) == b"old", "历史包内容未变")

# ---- A2 ----
print("A2 重传后规范名指向最新包")
first = b"OLD-PACKAGE-" * 100
second = b"NEW-PACKAGE-" * 200
check(run_process("u1", len(first), first), "首次归档成功")
check(entries() == [CASE + ".zip"], f"首次只有规范名：{entries()}")
# 把第一次的归档时间压到过去，避免与下面同秒撞车
t0 = time.mktime(time.strptime("20261106110400", "%Y%m%d%H%M%S"))
os.utime(os.path.join(case_dir(), CASE + ".zip"), (t0, t0))

check(run_process("u2", len(second), second), "重传归档成功")
names = entries()
check(len(names) == 2, f"共两份：{names}")
check(read(os.path.join(case_dir(), CASE + ".zip")) == second, "规范名内容 = 新包")
check(CASE + "-20261106110400.zip" in names, "旧包按 mtime 改名保留")
check(read(os.path.join(case_dir(), CASE + "-20261106110400.zip")) == first, "旧包内容 = 首包")
# 归档时间不能倒退：规范名 mtime 应更新
check(
    os.path.getmtime(os.path.join(case_dir(), CASE + ".zip")) >= t0,
    "规范名 mtime 已刷新为最新归档时刻",
)

# ---- A3 ----
print("A3 时间戳撞车追加序号")
p3 = os.path.join(BASE, "a3")
os.makedirs(p3)
fixed = time.mktime(time.strptime("20261107080000", "%Y%m%d%H%M%S"))
canon3 = os.path.join(p3, CASE + ".zip")
write(canon3, b"current")
os.utime(canon3, (fixed, fixed))
squat = os.path.join(p3, CASE + "-20261107080000.zip")
write(squat, b"squat")
moved2 = pu._stamp_existing_archive(canon3)
check(os.path.basename(moved2) == CASE + "-20261107080000-1.zip", f"追加 -1 后缀：{os.path.basename(moved2)}")
check(read(squat) == b"squat", "已存在的历史包未被覆盖")
canon = os.path.join(case_dir(), CASE + ".zip")

# ---- A4 ----
print("A4 坏包不得夺走规范名")
before = entries()
ok = run_process("u3", 999999, b"short")  # 声明大小与实际不符
check(ok is False, "大小不符判失败")
check(entries() == before, f"归档目录未被改动：{entries()}")
check(read(canon) == second, "规范名仍指向上一份正常包")

print(f"\n结果: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
