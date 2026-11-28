#!/usr/bin/env python3
"""5888 P1 票据鉴权与归档核验端点测试（纯标准库）。

覆盖：
  T1 未配置票据密钥：票据通道整体停用（不静默放行）
  T2 正确票据：init_upload 通过
  T3 无凭证 → 401
  T4 签名错误 → 401 reason=bad_signature
  T5 过期 → 401 reason=expired
  T6 产物名不匹配 → 401 reason=filename_mismatch
  T7 全局 Bearer 通道不受影响（P2/手工页兼容）
  T8 全局 Bearer 错误 → 401
  T9 /archive_status 归档缺失 → found=false
  T10 /archive_status 归档存在 → size/mtime/path 正确
  T11 /archive_status 同名改存（Q...-时间戳.zip）→ 取最新一条
  T12 /archive_status 文件名非法 → 400
  T13 票据无法查看别的工单归档（filename 绑定）

运行：/acli/.venv/bin/python /acli/tests/test_p1_ticket.py
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

# ---- 必须在 import app 之前设置（app.py 模块级读取）----
BASE = '/tmp/acli_p1test_' + uuid.uuid4().hex[:8]
TMP = os.path.join(BASE, 'chunks')
SES = os.path.join(BASE, 'sessions')
UP = os.path.join(BASE, 'upload')
for d in (TMP, SES, UP):
    os.makedirs(d, exist_ok=True)

os.environ['ACLI_TEMP_FOLDER'] = TMP
os.environ['ACLI_SESSION_STORE'] = SES
os.environ['ACLI_UPLOAD_FOLDER'] = UP
os.environ['ACLI_UPLOAD_TOKEN'] = 'global' + 'f' * 58
os.environ['ACLI_P1_TICKET_SECRET'] = 'p1secret' + '0' * 56

sys.path.insert(0, '/acli')
os.chdir(BASE)

import app as app_module  # noqa: E402

CLIENT = app_module.app.test_client()
GLOBAL_TOKEN = os.environ['ACLI_UPLOAD_TOKEN']
TICKET_SECRET = os.environ['ACLI_P1_TICKET_SECRET']
PREFIX = 'p1.v1.'
CASE_ID = 'Q2026110300278'
FILENAME = CASE_ID + '.zip'

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS:", msg)
    else:
        failed += 1
        print("  FAIL:", msg)


def issue_ticket(case_id=CASE_ID, filename=FILENAME, ttl=3600, secret=None, now=None):
    """按平台侧契约签发一张票据（独立实现，顺带验证两侧算法一致）。"""
    issued = int(now if now is not None else time.time())
    payload = {
        'v': 1,
        'cid': case_id,
        'fn': filename,
        'iat': issued,
        'exp': issued + ttl,
        'jti': uuid.uuid4().hex,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    ).rstrip(b'=').decode()
    signing_input = PREFIX + encoded
    sig = hmac.new((secret or TICKET_SECRET).encode(), signing_input.encode(), hashlib.sha256).hexdigest()
    return f'{signing_input}.{sig}'


def bearer(ticket):
    return {'Authorization': 'Bearer ' + ticket}


print("\n== T1 未配置票据密钥时整体停用 ==")
saved = app_module.P1_TICKET_SECRET
app_module.P1_TICKET_SECRET = ''
claims, err = app_module.verify_p1_ticket(issue_ticket(), filename=FILENAME)
check(claims is None and err == 'disabled', 'verify_p1_ticket 返回 disabled')
app_module.P1_TICKET_SECRET = saved

print("\n== T2 正确票据可完成 init_upload ==")
resp = CLIENT.post(
    '/init_upload',
    data={'filename': FILENAME, 'file_size': '1024'},
    headers=bearer(issue_ticket()),
)
check(resp.status_code == 200, f'init_upload 200，实际 {resp.status_code}')
check(bool(resp.get_json().get('upload_id')), '返回 upload_id')
check(resp.get_json().get('chunk_size') == app_module.CHUNK_SIZE, '返回 chunk_size')

print("\n== T3 无凭证拦截 ==")
resp = CLIENT.post('/init_upload', data={'filename': FILENAME, 'file_size': '1024'})
check(resp.status_code == 401, '无凭证 401')

print("\n== T4 签名错误拦截 ==")
bad = issue_ticket(secret='x' * 64)
resp = CLIENT.post(
    '/init_upload', data={'filename': FILENAME, 'file_size': '1024'}, headers=bearer(bad)
)
check(resp.status_code == 401 and resp.get_json().get('reason') == 'bad_signature', 'bad_signature')

print("\n== T5 过期票据拦截（超出时钟宽限）==")
old = issue_ticket(ttl=60, now=int(time.time()) - 60 - 400)
resp = CLIENT.post(
    '/init_upload', data={'filename': FILENAME, 'file_size': '1024'}, headers=bearer(old)
)
check(resp.status_code == 401 and resp.get_json().get('reason') == 'expired', 'expired')

print("\n== T5b 过期但仍在宽限窗口内 → 放行 ==")
skewed = issue_ticket(ttl=60, now=int(time.time()) - 120)
resp = CLIENT.post(
    '/init_upload', data={'filename': FILENAME, 'file_size': '1024'}, headers=bearer(skewed)
)
check(resp.status_code == 200, f'时钟宽限生效，实际 {resp.status_code}')

print("\n== T6 产物名不匹配拦截 ==")
other = issue_ticket(filename='Q2026110300999.zip')
resp = CLIENT.post(
    '/init_upload', data={'filename': FILENAME, 'file_size': '1024'}, headers=bearer(other)
)
check(
    resp.status_code == 401 and resp.get_json().get('reason') == 'filename_mismatch',
    'filename_mismatch',
)

print("\n== T7 全局 Bearer 通道不受影响 ==")
resp = CLIENT.post(
    '/init_upload',
    data={'filename': FILENAME, 'file_size': '1024'},
    headers=bearer(GLOBAL_TOKEN),
)
check(resp.status_code == 200, f'P2 代传仍可 200，实际 {resp.status_code}')

print("\n== T8 全局 Bearer 错误拦截 ==")
resp = CLIENT.post(
    '/init_upload',
    data={'filename': FILENAME, 'file_size': '1024'},
    headers=bearer('a' * 64),
)
check(resp.status_code == 401, '错误全局 token 401')

print("\n== T9 归档缺失 ==")
resp = CLIENT.get('/archive_status', query_string={'filename': FILENAME}, headers=bearer(issue_ticket()))
body = resp.get_json()
check(resp.status_code == 200 and body.get('found') is False, 'found=false')
check(body.get('status') == 'missing', 'status=missing')

print("\n== T10 归档存在时回报落盘事实 ==")
case_dir = os.path.join(UP, CASE_ID)
os.makedirs(case_dir, exist_ok=True)
target = os.path.join(case_dir, FILENAME)
with open(target, 'wb') as fh:
    fh.write(b'x' * 4096)
resp = CLIENT.get('/archive_status', query_string={'filename': FILENAME}, headers=bearer(issue_ticket()))
body = resp.get_json()
check(body.get('found') is True, 'found=true')
check(body.get('size') == 4096, 'size 正确，实际 {}'.format(body.get('size')))
check(body.get('stored_name') == FILENAME, 'stored_name 正确')
check(body.get('target_path') == target, 'target_path 正确')
check(isinstance(body.get('mtime'), int), 'mtime 为整数')

print("\n== T11 同名改存取最新一条 ==")
time.sleep(0.05)
renamed = os.path.join(case_dir, CASE_ID + '-20261103120000.zip')
with open(renamed, 'wb') as fh:
    fh.write(b'y' * 8192)
resp = CLIENT.get('/archive_status', query_string={'filename': FILENAME}, headers=bearer(issue_ticket()))
body = resp.get_json()
check(body.get('stored_name') == os.path.basename(renamed), '取最新改存件')
check(body.get('size') == 8192, '大小为最新件，实际 {}'.format(body.get('size')))

print("\n== T12 非法文件名（用全局通道，避免被票据绑定先拒）==")
resp = CLIENT.get(
    '/archive_status', query_string={'filename': 'bad.zip'}, headers=bearer(GLOBAL_TOKEN)
)
check(resp.status_code == 400, f'非法文件名 400，实际 {resp.status_code}')

print("\n== T13 票据只能查自己绑定的产物 ==")
resp = CLIENT.get(
    '/archive_status',
    query_string={'filename': 'Q2026110300999.zip'},
    headers=bearer(issue_ticket()),
)
check(
    resp.status_code == 401 and resp.get_json().get('reason') == 'filename_mismatch',
    '跨工单查询被拒',
)

print(f"\n---- 结果: {passed} passed, {failed} failed ----")
sys.exit(0 if failed == 0 else 1)
