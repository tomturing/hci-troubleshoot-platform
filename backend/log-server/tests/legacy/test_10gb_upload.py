#!/usr/bin/env python3
"""acli 10GB 大文件端到端测试（纯标准库，复用 test_upload 框架，不碰生产）。

模拟前端真实大文件分片上传：
  - 10GB 文件按 40MB/片切分（< app.py 的 50MB 单请求上限），共 256 片
  - 并发 3 上传（贴近前端 3 并发），本地按块顺序流式计算 MD5
  - 后台合片后，比对归档文件大小(精确 10GB)与 MD5(字节级一致)

运行：/acli/.venv/bin/python /acli/tests/test_10gb_upload.py
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

# ---- 独立测试目录，避免污染生产 /sf/log ----
BASE = '/tmp/acli_10gb_' + uuid.uuid4().hex[:8]
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

PORT = 5901
BASE_URL = f'http://127.0.0.1:{PORT}'
INIT_URL = BASE_URL + '/init_upload'
CHUNK_URL = BASE_URL + '/upload_chunk'
ROOT_URL = BASE_URL + '/'

SIZE = 10 * 1024 * 1024 * 1024          # 10GB
CHUNK = 40 * 1024 * 1024                # 40MB/片 (< 50MB MAX_REQUEST_SIZE)
N = SIZE // CHUNK                       # 256 片
FN = 'Q2026091100001.zip'              # 符合 ^Q\d{13}\.zip$
TICKET = FN[:-4]                        # 归档子目录名(去 .zip)


def _multipart(fields, files):
    boundary = '----acli_10gb_boundary'
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


def post_multipart(url, fields, files):
    body, ctype = _multipart(fields, files)
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def do_init():
    s, b = post_multipart(INIT_URL, {'filename': FN, 'file_size': str(SIZE)}, {})
    try:
        j = json.loads(b)
    except Exception:
        j = {}
    return s, j


def do_chunk(uid, idx, chunk):
    s, b = post_multipart(
        CHUNK_URL,
        {'upload_id': uid, 'chunk_index': str(idx), 'chunk_total': str(N)},
        {'chunk': ('c.bin', chunk)})
    try:
        j = json.loads(b)
    except Exception:
        j = {}
    return s, j


def wait_target(timeout=300):
    path = os.path.join(UP, TICKET, FN)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(path):
            return path
        time.sleep(0.5)
    return None


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


def gen_chunk(i):
    # 每块 40MB，内容为 sha256(i) 铺满：确定性且每块不同（可检出合片顺序/错位）
    seed = hashlib.sha256(str(i).encode()).digest()
    return (seed * (CHUNK // len(seed) + 1))[:CHUNK]


def main():
    print("测试基目录:", BASE)
    if not start_server():
        print("FAIL 服务器启动失败")
        sys.exit(2)
    print(f"[BIG] 10GB 端到端: {N} 片 x {CHUNK // (1024 * 1024)}MB, 并发 3")
    s, j = do_init()
    print("init", s, "upload_id={}".format(j.get('upload_id', '')[:8]))
    if s != 200 or not j.get('upload_id'):
        print("FAIL init")
        sys.exit(1)
    uid = j['upload_id']

    m = hashlib.md5()
    t0 = time.time()
    done = 0

    def worker(i):
        chunk = gen_chunk(i)
        m.update(chunk)              # 本地按块顺序算 MD5
        st, jj = do_chunk(uid, i, chunk)
        return i, st, jj

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(worker, i) for i in range(N)]
        for f in futs:
            i, st, jj = f.result()
            if st != 200:
                print("FAIL chunk", i, st, jj)
                sys.exit(1)
            done += 1
            if done % 32 == 0:
                print(f"  uploaded {done}/{N}  ({time.time() - t0:.1f}s)")

    local_md5 = m.hexdigest()
    print(f"上传完成 本地MD5={local_md5} 耗时{time.time() - t0:.1f}s 等待后台合片...")

    path = wait_target(timeout=300)
    if not path:
        print("FAIL 归档未在 300s 内生成")
        sys.exit(1)

    am = hashlib.md5()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            am.update(blk)
    size = os.path.getsize(path)
    print(f"归档大小={size} bytes (期望 {SIZE}) 归档MD5={am.hexdigest()}")

    if am.hexdigest() == local_md5 and size == SIZE:
        print("=== RESULT: PASS (10GB 端到端 MD5 一致, 大小精确 10GB) ===")
        print("测试数据位于:", BASE, "(清理: rm -rf", BASE + ")")
        sys.exit(0)
    else:
        print("=== RESULT: FAIL (MD5 或大小不一致) ===")
        sys.exit(1)


if __name__ == '__main__':
    main()
