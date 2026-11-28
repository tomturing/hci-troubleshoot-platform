#!/usr/bin/env python3
"""5888 归档判重测试（纯标准库）。

覆盖：
  D1 内容相同的旧包在归档时被删除，只留规范名一份
  D2 内容不同的旧包仍按时间戳改名保留（不误删）
  D3 多份同内容副本（规范名 + 两个带戳）一次清干净
  D4 摘要缓存落盘；文件一变（size 变）摘要必须重算
  D5 删除失败时降级为"改名保留"，不丢文件
  D6 /precheck 带 sha256 精确命中；不同内容判为不存在
  D7 /precheck 不带 sha256 时只按体积给"疑似"（certain=false）
  D8 init_upload 带命中 sha256 → 409，且不创建会话
  D9 init_upload 带 force=1 → 放行
  D10 非法入参 → 400

运行：/acli/.venv/bin/python /acli/tests/test_archive_dedupe.py
"""
import hashlib
import logging
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

# ---- 必须在 import app 之前设置（两个模块都在模块级读环境变量）----
BASE = tempfile.mkdtemp(prefix='acli_dedupe_')
TMP = os.path.join(BASE, 'chunks')
SES = os.path.join(BASE, 'sessions')
UP = os.path.join(BASE, 'upload')
for d in (TMP, SES, UP):
    os.makedirs(d, exist_ok=True)

os.environ['ACLI_TEMP_FOLDER'] = TMP
os.environ['ACLI_SESSION_STORE'] = SES
os.environ['ACLI_UPLOAD_FOLDER'] = UP
os.environ.pop('ACLI_UPLOAD_TOKEN', None)
os.environ.pop('ACLI_P1_TICKET_SECRET', None)

sys.path.insert(0, '/acli')
os.chdir(BASE)

import app as app_module  # noqa: E402
import archive_index  # noqa: E402
import process_upload as pu  # noqa: E402

logging.disable(logging.CRITICAL)  # 测试不往 /acli/logs 刷生产日志

CLIENT = app_module.app.test_client()
CASE = 'Q2026110600493'
FILENAME = CASE + '.zip'
# 归档目录按"文件名去掉 .zip"分组，与上传会话 id 无关
CASE_DIR = os.path.join(UP, CASE)
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print('  PASS:', msg)
    else:
        failed += 1
        print('  FAIL:', msg)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, data):
    with open(path, 'wb') as f:
        f.write(data)


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def reset():
    """每个用例从空工单目录开始，避免用例之间互相污染。"""
    shutil.rmtree(CASE_DIR, ignore_errors=True)
    os.makedirs(CASE_DIR, exist_ok=True)


def names():
    """只看归档 zip，摘要缓存是隐藏文件、不参与断言。"""
    return sorted(n for n in os.listdir(CASE_DIR) if n.lower().endswith('.zip'))


def archive(uid, payload, declare=None):
    """直接调 process()：假会话 + 假合片，绕开 HTTP。"""
    merged = os.path.join(TMP, uid + '.merged')
    write(merged, payload)
    session = {
        'filename': FILENAME,
        'file_size': len(payload) if declare is None else declare,
        'chunks': {},
        'status': 'pending',
    }
    pu.load_session = lambda _uid: dict(session)
    pu.merge_chunks = lambda _s, _u: (merged, sha(payload), 'md5-fake')
    return pu.process(uid)


OLD = b'OLD-PACKAGE-' * 500
NEW = b'NEW-PACKAGE-' * 700

# ---- D1 ----
print('D1 同内容旧包被删除')
reset()
write(os.path.join(CASE_DIR, FILENAME), OLD)
check(archive('d1', OLD), '归档成功')
check(names() == [FILENAME], f'目录只剩规范名：{names()}')
check(read(os.path.join(CASE_DIR, FILENAME)) == OLD, '规范名内容正确')

# ---- D2 ----
print('D2 不同内容保留历史')
reset()
write(os.path.join(CASE_DIR, FILENAME), OLD)
check(archive('d2', NEW), '归档成功')
n2 = names()
check(len(n2) == 2, f'共两份：{n2}')
check(read(os.path.join(CASE_DIR, FILENAME)) == NEW, '规范名 = 新包')
kept = [n for n in n2 if n != FILENAME]
check(len(kept) == 1, f'旧包带时间戳保留：{kept}')
check(read(os.path.join(CASE_DIR, kept[0])) == OLD, '旧包内容不变')

# ---- D3 ----
print('D3 多份同内容一次清干净')
reset()
write(os.path.join(CASE_DIR, CASE + '-20261106113702.zip'), OLD)
write(os.path.join(CASE_DIR, CASE + '-20261106155217.zip'), OLD)
check(archive('d3', OLD), '归档成功')
check(names() == [FILENAME], f'只剩规范名：{names()}')

# ---- D4 ----
print('D4 摘要缓存')
cache_path = os.path.join(CASE_DIR, archive_index.DIGEST_CACHE_NAME)
check(os.path.isfile(cache_path), '缓存文件已生成')
cache = archive_index.load_cache(CASE_DIR)
entry = cache.get(FILENAME) or {}
check(entry.get('sha256') == sha(OLD), '缓存 sha256 正确')
check(entry.get('size') == len(OLD), '缓存 size 正确')
check(isinstance(entry.get('mtime_ns'), int) and entry['mtime_ns'] > 0, '缓存带纳秒 mtime')

write(os.path.join(CASE_DIR, FILENAME), OLD + b'x')
check(archive_index.sha256_of(os.path.join(CASE_DIR, FILENAME), cache) == sha(OLD + b'x'),
      'size 变化后重算摘要')

# ---- D5 ----
print('D5 删除失败降级为改名保留')
reset()
write(os.path.join(CASE_DIR, FILENAME), OLD)


def boom(path, *a, **kw):
    raise OSError('permission denied')


with patch('process_upload.os.remove', boom):
    check(archive('d5', OLD), '归档仍然成功（不因删不掉旧包而失败）')
check(len(names()) == 2, f'旧包被保留：{names()}')
check(read(os.path.join(CASE_DIR, FILENAME)) == OLD, '规范名 = 新包')

# ---- D6 ----
print('D6 /precheck 精确命中')
reset()
write(os.path.join(CASE_DIR, FILENAME), OLD)
r = CLIENT.post('/precheck', data={
    'filename': FILENAME, 'file_size': str(len(OLD)), 'sha256': sha(OLD)})
j = r.get_json()
check(r.status_code == 200 and j['found'] is True, '命中 found=true')
check(j['match'] == 'sha256' and j['certain'] is True, 'match=sha256/certain=true')
check(j['stored_name'] == FILENAME, '回传真实文件名')

r = CLIENT.post('/precheck', data={
    'filename': FILENAME, 'file_size': str(len(NEW)), 'sha256': sha(NEW)})
check(r.get_json()['found'] is False, '不同内容判为不存在')

# ---- D7 ----
print('D7 /precheck 只按体积')
r = CLIENT.post('/precheck', data={'filename': FILENAME, 'file_size': str(len(OLD))})
j = r.get_json()
check(j['found'] is True and j['match'] == 'size', '体积命中 match=size')
check(j['certain'] is False, 'certain=false（不可据此拒收）')

r = CLIENT.post('/precheck', data={'filename': FILENAME, 'file_size': '123'})
check(r.get_json()['found'] is False, '体积不同判为不存在')

# ---- D8 ----
print('D8 init_upload 拦截重复')
before = set(os.listdir(SES))
r = CLIENT.post('/init_upload', data={
    'filename': FILENAME, 'file_size': str(len(OLD)), 'sha256': sha(OLD)})
check(r.status_code == 409, f'返回 409，实际 {r.status_code}')
check(r.get_json().get('error') == 'duplicate_archive', 'error=duplicate_archive')
check(set(os.listdir(SES)) == before, '未创建上传会话')

# ---- D9 ----
print('D9 force=1 放行')
r = CLIENT.post('/init_upload', data={
    'filename': FILENAME, 'file_size': str(len(OLD)), 'sha256': sha(OLD), 'force': '1'})
check(r.status_code == 200, f'返回 200，实际 {r.status_code}')
check(bool(r.get_json().get('upload_id')), '返回 upload_id')

# ---- D10 ----
print('D10 非法入参')
r = CLIENT.post('/precheck', data={'filename': 'not-a-ticket.zip', 'file_size': '1'})
check(r.status_code == 400, f'非法文件名 400，实际 {r.status_code}')
r = CLIENT.post('/init_upload', data={
    'filename': FILENAME, 'file_size': str(len(OLD)), 'sha256': 'zz'})
check(r.status_code == 400, f'非法 sha256 400，实际 {r.status_code}')

print(f'\n---- 结果: {passed} passed, {failed} failed ----')
sys.exit(0 if failed == 0 else 1)
