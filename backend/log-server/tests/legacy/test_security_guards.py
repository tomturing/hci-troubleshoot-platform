#!/usr/bin/env python3
"""5888 上传侧安全加固回归测试（纯标准库 + Flask test_client，隔离目录）。

运行：/acli/.venv/bin/python /acli/tests/test_security_guards.py
覆盖：畸形产物名、工单号三层判定、历史归档豁免、precheck 只读、
      init 配额、单 IP 限流、调用链透传。
"""
import logging
import os
import sys
import uuid

BASE = '/tmp/acli_guard_' + uuid.uuid4().hex[:8]
for name in ('chunks', 'sessions', 'upload'):
    os.makedirs(os.path.join(BASE, name), exist_ok=True)

os.environ['ACLI_TEMP_FOLDER'] = os.path.join(BASE, 'chunks')
os.environ['ACLI_SESSION_STORE'] = os.path.join(BASE, 'sessions')
os.environ['ACLI_UPLOAD_FOLDER'] = os.path.join(BASE, 'upload')
os.environ['ACLI_RATE_STATE_FILE'] = os.path.join(BASE, 'rate.json')
os.environ['ACLI_RATE_INIT_MAX'] = '5'
os.environ['ACLI_RATE_PRECHECK_MAX'] = '20'
os.environ['ACLI_MAX_CASE_DIR_BYTES'] = str(10 * 1024 * 1024)

sys.path.insert(0, '/acli')
os.chdir(BASE)
import app as m  # noqa: E402
from flask import g  # noqa: E402

app = m.app
app.config['TESTING'] = True
app.logger.handlers[:] = [logging.NullHandler()]  # 不污染生产日志
client = app.test_client()

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print('  PASS:', msg)
    else:
        failed += 1
        print('  FAIL:', msg)


print('== T1 产物名严格校验 ==')
bad_names = [
    'Q1234567890123.zip\n', 'Q١٢٣٤٥٦٧٨٩٠١٢٣.zip', '../Q1234567890123.zip',
    'Q1234567890123.zip;id', 'Q1234567890123.zip$(id)',
    'Q1234567890123.zip{{7*7}}', 'Q12345678901234.zip',
    'Q1234567890123.txt', 'badname.zip', 'Q1234567890123.zip/../x',
]
for name in bad_names:
    check(not m.validate_filename(name), '拒绝畸形产物名 ' + repr(name))
check(m.validate_filename('Q2026110901688.zip'), '放行真实产物名')

print('== T2 工单号三层判定 ==')
with app.test_request_context('/init_upload', method='POST'):
    for case in ('Q9999999999999', 'Q1794151722888', 'Q1611072266479',
                 'Q1869112086901', 'Q1234567890123'):
        check(not m.validate_case_id(case + '.zip')[0], '拒绝假工单 ' + case)
    check(m.validate_case_id('Q2026110901688.zip')[0], '放行业务工单')
    check(m.validate_case_id('Q2027102700999.zip')[0], '未来日期不误伤')

legacy = os.path.join(BASE, 'upload', 'Q2026199991160')
os.makedirs(legacy, exist_ok=True)
with open(os.path.join(legacy, 'Q2026199991160.zip'), 'wb') as f:
    f.write(b'x' * (2 * 1024 * 1024))
with app.test_request_context('/init_upload', method='POST'):
    ok, mode, _ = m.validate_case_id('Q2026199991160.zip')
    check(ok and mode == 'legacy_archive', '历史真实归档豁免生效')

wl = os.path.join(BASE, 'whitelist.txt')
with open(wl, 'w', encoding='utf-8') as f:
    f.write('# 平台同步\nQ2026110901688\n')
m.TICKET_WHITELIST_FILE = wl
m._WHITELIST_CACHE.update(path=None, mtime=None, ids=None)
with app.test_request_context('/init_upload', method='POST'):
    check(m.validate_case_id('Q2026110901688.zip')[0], '白名单命中放行')
    check(not m.validate_case_id('Q2026110901999.zip')[0], '白名单未登记拒绝')
m.TICKET_WHITELIST_FILE = ''

with app.test_request_context('/init_upload', method='POST'):
    g.p1_claims = {'case_id': 'Q2026110901688'}
    check(m.validate_case_id('Q2026110901688.zip')[0], '票据 cid 一致放行')
    check(not m.validate_case_id('Q2026110901999.zip')[0], '票据 cid 不一致拒绝')

print('== T3 precheck 只读 ==')
r = client.post('/precheck', data={'filename': 'Q9999999999999.zip', 'file_size': '10'})
check(r.status_code == 400, 'precheck 拒绝假工单')
check(not os.path.isdir(os.path.join(BASE, 'upload', 'Q9999999999999')),
      'precheck 不建目录')

print('== T4 init 判定与目录配额 ==')
r = client.post('/init_upload',
                data={'filename': 'Q9999999999999.zip', 'file_size': '10'})
check(r.status_code == 400, 'init 拒绝假工单')
r = client.post('/init_upload',
                data={'filename': 'Q2026110901688.zip',
                      'file_size': str(20 * 1024 * 1024), 'force': '1'})
check(r.status_code == 413, 'init 拒绝超目录配额')
r = client.post('/init_upload',
                data={'filename': 'Q2026110901688.zip', 'file_size': '1024'})
check(r.status_code == 200, 'init 放行业务工单')

print('== T5 单 IP 限流（不限 upload_chunk） ==')
statuses = [client.post('/precheck',
                        data={'filename': 'Q2026110901688.zip',
                              'file_size': '10'}).status_code
            for _ in range(25)]
check(429 in statuses, 'precheck 超阈值返回 429')

print('== T6 唯一调用链 ==')
r = client.get('/', headers={'X-Request-ID': 'lc-guard-test-001'})
check(r.headers.get('X-Request-ID') == 'lc-guard-test-001',
      '调用链透传并回写响应头')
r = client.get('/')
check(bool(r.headers.get('X-Request-ID')), '无上游调用链时自签')

print(f'\n结果: PASS={passed} FAIL={failed}')
sys.exit(0 if failed == 0 else 1)
