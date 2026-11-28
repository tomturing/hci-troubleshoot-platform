#!/usr/bin/env python3
"""断点续传回归测试（纯标准库，用 .venv 的 python 运行）。

覆盖：
  T1 首次 init：新建会话、received_chunks 为空
  T2 传 2 片后重新 init：复用同一 upload_id，回传已收分片与字节数
  T3 重复上传同一分片：uploaded_size 不重复累加
  T4 文件 mtime 变化：视为重新收集，新建会话（不接错包）
  T5 收满全部分片：返回 status=complete，后台派发一次
  T6 已有 done 会话 + 归档文件存在：init 直接返回 already_done
  T7 chunk_total 与会话不一致：409，要求重新 init
  T8 会话里的分片文件被清理：按剩余分片回传，不漏算
  T9 precheck 也会带出 resume 信息

运行：/acli/.venv/bin/python /acli/tests/test_resume.py
"""
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

# ---- 在 import app 之前设置测试路径（app.py 模块级读取这些环境变量）----
BASE = '/tmp/acli_resume_test_' + uuid.uuid4().hex[:8]
TMP = os.path.join(BASE, 'chunks')
SES = os.path.join(BASE, 'sessions')
UP = os.path.join(BASE, 'upload')
for d in (TMP, SES, UP):
    os.makedirs(d, exist_ok=True)

os.environ['ACLI_TEMP_FOLDER'] = TMP
os.environ['ACLI_SESSION_STORE'] = SES
os.environ['ACLI_UPLOAD_FOLDER'] = UP
os.environ.pop('ACLI_UPLOAD_TOKEN', None)
os.environ.pop('ACLI_P1_REQUIRE_AUTH', None)

APP_DIR = os.environ.get('ACLI_APP_DIR', '/acli')
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

import app as app_module  # noqa: E402

# 把分片切小，测试不必造 20MB 数据：file_size=10 -> 3 片（4/4/2）
app_module.CHUNK_SIZE = 4

app = app_module.app
app.config['TESTING'] = True

# 合片交给独立进程，测试里只记录"派发了一次"
dispatched = []
app_module.subprocess.Popen = lambda *a, **kw: dispatched.append(a)

PORT = 5898
BASE_URL = f'http://127.0.0.1:{PORT}'
INIT_URL = BASE_URL + '/init_upload'
CHUNK_URL = BASE_URL + '/upload_chunk'
PRECHECK_URL = BASE_URL + '/precheck'
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


def _multipart(fields, files):
    boundary = '----acli_resume_boundary'
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


def post_multipart(url, fields, files=None):
    body, ctype = _multipart(fields, files or {})
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', ctype)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'raw': raw}


def do_init(filename, size, mtime='1700000000000', extra=None):
    fields = {'filename': filename, 'file_size': str(size), 'client_mtime': mtime}
    if extra:
        fields.update(extra)
    return post_multipart(INIT_URL, fields)


def do_chunk(upload_id, idx, total, data):
    return post_multipart(CHUNK_URL,
                          {'upload_id': upload_id,
                           'chunk_index': str(idx),
                           'chunk_total': str(total)},
                          {'chunk': ('c.bin', data)})


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


# 固定文件名与内容：file_size=10，3 片（4/4/2）
FN = 'Q2026110900777.zip'
SIZE = 10
P0, P1, P2 = b'AAAA', b'BBBB', b'CC'
MTIME = '1700000000000'


def test_first_init():
    print("[T1] 首次 init：新建会话，无已收分片")
    st, j = do_init(FN, SIZE, MTIME)
    check(st == 200, f'init 返回 200（实际 {st}）')
    check(j.get('resumed') is False, 'resumed=false')
    check(j.get('received_chunks') == [], 'received_chunks 为空')
    check(j.get('chunk_total') == 3, 'chunk_total=3（实际 {}）'.format(j.get('chunk_total')))
    check(bool(j.get('upload_id')), '返回 upload_id')
    return j


def test_resume_after_two_chunks(init_j):
    print("[T2] 传 2 片后重新 init：复用会话并回传已收分片")
    up = init_j['upload_id']
    st, _ = do_chunk(up, 0, 3, P0)
    check(st == 200, '第 0 片 200')
    st, _ = do_chunk(up, 1, 3, P1)
    check(st == 200, '第 1 片 200')

    st, j = do_init(FN, SIZE, MTIME)
    check(st == 200, '再次 init 200')
    check(j.get('upload_id') == up, '复用同一 upload_id')
    check(j.get('resumed') is True, 'resumed=true')
    check(j.get('received_chunks') == [0, 1], 'received_chunks=[0,1]（实际 {}）'.format(j.get('received_chunks')))
    check(j.get('received_bytes') == 8, 'received_bytes=8（实际 {}）'.format(j.get('received_bytes')))


def test_duplicate_chunk(upload_id):
    print("[T3] 重复上传同一分片：不重复累加")
    st, _ = do_chunk(upload_id, 0, 3, P0)
    check(st == 200, '重复第 0 片 200')
    _, j = do_init(FN, SIZE, MTIME)
    check(j.get('received_bytes') == 8, 'uploaded_size 仍为 8（实际 {}）'.format(j.get('received_bytes')))


def test_mtime_changed_new_session(old_id):
    print("[T4] mtime 变化：视为重新收集，新建会话")
    st, j = do_init(FN, SIZE, '1800000000000')
    check(st == 200, 'init 200')
    check(j.get('upload_id') != old_id, 'upload_id 与旧会话不同')
    check(j.get('received_chunks') == [], '新会话没有已收分片')


def test_complete_dispatches(upload_id):
    print("[T5] 收满全部分片：返回 complete 且只派发一次")
    st, j = do_chunk(upload_id, 2, 3, P2)
    check(st == 200, '最后一片 200')
    check(j.get('status') == 'complete', 'status=complete（实际 {}）'.format(j.get('status')))
    check(len(dispatched) == 1, f'后台合片派发 1 次（实际 {len(dispatched)}）')


def test_already_done():
    print("[T6] 已归档完成的会话：init 直接返回 already_done")
    target = os.path.join(UP, 'Q2026110900777', 'Q2026110900777.zip')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'wb') as f:
        f.write(b'A' * 10)
    done_id = str(uuid.uuid4())
    session = {
        'upload_id': done_id,
        'filename': FN,
        'temp_filepath': os.path.join(TMP, done_id + '.part'),
        'file_size': SIZE,
        'uploaded_size': SIZE,
        'chunks': {},
        'status': 'done',
        'target_path': target,
        'digest': {'sha256': 'x' * 64},
        'start_time': time.time(),
        'last_updated': app_module.datetime.now().isoformat(),
    }
    with open(os.path.join(SES, done_id + '.json'), 'w') as f:
        json.dump(session, f)

    st, j = do_init(FN, SIZE, '1900000000000')
    check(st == 200, 'init 200')
    check(j.get('already_done') is True, 'already_done=true')
    check(j.get('upload_id') == done_id, '回传 done 会话的 id')


def test_chunk_total_mismatch(upload_id):
    print("[T7] chunk_total 与会话不一致：409")
    st, j = do_chunk(upload_id, 0, 99, P0)
    check(st == 409, f'返回 409（实际 {st}）')
    check(j.get('expected') == 3, 'expected=3（实际 {}）'.format(j.get('expected')))


def test_missing_chunk_file():
    print("[T8] 分片文件被清理：按剩余分片回传")
    fn = 'Q2026110900888.zip'
    _, j = do_init(fn, SIZE, MTIME)
    up = j['upload_id']
    do_chunk(up, 0, 3, P0)
    do_chunk(up, 1, 3, P1)
    os.remove(os.path.join(TMP, f'{up}_chunk_1.part'))

    _, j2 = do_init(fn, SIZE, MTIME)
    check(j2.get('received_chunks') == [0], '只剩 [0]（实际 {}）'.format(j2.get('received_chunks')))
    check(j2.get('received_bytes') == 4, 'received_bytes=4（实际 {}）'.format(j2.get('received_bytes')))


def test_legacy_session_without_mtime():
    print("[T10] 旧会话没有 client_mtime：按文件名+体积回退续传")
    fn = 'Q2026110900770.zip'
    up = str(uuid.uuid4())
    chunk0 = os.path.join(TMP, f'{up}_chunk_0.part')
    with open(chunk0, 'wb') as f:
        f.write(P0)
    session = {
        'upload_id': up,
        'filename': fn,
        'temp_filepath': os.path.join(TMP, up + '.part'),
        'file_size': SIZE,
        'uploaded_size': len(P0),
        'chunks': {'0': chunk0},
        'start_time': time.time(),
        'last_updated': app_module.datetime.now().isoformat(),
    }
    with open(os.path.join(SES, up + '.json'), 'w') as f:
        json.dump(session, f)

    st, j = do_init(fn, SIZE, MTIME)
    check(st == 200, 'init 200')
    check(j.get('upload_id') == up, '复用旧会话（无 mtime 字段）')
    check(j.get('received_chunks') == [0], '回传已收 [0]')


def test_precheck_resume():
    print("[T9] precheck 带出 resume 信息")
    fn = 'Q2026110900999.zip'
    _, j = do_init(fn, SIZE, MTIME)
    do_chunk(j['upload_id'], 0, 3, P0)

    st, p = post_multipart(PRECHECK_URL, {'filename': fn,
                                          'file_size': str(SIZE),
                                          'client_mtime': MTIME})
    check(st == 200, 'precheck 200')
    resume = p.get('resume') or {}
    check(resume.get('received_chunks') == [0], 'precheck 回传已收分片')
    check(resume.get('upload_id') == j['upload_id'], '指向同一会话')


def main():
    print("== 断点续传回归测试 ==")
    print("临时目录:", BASE)
    if not start_server():
        print("服务未启动，退出")
        return 1

    init_j = test_first_init()
    test_resume_after_two_chunks(init_j)
    test_duplicate_chunk(init_j['upload_id'])
    test_mtime_changed_new_session(init_j['upload_id'])
    test_complete_dispatches(init_j['upload_id'])
    test_already_done()

    # 单独造一个会话，测 chunk_total 不一致（用 T4 新建的那个）
    _, fresh = do_init('Q2026110900666.zip', SIZE, MTIME)
    test_chunk_total_mismatch(fresh['upload_id'])

    test_missing_chunk_file()
    test_legacy_session_without_mtime()
    test_precheck_resume()

    print(f"\n结果: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    rc = main()
    shutil.rmtree(BASE, ignore_errors=True)
    sys.exit(rc)
