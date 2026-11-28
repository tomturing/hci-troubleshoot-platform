#!/usr/bin/env python3
"""10GB 大文件端到端测试（流式、确定性生成，避免内存爆炸）。

验证：大文件并发分片上传 -> 会话并发竞态不丢片 -> 后台合片 -> 整体 MD5 一致。
不一次性把 10GB 载入内存：分片用固定种子派生（确定性、类随机），
整体期望 MD5 与最终文件 MD5 均流式计算。

运行：/acli/.venv/bin/python /acli/tests/test_large_file.py
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

BASE = '/tmp/acli_test_large_' + uuid.uuid4().hex[:8]
TMP = os.path.join(BASE, 'chunks')
SES = os.path.join(BASE, 'sessions')
UP = os.path.join(BASE, 'upload')
for d in (TMP, SES, UP):
    os.makedirs(d, exist_ok=True)

# 把分片/会话/归档全部指向临时目录，不碰生产 /sf/log
os.environ['ACLI_TEMP_FOLDER'] = TMP
os.environ['ACLI_SESSION_STORE'] = SES
os.environ['ACLI_UPLOAD_FOLDER'] = UP

sys.path.insert(0, '/acli')
os.chdir('/acli')
import app as app_module  # noqa: E402

app = app_module.app
app.config['TESTING'] = True

PORT = 5898
BASE_URL = f'http://127.0.0.1:{PORT}'
INIT_URL = BASE_URL + '/init_upload'
CHUNK_URL = BASE_URL + '/upload_chunk'
ROOT_URL = BASE_URL + '/'

CHUNK = 10 * 1024 * 1024          # 10 MiB
SIZE = 10 * 1024 * 1024 * 1024    # 10 GiB（整除 CHUNK）
N = SIZE // CHUNK
SEED = b'acli-large-file-2026'


def gen_block(i):
    """确定性分片内容：sha256(seed+i) 派生填充到 CHUNK 字节（类随机、可重算）。"""
    h = hashlib.sha256(SEED + str(i).encode()).digest()  # 32 bytes
    reps = CHUNK // len(h)
    rem = CHUNK % len(h)
    return h * reps + h[:rem]


def expected_md5():
    m = hashlib.md5()
    for i in range(N):
        m.update(gen_block(i))
    return m.hexdigest()


def _multipart(fields, files):
    boundary = '----acli_large_boundary'
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
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def do_init(filename, size):
    s, b = post_multipart(INIT_URL, {'filename': filename, 'file_size': str(size)}, {})
    try:
        j = json.loads(b)
    except Exception:
        j = {}
    return s, j


def do_chunk(uid, idx, total, chunk):
    s, b = post_multipart(
        CHUNK_URL,
        {'upload_id': uid, 'chunk_index': str(idx), 'chunk_total': str(total)},
        {'chunk': ('c.bin', chunk)})
    try:
        j = json.loads(b)
    except Exception:
        j = {}
    return s, j


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


def wait_target(filename, timeout=1200):
    """在归档目录递归查找目标文件（不依赖子目录命名规则）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for root, _dirs, files in os.walk(UP):
            if filename in files:
                return os.path.join(root, filename)
        time.sleep(0.5)
    return None


def file_md5(path):
    m = hashlib.md5()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(16 * 1024 * 1024), b''):
            m.update(blk)
    return m.hexdigest()


if __name__ == '__main__':
    print("基目录:", BASE)
    print(f"文件大小: {SIZE} bytes ({SIZE / 1024 ** 3:.2f} GiB), 分片数: {N}")
    if not start_server():
        print("服务器启动失败")
        sys.exit(2)

    fn = 'Q2026090900100.zip'
    t_total0 = time.time()

    s, j = do_init(fn, SIZE)
    print("init:", s, j.get('upload_id'))
    uid = j.get('upload_id')

    print("期望 MD5 计算中(流式)...")
    exp = expected_md5()
    print("期望 MD5:", exp)

    print(f"并发上传 {N} 片 (workers=8)...")
    t_up0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(do_chunk, uid, i, N, gen_block(i)) for i in range(N)]
        res = [f.result() for f in futs]
    completes = sum(1 for x in res if x[1].get('status') == 'complete')
    bad = [x for x in res if x[0] != 200]
    print(f"上传完成: 用时 {time.time() - t_up0:.1f}s, complete数={completes}, 非200响应={len(bad)}")

    path = wait_target(fn)
    if not path:
        print("FAIL: 后台未在超时内生成归档")
        sys.exit(1)
    actual_size = os.path.getsize(path)
    print(f"归档大小: {actual_size} (期望 {SIZE})")

    print("计算实际 MD5 (流式)...")
    act = file_md5(path)
    print("实际 MD5:", act)

    ok = (actual_size == SIZE) and (act == exp) and (completes == 1) and (len(bad) == 0)
    print("=== RESULT: %s ===" % ("PASS" if ok else "FAIL"))
    print(f"大小一致={actual_size == SIZE}  MD5一致={act == exp}  complete恰好1={completes == 1}  全200={len(bad) == 0}")
    print("总耗时 %.1fs" % (time.time() - t_total0))
    print("测试数据位于:", BASE, "(清理: rm -rf", BASE + ")")
    sys.exit(0 if ok else 1)
