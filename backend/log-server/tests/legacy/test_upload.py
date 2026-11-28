#!/usr/bin/env python3
"""acli 上传服务集成测试（纯标准库，使用 .venv 的 python 运行）。

覆盖：
  T1 默认无鉴权：上传成功 + 后台合片内容一致
  T2 并发 3 片上传，合片正确（MD5 一致）
  T3 高并发压力（竞态核心）：30 片 / 并发 12，跑 3 轮，每轮分片计数完整且合片正确
  T4 update_session_chunk 重复分片不重复累加 uploaded_size（并发重试安全）
  T5 启用 ACLI_UPLOAD_TOKEN 后：无 token 401 / 有 token 200 / 首页免鉴权且注入 token

运行：/acli/.venv/bin/python /acli/tests/test_upload.py
"""
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

# ---- 在 import app 之前设置测试路径（app.py 模块级读取这些环境变量）----
BASE = '/tmp/acli_test_' + uuid.uuid4().hex[:8]
TMP = os.path.join(BASE, 'chunks')
SES = os.path.join(BASE, 'sessions')
UP = os.path.join(BASE, 'upload')
for d in (TMP, SES, UP):
    os.makedirs(d, exist_ok=True)

os.environ['ACLI_TEMP_FOLDER'] = TMP
os.environ['ACLI_SESSION_STORE'] = SES
os.environ['ACLI_UPLOAD_FOLDER'] = UP

sys.path.insert(0, '/acli')
os.chdir('/acli')

import app as app_module  # noqa: E402

app = app_module.app
app.config['TESTING'] = True

PORT = 5899
BASE_URL = f'http://127.0.0.1:{PORT}'
INIT_URL = BASE_URL + '/init_upload'
CHUNK_URL = BASE_URL + '/upload_chunk'
ROOT_URL = BASE_URL + '/'

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS:", msg)
    else:
        failed += 1
        print("  FAIL:", msg)


def md5(b):
    return hashlib.md5(b).hexdigest()


def make_data(n):
    return os.urandom(n)


def wait_target(ticket, filename, timeout=30):
    path = os.path.join(UP, ticket, filename)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(path):
            return path
        time.sleep(0.2)
    return None


def _multipart(fields, files):
    boundary = '----acli_test_boundary'
    parts = []
    for k, v in fields.items():
        parts.append(('--' + boundary).encode())
        parts.append((f'Content-Disposition: form-data; name="{k}"').encode())
        parts.append(b'')
        parts.append(str(v).encode())
    for k, (fname, data) in files.items():
        parts.append(('--' + boundary).encode())
        parts.append((f'Content-Disposition: form-data; name="{k}"; filename="{fname}"').encode())
        parts.append(b'Content-Type: application/octet-stream')
        parts.append(b'')
        parts.append(data)
    parts.append(('--' + boundary + '--').encode())
    parts.append(b'')
    return b'\r\n'.join(parts), 'multipart/form-data; boundary=' + boundary


def post_multipart(url, fields, files, headers=None):
    body, ctype = _multipart(fields, files)
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', ctype)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def do_init(filename, size, token=None):
    h = {'Authorization': 'Bearer ' + token} if token else None
    status, body = post_multipart(INIT_URL, {'filename': filename, 'file_size': str(size)}, {}, h)
    try:
        j = json.loads(body)
    except Exception:
        j = {}
    return status, j


def do_chunk(upload_id, idx, total, chunk, token=None):
    h = {'Authorization': 'Bearer ' + token} if token else None
    status, body = post_multipart(
        CHUNK_URL,
        {'upload_id': upload_id, 'chunk_index': str(idx), 'chunk_total': str(total)},
        {'chunk': ('c.bin', chunk)}, h)
    try:
        j = json.loads(body)
    except Exception:
        j = {}
    return status, j


def start_server():
    t = threading.Thread(
        target=lambda: app.run(host='127.0.0.1', port=PORT, threaded=True,
                               debug=False, use_reloader=False),
        daemon=True)
    t.start()
    for _ in range(100):
        try:
            with urllib.request.urlopen(ROOT_URL, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def test_auth_off():
    print("[T1] 默认无鉴权：上传成功 + 合片一致")
    data = make_data(1)
    fn = 'Q2026090900001.zip'
    status, j = do_init(fn, len(data))
    check(status == 200, f'init 返回 200 (got {status})')
    uid = j.get('upload_id')
    check(bool(uid), '返回 upload_id')
    s2, j2 = do_chunk(uid, 0, 1, data)
    check(s2 == 200, f'chunk 返回 200 (got {s2})')
    check(j2.get('status') == 'complete', '单片即 complete')
    path = wait_target('Q2026090900001', fn)
    check(path is not None, '后台进程生成归档文件')
    if path:
        with open(path, 'rb') as f:
            got = f.read()
        check(got == data, f'归档内容与原文件一致 (MD5={md5(data)})')


def test_concurrent_merge():
    print("[T2] 并发 3 片上传，合片正确")
    cs = 10 * 1024 * 1024
    data = make_data(25 * 1024 * 1024)
    chunks = [data[0:cs], data[cs:2 * cs], data[2 * cs:]]
    fn = 'Q2026090900002.zip'
    status, j = do_init(fn, len(data))
    uid = j.get('upload_id')
    results = [None] * 3
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(do_chunk, uid, i, 3, chunks[i]) for i in range(3)]
        for i, f in enumerate(futs):
            results[i] = f.result()
    completes = sum(1 for x in results if x[1].get('status') == 'complete')
    check(completes == 1, f'恰好一个 complete (got {completes})')
    path = wait_target('Q2026090900002', fn)
    check(path is not None, '归档文件生成')
    if path:
        with open(path, 'rb') as f:
            got = f.read()
        check(got == data, f'合片内容与原文件一致 (MD5={md5(data)})')


def test_stress_no_loss():
    print("[T3] 高并发压力(竞态): 30片/并发12, 3轮")
    for rnd in range(3):
        cs = 1024 * 1024
        n = 30
        data = make_data(n * cs)
        chunks = [data[i * cs:(i + 1) * cs] for i in range(n)]
        fn = f'Q20260909{rnd + 10:05d}.zip'
        status, j = do_init(fn, len(data))
        uid = j.get('upload_id')
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(do_chunk, uid, i, n, chunks[i]) for i in range(n)]
            res = [f.result() for f in futs]
        completes = sum(1 for x in res if x[1].get('status') == 'complete')
        check(completes == 1, f'round{rnd}: 恰好一个 complete (got {completes})')
        path = wait_target(f'Q20260909{rnd + 10:05d}', fn)
        check(path is not None, f'round{rnd}: 归档文件生成')
        if path:
            with open(path, 'rb') as f:
                got = f.read()
            check(got == data, f'round{rnd}: 合片内容一致 MD5={md5(data)}')


def test_retry_no_double_count():
    print("[T4] 重复分片不重复累加 uploaded_size")
    fn = 'Q2026090900099.zip'
    status, j = do_init(fn, 100)
    uid = j.get('upload_id')
    cpath = os.path.join(TMP, f'{uid}_chunk_0.part')
    with open(cpath, 'wb') as f:
        f.write(b'x' * 100)
    s1, all1 = app_module.update_session_chunk(uid, 0, cpath, 2)
    s2, all2 = app_module.update_session_chunk(uid, 0, cpath, 2)
    check(s1 is not None and s2 is not None, 'update 返回会话数据')
    check(s2['uploaded_size'] == 100, '重复分片 uploaded_size 不翻倍 (got {})'.format(s2['uploaded_size']))
    check(len(s2['chunks']) == 1, 'chunks 仅 1 个键')


def test_auth_required():
    print("[T5] 启用鉴权: 无token 401 / 有token 200 / 首页注入token")
    app_module.AUTH_TOKEN = 'secret-token'
    fn = 'Q2026090900003.zip'
    s, j = do_init(fn, 1)  # 无 token
    check(s == 401, f'无token init 返回 401 (got {s})')
    s2, j2 = do_init(fn, 1, token='secret-token')
    check(s2 == 200, f'有token init 返回 200 (got {s2})')
    # 首页免鉴权 + 注入 token
    try:
        with urllib.request.urlopen(ROOT_URL, timeout=5) as r:
            html = r.read()
        check(r.status == 200, '首页免鉴权 200')
        check(b'upload-token' in html, '页面注入 upload-token meta')
    except Exception as e:
        check(False, f'首页请求异常: {e}')
    app_module.AUTH_TOKEN = ''


if __name__ == '__main__':
    print("测试基目录:", BASE)
    if not start_server():
        print("服务器启动失败")
        sys.exit(2)
    test_auth_off()
    test_concurrent_merge()
    test_stress_no_loss()
    test_retry_no_double_count()
    test_auth_required()
    print(f"\n=== RESULT: passed={passed} failed={failed} ===")
    print("测试数据位于:", BASE, "(如需清理可手动 rm -rf)")
    sys.exit(1 if failed else 0)
