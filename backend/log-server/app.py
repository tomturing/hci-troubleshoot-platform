import base64
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler

import archive_index
from flask import Flask, g, jsonify, render_template, request, send_from_directory

# 初始化Flask应用
app = Flask(__name__, static_url_path='')

# 配置日志
def configure_logging():
    # 创建日志目录
    log_dir = os.environ.get('ACLI_LOG_DIR', 'logs')  # 回归测试可指向隔离目录
    os.makedirs(log_dir, exist_ok=True)

    # 设置日志格式
    class TraceFilter(logging.Filter):
        """给每条日志注入本次请求的唯一调用链标识。

        非请求期（启动、定时任务）没有应用上下文，退回 '-'；可观测性组件
        自身异常绝不能拖垮日志主链路。
        """
        def filter(self, record):
            fn = globals().get('_trace_id')
            try:
                record.trace_id = fn() if fn else '-'
            except Exception:
                record.trace_id = '-'
            return True

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - trace=%(trace_id)s - %(message)s '
        '[in %(pathname)s:%(lineno)d]'
    )

    # 文件日志处理器 (自动轮转，每个文件10MB，保留5个备份)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, 'upload_server.log'),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    file_handler.addFilter(TraceFilter())

    # 控制台日志处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    console_handler.addFilter(TraceFilter())

    # 配置应用日志
    app.logger.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)

    # 禁用werkzeug默认日志
    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.setLevel(logging.WARNING)
    werkzeug_logger.addHandler(file_handler)

# 调用日志配置
configure_logging()

# 应用配置
# 路径支持环境变量覆盖，便于测试隔离与多实例部署（默认值保持生产行为）
UPLOAD_FOLDER = os.environ.get('ACLI_UPLOAD_FOLDER', '/sf/log')
ALLOWED_EXTENSIONS = {'zip'}
MAX_FILE_SIZE = 100 * 1024 * 1024 * 1024  # 100GB：单个上传文件的总大小上限
MAX_REQUEST_SIZE = 50 * 1024 * 1024  # 单请求上限（分片 10MB + 表单开销，防止超大单请求）
FILENAME_PATTERN = r'^Q\d{13}\.zip$'  # 旧常量保留，仅用于兼容性断言
FILENAME_STRICT_PATTERN = r'Q[0-9]{13}\.zip'  # 严格 ASCII 数字 + 整体完全匹配
CASE_ID_PATTERN = r'Q([0-9]{8})[0-9]{5}'  # Q + 8 位日期 + 5 位序号
MIN_CASE_YEAR = 2000  # 发号规则年份下界：挡掉 1611/1794/1869 这类随机数字
MAX_CASE_YEAR_AHEAD = 3  # 上界留 3 年，避免误伤未来日期的历史工单
# 5888 是独立落盘服务，离线且无工单主数据源，无法自证某工单"真实存在"。
# 能自证的只有三层：① 发号规则；② 平台签发的 P1 票据 cid 与产物名一致；
# ③ 运维同步的白名单文件显式登记。白名单默认不启用：新工单刚创建时
# 5888 尚无归档，默认启用白名单会把全部新工单挡在门外，属不可用设计。
TICKET_WHITELIST_FILE = os.environ.get('ACLI_TICKET_WHITELIST_FILE', '').strip()
LEGACY_CASE_DIR_EXEMPT = os.environ.get('ACLI_LEGACY_CASE_DIR_EXEMPT', '1').strip() == '1'
LEGACY_MIN_BYTES = 1024 * 1024  # 历史归档豁免门槛：目录内至少 1MiB 真实 zip
# 限流只约束"造会话/造目录"的两个入口，不限 upload_chunk：2.8GB 包有
# 271 个分片，限分片等于误伤正常上传。跨 4 个 worker 用文件锁共享计数。
RATE_WINDOW_SEC = int(os.environ.get('ACLI_RATE_WINDOW_SEC', '60'))
RATE_LIMITS = {
    '/init_upload': int(os.environ.get('ACLI_RATE_INIT_MAX', '20')),
    '/precheck': int(os.environ.get('ACLI_RATE_PRECHECK_MAX', '120')),
}
RATE_STATE_FILE = os.environ.get('ACLI_RATE_STATE_FILE', '/tmp/acli_upload_rate_state.json')
# 单工单目录累计体积配额：把"单文件 100GB"的灌盘风险压到目录级。
MAX_CASE_DIR_BYTES = int(os.environ.get('ACLI_MAX_CASE_DIR_BYTES', str(30 * 1024 ** 3)))
CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks
TEMP_FOLDER = os.environ.get('ACLI_TEMP_FOLDER', '/tmp/acli_upload_chunks')
SESSION_STORE = os.environ.get('ACLI_SESSION_STORE', '/tmp/acli_upload_sessions')  # 上传会话存储目录
SESSION_EXPIRE_HOURS = 24  # 会话过期时间(小时)
PROCESS_SCRIPT = os.environ.get(
    'ACLI_PROCESS_SCRIPT',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'process_upload.py'),
)  # 独立处理脚本：合片 / MD5 / 移动归档
# 完整工单状态查询与停滞判定阈值（支撑 P1 静默丢包检测与平台批量对账）
STALL_THRESHOLD_SECONDS = int(os.environ.get('ACLI_UPLOAD_STALL_SECONDS', '300'))
PROCESSING_STALL_THRESHOLD_SECONDS = int(os.environ.get('ACLI_PROCESSING_STALL_SECONDS', '1800'))
BATCH_STATUS_MAX_CASES = int(os.environ.get('ACLI_BATCH_STATUS_MAX_CASES', '50'))
ALLOWED_UPLOAD_SOURCES = frozenset({'p1_ticket', 'acli_direct', 'p2_relay', 'web_manual', 'unknown'})
TASK_ID_PATTERN = r'[A-Za-z0-9._-]{1,64}'

# 上传鉴权 Token（P3 安全项）。
# 仅当显式设置 ACLI_UPLOAD_TOKEN 时启用鉴权；未设置则保持向后兼容（无鉴权）。
# 启用后前端页面会自动注入该 token，所有上传/状态接口需携带
# Authorization: Bearer <token> 或 ?token=<token>。
AUTH_TOKEN = os.environ.get('ACLI_UPLOAD_TOKEN', '')

# ── P1 直传票据（2026-11-03）───────────────────────────────────────────────
# 为什么需要它：节点 acli 直传时不会带任何认证头，只认 `-u` 给的地址；
# 5888 启用 ACLI_UPLOAD_TOKEN 后 P1 全量 401（现场 Q2026110300278）。
# acli 1.0.0 起支持 `-L|--url-header`，可由 bridge 注入请求头，因此 P1 只需
# 一个"平台签发、本服务验签"的短期凭据即可恢复，不必改 acli。
#
# 设计边界：
# - 只验签、不回查平台：回查会把"5888 能否访问平台"变成新的单点故障；
# - 票据绑定产物名：init_upload 阶段即校验，防止拿 A 工单的票据传 B 的包；
# - 与全局 ACLI_UPLOAD_TOKEN 并存：P2 代传与手工上传页继续用旧通道。
# 与平台侧 backend/case-service/app/services/p1_ticket.py 必须逐字节一致。
P1_TICKET_SECRET = os.environ.get('ACLI_P1_TICKET_SECRET', '').strip()
P1_TICKET_PREFIX = 'p1.v1.'
P1_TICKET_CLOCK_SKEW = 300  # 平台与本机时钟未强制对齐，留 5 分钟宽限

# ── P1 直传鉴权开关（两阶段，2026-11-06）──────────────────────────────────
#
# 背景（源码审查 + 黑盒实测相互印证）：
#   acli 的 remote_transfer.py 中 http_transfer / TransferToSangfor.upload_init /
#   chunk_upload 把 headers={} 硬编码、未接 auth=；-u 只认 3 个 sangfor 域名并
#   硬编码 /init_upload。实测 -H / -L / -U / -P / 自定义 path 全部无效：create
#   会解析这些参数，但真正发请求的传输层不带任何认证信息。因此只要 5888 要求
#   凭据，P1 直传就 100% 在 /init_upload 被 401 打回并降级 P2。
#
# 阶段一（当前，ACLI_P1_REQUIRE_AUTH=0）：
#   允许"完全无认证头"的直传请求命中上传链路白名单，保证 P1 可用。
#   票据验签、全局 Bearer 校验的代码全部保留且继续生效：P2 代传与页面人工
#   上传仍走 Bearer；若哪个 acli 版本能带出票据，会优先命中票据通道。
#
# 阶段二（acli 修好 headers/auth 并分发后，ACLI_P1_REQUIRE_AUTH=1）：
#   恢复强制鉴权，匿名直传一律 401。无需改代码，一条命令切换：
#     sudo /acli/switch_p1_auth.sh on    # 写 drop-in + 重启
#     sudo /acli/switch_p1_auth.sh off   # 回退
#
# 放宽期间的兜底：产物名格式/大小/分片校验仍由各上传端点执行；每次匿名放行
# 都记审计日志（工单号 + 来源 IP）；会话清理不变。另注意首页 HTML 本来就内联
# 全局 token，这层鉴权本质只挡"不读页面的扫描脚本"，放宽的安全损失有限。
P1_REQUIRE_AUTH = os.environ.get('ACLI_P1_REQUIRE_AUTH', '0').strip() == '1'
P1_ANONYMOUS_EXACT = frozenset({'/precheck', '/init_upload', '/upload_chunk'})
P1_ANONYMOUS_PREFIX = ('/upload_status/',)


def _p1_anonymous_allowed(path):
    """仅上传链路可匿名；/archive_status 等核验/管理接口保持鉴权。"""
    return path in P1_ANONYMOUS_EXACT or path.startswith(P1_ANONYMOUS_PREFIX)


app.logger.info(
    f"[P1] 鉴权模式: {'strict(强制凭据)' if P1_REQUIRE_AUTH else 'relaxed(临时匿名直传放行)'}"
)


def _p1_b64url_decode(text):
    padding = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def verify_p1_ticket(token, filename=None, now=None):
    """校验 P1 票据，返回 (claims, error_code)。

    error_code 与平台侧 P1TicketError.code 同名，便于两侧排障时对同一串。
    """
    if not P1_TICKET_SECRET:
        return None, 'disabled'
    raw = (token or '').strip()
    if not raw:
        return None, 'empty'
    if not raw.startswith(P1_TICKET_PREFIX):
        return None, 'not_ticket'
    body = raw[len(P1_TICKET_PREFIX):]
    encoded, _, signature = body.rpartition('.')
    if not encoded or not signature:
        return None, 'malformed'
    signing_input = P1_TICKET_PREFIX + encoded
    expected = hmac.new(
        P1_TICKET_SECRET.encode('utf-8'),
        signing_input.encode('ascii'),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None, 'bad_signature'
    try:
        payload = json.loads(_p1_b64url_decode(encoded))
    except Exception:
        return None, 'malformed'
    if not isinstance(payload, dict):
        return None, 'malformed'
    if int(payload.get('v') or 0) != 1:
        return None, 'unsupported_version'
    expires_at = int(payload.get('exp') or 0)
    current = int(now if now is not None else time.time())
    if current > expires_at + P1_TICKET_CLOCK_SKEW:
        return None, 'expired'
    case_id = str(payload.get('cid') or '')
    ticket_filename = str(payload.get('fn') or '')
    if not case_id:
        return None, 'malformed'
    if filename is not None and ticket_filename != filename:
        return None, 'filename_mismatch'
    return {
        'case_id': case_id,
        'filename': ticket_filename,
        'exp': expires_at,
        'jti': str(payload.get('jti') or ''),
    }, ''


def _p1_request_filename():
    """从请求里取"这次上传的是哪个产物"，取不到返回 None（只验签不绑名）。

    init_upload / upload_chunk 从表单取；archive_status / upload_progress 从查询串取；
    case_status 从查询串 case_id 推导为 {case_id}.zip，确保 P1 票据不能跨工单查询。
    """
    filename = request.form.get('filename')
    if not filename:
        filename = request.args.get('filename')
    filename = (filename or '').strip()
    if filename:
        return filename
    case_id = (request.args.get('case_id') or '').strip()
    if case_id:
        return f'{case_id}.zip'
    return None

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_REQUEST_SIZE
app.config['TEMP_FOLDER'] = TEMP_FOLDER
app.config['SESSION_STORE'] = SESSION_STORE

# 注意：合片、MD5 计算、移动归档均已移交独立进程 PROCESS_SCRIPT。
# 这些操作耗时长且阻塞（大文件可达数十分钟），若放在 gevent worker 内执行
# 会阻塞事件循环导致心跳超时，master 会杀掉 worker（历史上 80GB 上传即因此失败）。

# 确保所有目录存在
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(SESSION_STORE, exist_ok=True)

def log_operation(action, details):
    """记录关键操作日志"""
    client_ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', 'unknown')

    log_message = f"[OPERATION] {action} - Client: {client_ip} - UA: {user_agent} - Details: {details}"
    app.logger.info(log_message)

def allowed_file(filename):
    """检查文件扩展名是否合法"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _trace_id():
    """本次请求的唯一调用链标识；非请求期返回 '-'。"""
    try:
        return getattr(g, 'trace_id', '') or '-'
    except RuntimeError:
        return '-'


def validate_filename(filename):
    r"""严格校验产物名：只允许 ASCII 数字 + 13 位 + 整体完全匹配。

    旧实现 re.match(r'^Q\d{13}\.zip$') 有三处漏洞：
      1) \d 默认匹配 Unicode 数字，阿拉伯数字串可绕过位数校验；
      2) $ 允许结尾有一个换行符，配合 init_upload 未 strip 会落成畸形目录；
      3) re.match 只从头匹配，不要求整体匹配。
    """
    return (isinstance(filename, str)
            and re.fullmatch(FILENAME_STRICT_PATTERN, filename) is not None)


def case_id_of(filename):
    """从已校验的产物名取工单号。"""
    return filename[: -len('.zip')]


def _log_guard(event, **fields):
    """安全判定统一结构化日志：链路 + 指标 + 判定依据。"""
    parts = ' '.join(f'{k}={v}' for k, v in fields.items())
    app.logger.warning(f'[GUARD] {event} ip={request.remote_addr} {parts}')


_WHITELIST_CACHE = {'path': None, 'mtime': None, 'ids': None}


def _load_ticket_whitelist():
    """加载运维同步的工单白名单；文件不存在返回 None（退化为规则模式）。"""
    if not TICKET_WHITELIST_FILE:
        return None
    try:
        st = os.stat(TICKET_WHITELIST_FILE)
    except OSError:
        return None
    cache = _WHITELIST_CACHE
    if cache['path'] == TICKET_WHITELIST_FILE and cache['mtime'] == st.st_mtime_ns:
        return cache['ids']
    ids = set()
    with open(TICKET_WHITELIST_FILE, encoding='utf-8') as f:
        for line in f:
            value = line.strip()
            if value and not value.startswith('#'):
                ids.add(value)
    cache.update(path=TICKET_WHITELIST_FILE, mtime=st.st_mtime_ns,
                 ids=frozenset(ids))
    return cache['ids']


def _case_id_date_valid(case_id):
    """发号规则：日期段必须是真实日历日期，且年份落在合理区间。

    只判"合法日历日期"不够：随机 13 位数字的前 8 位有相当概率凑成
    16110722、18691120 这类合法但绝无可能是工单的日期，必须加年份过滤。
    上界用"今年 +3 年"而非"不晚于今天"，否则现场真实归档
    Q2027102700999（635MB）会被误挡，客户无法重传。
    """
    m = re.fullmatch(CASE_ID_PATTERN, case_id)
    if m is None:
        return False
    try:
        day = datetime.strptime(m.group(1), '%Y%m%d').date()
    except ValueError:
        return False
    return MIN_CASE_YEAR <= day.year <= datetime.now().year + MAX_CASE_YEAR_AHEAD


def _legacy_archived_case(case_id):
    """历史归档豁免：目录已存在且含 >=1MiB 真实 zip 才放行。

    现场存在格式不合规但确为真实产物的历史目录（Q2026199991160 290MB、
    Q2027102700999 635MB），一刀切会挡掉这些工单的重传与核验。
    """
    if not LEGACY_CASE_DIR_EXEMPT:
        return False
    case_dir = os.path.join(app.config['UPLOAD_FOLDER'], case_id)
    if not os.path.isdir(case_dir):
        return False
    total = 0
    for entry in os.scandir(case_dir):
        try:
            if entry.is_file() and entry.name.endswith('.zip'):
                total += entry.stat().st_size
        except OSError:
            continue
        if total >= LEGACY_MIN_BYTES:
            return True
    return False


def validate_case_id(filename):
    """返回 (是否放行, 判定模式, 原因)。三层证据按可用性优先叠加。"""
    case_id = case_id_of(filename)
    claims = getattr(g, 'p1_claims', None)
    if claims:
        if claims.get('case_id') != case_id:
            return False, 'p1_ticket', '票据工单号与产物名不一致'
        return True, 'p1_ticket', '平台票据确认'
    allowed = _load_ticket_whitelist()
    if allowed is not None:
        hit = case_id in allowed
        return hit, 'whitelist', ('名单命中' if hit else '名单未登记')
    if _case_id_date_valid(case_id):
        return True, 'issue_rule', '日期段合法'
    if _legacy_archived_case(case_id):
        return True, 'legacy_archive', '已有真实归档豁免'
    return False, 'issue_rule', '工单号不符合平台发号规则'


@contextmanager
def _rate_state_lock():
    """跨 gunicorn worker 的文件锁：进程内计数会被 4 个 worker 各算一份。"""
    parent = os.path.dirname(RATE_STATE_FILE) or '.'
    os.makedirs(parent, exist_ok=True)
    with open(RATE_STATE_FILE + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield


def check_rate_limit(path):
    """固定窗口计数，返回 (是否放行, 当前计数, 上限)。"""
    limit = RATE_LIMITS.get(path)
    if not limit or limit <= 0:
        return True, 0, 0
    key = '{}|{}'.format(path, request.remote_addr or '-')
    window = int(time.time()) // RATE_WINDOW_SEC
    try:
        with _rate_state_lock():
            try:
                with open(RATE_STATE_FILE, encoding='utf-8') as f:
                    state = json.load(f)
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            # 只保留当前窗口与上一窗口，避免状态文件无限增长
            state = {k: v for k, v in state.items()
                     if isinstance(v, list) and len(v) == 2 and v[0] >= window - 1}
            cur_window, count = state.get(key, [window, 0])
            if cur_window != window:
                cur_window, count = window, 0
            count += 1
            state[key] = [cur_window, count]
            tmp = RATE_STATE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f)
            os.replace(tmp, RATE_STATE_FILE)
        return count <= limit, count, limit
    except Exception as e:
        # 限流组件故障：可用性优先放行，但留降级告警，绝不静默失效
        app.logger.error(f'[GUARD] rate_limit_degraded err={e}')
        return True, -1, limit


def _case_dir_bytes(case_dir):
    """统计单个工单目录内所有普通文件的累计体积。"""
    if not os.path.isdir(case_dir):
        return 0
    total = 0
    for entry in os.scandir(case_dir):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def check_case_dir_quota(filename, file_size):
    """返回 (已用字节, 加本次声明后是否仍在配额内)。"""
    case_dir = os.path.join(app.config['UPLOAD_FOLDER'], case_id_of(filename))
    used = _case_dir_bytes(case_dir)
    return used, used + int(file_size) <= MAX_CASE_DIR_BYTES


@app.before_request
def _assign_trace_id():
    """请求入口：优先复用上游 X-Request-ID，否则自签，贯穿日志与响应头。"""
    incoming = (request.headers.get('X-Request-ID') or '').strip()
    g.trace_id = re.sub(r'[^A-Za-z0-9._-]', '', incoming)[:64] or uuid.uuid4().hex
    g.request_start = time.time()


@app.before_request
def _enforce_upload_guards():
    """限流先于鉴权：扫描器高频打点时不必再走一遍凭据判定。"""
    if request.path not in RATE_LIMITS:
        return
    allowed, count, limit = check_rate_limit(request.path)
    if not allowed:
        _log_guard('rate_limited', path=request.path, count=count, limit=limit,
                   window=RATE_WINDOW_SEC, metric='rate_reject_total')
        return jsonify({'error': 'Too many requests',
                        'retry_after': RATE_WINDOW_SEC}), 429


@app.after_request
def _finalize_request_trace(response):
    """请求出口：回写调用链 + 结构化指标日志。"""
    tid = _trace_id()
    response.headers['X-Request-ID'] = tid
    response.headers['X-Trace-ID'] = tid
    cost = (time.time() - getattr(g, 'request_start', time.time())) * 1000
    app.logger.info(
        f'[REQ] ip={request.remote_addr} method={request.method} path={request.path} '
        f'status={response.status_code} cost_ms={cost:.1f} metric=request_total'
    )
    return response



def _session_lock_path(upload_id):
    """会话文件锁路径（与 JSON 同目录，按 upload_id 区分）"""
    return os.path.join(app.config['SESSION_STORE'], f"{upload_id}.lock")


# ── 断点续传（2026-11-09）───────────────────────────────────────────────
# 背景：init_upload 原本每次生成新 upload_id，前端也从第 0 片重新排队。
# 实测客户传到 2.18GB 后链路中断，刷新页面即 2.18GB 白传——"支持断点续传"
# 只对"暂停/继续"这一种情况成立。
# 修法：以 (filename, file_size, client_mtime) 定位同一文件的未完成会话，
# 复用 upload_id 并回传已收分片，前端只补缺失片。
# 为什么三个键都要：
#   filename     —— 区分工单产物；
#   file_size    —— 区分同一工单的不同收集批次；
#   client_mtime —— 区分"同名同大小的重新收集"（文件没变则 mtime 不变）。
# mtime 缺失时退回 filename+file_size（兼容旧会话与拿不到 mtime 的浏览器），
# 这条回退只接受"已有分片且未过期"的会话：宁可多传一片，也不接错一个包。

RESUME_INDEX_LOCK = 'resume_index.lock'


def _resume_index_lock_path():
    return os.path.join(app.config['SESSION_STORE'], RESUME_INDEX_LOCK)


@contextmanager
def _session_index_lock():
    """把「扫描已有会话 + 决定复用/新建」串行化。

    不加锁时两个并发的 init 都可能判定"没有可续传会话"，各自建一个
    upload_id，客户侧就等于什么都没续上。
    """
    with open(_resume_index_lock_path(), 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield


def _chunk_total_for(file_size):
    """按 CHUNK_SIZE 推算总分片数（与前端 Math.ceil 口径一致）"""
    size = int(file_size or 0)
    return max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)


def _receivable_chunks(session_data):
    """滤掉"会话里有记录但文件已不在"的分片，并重算 uploaded_size。

    分片可能被 lifecycle_clean 或人工清理删掉；若只信会话 JSON 里的计数，
    续传会漏传这些片，一直到合片阶段才报"大小不符"，白等一轮。
    """
    chunks = session_data.get('chunks') or {}
    alive = {}
    total = 0
    for key, path in chunks.items():
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        alive[str(key)] = path
        total += size

    if len(alive) != len(chunks):
        app.logger.warning(
            f"续传会话 {session_data.get('upload_id')} 有 "
            f"{len(chunks) - len(alive)} 个分片文件已丢失，按剩余 {len(alive)} 片计算")
        session_data['chunks'] = alive
    session_data['uploaded_size'] = total
    return session_data


def find_resumable_session(filename, file_size, client_mtime=None):
    """找一个能接着传的未完成会话，没有则返回 None。

    只认非终态（done/failed/pending 之外）、未过期、且至少已收一片的会话。
    同一工单可能有多条历史会话，取 last_updated 最新的一条；
    带 mtime 精确命中的优先于仅按文件名+体积命中的。
    """
    session_dir = app.config['SESSION_STORE']
    now = datetime.now()
    best = None
    best_last = None
    best_exact = False

    try:
        names = os.listdir(session_dir)
    except OSError as e:
        app.logger.error(f"扫描会话目录失败: {str(e)}")
        return None

    for name in names:
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(session_dir, name)) as f:
                data = json.load(f)
        except Exception:
            continue

        if data.get('filename') != filename:
            continue
        if int(data.get('file_size') or -1) != int(file_size):
            continue
        if data.get('status') in ('done', 'failed', 'pending'):
            continue
        if not data.get('chunks'):
            continue

        try:
            last = datetime.fromisoformat(data['last_updated'])
        except Exception:
            last = now
        if (now - last) > timedelta(hours=SESSION_EXPIRE_HOURS):
            continue

        data = _receivable_chunks(data)
        if not data['chunks']:
            continue

        stored_mtime = str(data.get('client_mtime') or '').strip()
        if client_mtime is not None and stored_mtime:
            # 双方都有 mtime：必须完全相等，否则是"同名同大小但重新收集"的另一个包，
            # 接上去就会产出内容错乱的归档。
            if stored_mtime != str(client_mtime):
                continue
            exact = True
        else:
            # 旧版本会话没有 mtime 字段，或浏览器拿不到 lastModified：
            # 只能按文件名+体积回退匹配，并把它排在精确命中之后。
            exact = False

        if best is None or exact or (not best_exact and last > best_last):
            best, best_last, best_exact = data, last, exact

    if best is not None:
        app.logger.info(
            f"命中可续传会话: id={best.get('upload_id')} file={filename} "
            f"size={file_size} 已收={len(best['chunks'])} "
            f"匹配={'exact' if best_exact else 'filename+size'}")
    return best


def find_completed_session(filename, file_size):
    """同一文件是否已经有"合片成功"的会话。

    用途：断线重连时前端会重新 init，如果后台其实已经合片完成、只是
    complete 响应没回到浏览器，这里直接告诉它"已经好了"，避免再传一遍 2.6GB。
    """
    session_dir = app.config['SESSION_STORE']
    best = None
    best_last = None
    now = datetime.now()

    try:
        names = os.listdir(session_dir)
    except OSError:
        return None

    for name in names:
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(session_dir, name)) as f:
                data = json.load(f)
        except Exception:
            continue

        if data.get('filename') != filename:
            continue
        if int(data.get('file_size') or -1) != int(file_size):
            continue
        if data.get('status') != 'done':
            continue
        target = data.get('target_path')
        if not target or not os.path.exists(target):
            continue
        try:
            last = datetime.fromisoformat(data['last_updated'])
        except Exception:
            last = now
        if (now - last) > timedelta(hours=SESSION_EXPIRE_HOURS):
            continue
        if best is None or last > best_last:
            best, best_last = data, last

    return best


def resume_payload(session_data):
    """整理成前端续传需要的数据形状（分片索引升序）"""
    indexes = sorted(int(k) for k in (session_data.get('chunks') or {}))
    return {
        'upload_id': session_data.get('upload_id'),
        'file_size': session_data.get('file_size'),
        'chunk_size': CHUNK_SIZE,
        'chunk_total': _chunk_total_for(session_data.get('file_size')),
        'received_chunks': indexes,
        'received_bytes': session_data.get('uploaded_size') or 0,
        'last_updated': session_data.get('last_updated'),
    }


def _atomic_write_session(session_path, session_data):
    """先写临时文件再 rename 原子替换，避免读到半截 JSON（P2 并发竞态）"""
    tmp_path = session_path + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(session_data, f)
    os.replace(tmp_path, session_path)


def save_upload_session(upload_id, session_data):
    """保存上传会话到文件（原子写）"""
    try:
        session_path = os.path.join(app.config['SESSION_STORE'], f"{upload_id}.json")
        session_data['last_updated'] = datetime.now().isoformat()
        _atomic_write_session(session_path, session_data)
        return True
    except Exception as e:
        app.logger.error(f"保存会话失败: {str(e)}")
        return False


def update_session_chunk(upload_id, chunk_index, chunk_path, chunk_total):
    """在文件锁内完成 读取->写入分片->累加->原子写回（P2 并发竞态根治）。

    关键点：
    - fcntl.LOCK_EX 把「读-改-写」整个临界区串行化。多 worker 并发上传同一
      上传任务的不同分片时，不会因后写覆盖先写而丢失 chunks 计数，
      也就不会「永远不触发合片、前端一直转圈」。
    - 只有使分片计数达到 chunk_total 的那一次更新返回 all_received=True，
      天然去重，避免重复派发后台进程做合片。
    - 重复分片（失败重试）不再重复累加 uploaded_size。
    返回 (session_data, all_received)；会话不存在/异常返回 (None, False)。
    """
    session_path = os.path.join(app.config['SESSION_STORE'], f"{upload_id}.json")
    lock_path = _session_lock_path(upload_id)
    try:
        with open(lock_path, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            if not os.path.exists(session_path):
                return None, False
            with open(session_path) as f:
                session_data = json.load(f)
            key = str(chunk_index)
            # 仅在首次记录该分片时累加大小（重试不重复计）
            if key not in session_data['chunks']:
                with suppress(OSError):
                    session_data['uploaded_size'] += os.path.getsize(chunk_path)
            session_data['chunks'][key] = chunk_path
            # 老会话没有 chunk_total，第一次收片时补上，后续一致性校验才有基准
            session_data['chunk_total'] = chunk_total
            session_data['last_updated'] = datetime.now().isoformat()
            _atomic_write_session(session_path, session_data)
    except Exception as e:
        app.logger.error(f"更新会话分片失败 {upload_id}: {str(e)}")
        return None, False
    all_received = len(session_data['chunks']) == chunk_total
    return session_data, all_received

def load_upload_session(upload_id):
    """从文件加载上传会话"""
    try:
        session_path = os.path.join(app.config['SESSION_STORE'], f"{upload_id}.json")
        if not os.path.exists(session_path):
            return None

        with open(session_path) as f:
            session_data = json.load(f)

        # 检查会话是否过期
        last_updated = datetime.fromisoformat(session_data['last_updated'])
        if (datetime.now() - last_updated) > timedelta(hours=SESSION_EXPIRE_HOURS):
            app.logger.info(f"会话已过期: {upload_id}")
            delete_upload_session(upload_id)
            delete_upload_chunks(upload_id)
            return None

        return session_data
    except Exception as e:
        app.logger.error(f"加载会话失败: {str(e)}")
        return None

def delete_upload_session(upload_id):
    """删除上传会话"""
    try:
        session_path = os.path.join(app.config['SESSION_STORE'], f"{upload_id}.json")
        if os.path.exists(session_path):
            os.remove(session_path)
        return True
    except Exception as e:
        app.logger.error(f"删除会话失败: {str(e)}")
        return False

def delete_upload_chunks(upload_id):
    """删除某次上传产生的所有临时文件（分片与合并文件）

    分片命名为 {upload_id}.part 与 {upload_id}_chunk_{index}.part，
    以 upload_id 为前缀匹配可覆盖该次上传的全部临时文件。
    """
    removed = 0
    freed = 0
    try:
        temp_dir = app.config['TEMP_FOLDER']
        for name in os.listdir(temp_dir):
            if name.startswith(upload_id) and name.endswith('.part'):
                path = os.path.join(temp_dir, name)
                try:
                    freed += os.path.getsize(path)
                    os.remove(path)
                    removed += 1
                except OSError as e:
                    app.logger.error(f"清理临时文件失败 {name}: {str(e)}")
        if removed:
            app.logger.info(f"已清理上传临时文件 {removed} 个，"
                            f"释放 {freed / (1024 * 1024):.2f}MB，upload_id: {upload_id}")
        return removed
    except Exception as e:
        app.logger.error(f"清理上传临时文件时出错: {str(e)}")
        return removed

# 注意：过期会话与孤儿分片的兜底清理已移交 /acli/lifecycle_clean.py
# （由 cron 每小时调度 chunks 子命令），不再于此常驻线程——
# 原实现会在每个 gunicorn worker 内各起一个线程重复扫描同一目录。
# 请求路径内的即时清理由 delete_upload_chunks() 负责。

@app.before_request
def _require_auth():
    """上传接口鉴权：P1 票据优先，全局 token 兼容。

    两条通道并存是刻意的：
    - P1（节点 acli 直传）无法携带全局 token，只能由平台签发 per-task 票据；
    - P2（平台代传）与手工上传页继续用 ACLI_UPLOAD_TOKEN，不改既有行为。

    判定顺序按"哪个更具体"来：有 p1.v1. 前缀的必须是合法票据，
    绝不能因为"格式不像"就退化到全局 token 比较后放行。
    """
    if not AUTH_TOKEN and not P1_TICKET_SECRET:
        g.auth_source = 'open'
        return
    if request.path == '/' or request.path.startswith('/static/') or request.path == '/healthz':
        g.auth_source = 'public'
        return
    auth = request.headers.get('Authorization', '')
    # 认证方式必须可观测：acli 的 -H / -L 已被证明不会出现在上传请求上，
    # 只有把 scheme 与"头是否存在"记进日志，才能判断 -U / -P 是否真的生效。
    scheme = 'none'
    basic_user = ''
    token = ''
    if auth.startswith('Bearer '):
        scheme = 'bearer'
        token = auth[len('Bearer '):]
    elif auth.startswith('Basic '):
        scheme = 'basic'
        try:
            raw = base64.b64decode(auth[len('Basic '):].strip()).decode('utf-8', 'replace')
            basic_user, _, password = raw.partition(':')
            token = password
        except Exception:
            token = ''
    if not token:
        query_token = request.args.get('token')
        if query_token:
            scheme = 'query'
            token = query_token
    token = (token or '').strip()

    if token.startswith(P1_TICKET_PREFIX):
        # 批量查询接口面向平台后台对账，严禁单工单 P1 票据访问
        if request.path == '/cases/status/batch':
            app.logger.warning(
                f"[P1] 拒绝票据访问批量接口: path={request.path} from {request.remote_addr}"
            )
            return jsonify({'error': 'Forbidden', 'reason': 'batch_requires_global_token'}), 403
        filename = _p1_request_filename()
        claims, error_code = verify_p1_ticket(token, filename=filename)
        if claims is None:
            # 审计到工单粒度：P1 失败必须能回答"哪个工单、哪个产物、为什么"。
            app.logger.warning(
                f"[P1] 票据拒绝: reason={error_code} path={request.path} "
                f"filename={filename or '-'} from {request.remote_addr}")
            return jsonify({'error': 'Unauthorized', 'reason': error_code}), 401
        g.p1_claims = claims
        g.auth_source = 'p1_ticket'
        app.logger.info(
            f"[P1] 票据通过: case={claims['case_id']} jti={claims['jti']} "
            f"path={request.path} filename={filename or '-'} from {request.remote_addr}")
        return

    # 全局 token 通道（P2 平台代传 + 页面人工上传）
    if AUTH_TOKEN and hmac.compare_digest(token, AUTH_TOKEN):
        g.auth_source = 'global_bearer'
        return

    # ── 阶段一：acli 匿名直传临时放行（见文件头 P1_REQUIRE_AUTH 说明）──
    # 条件刻意收紧，避免"随便发个错头就绕过鉴权"：
    #  1) 开关关闭；2) 完全没有任何认证信息（带错头/错 query token 一律拒绝）；
    #  3) 仅上传链路白名单；4) 产物名/大小校验仍由各端点自己做。
    no_credentials = not auth and not request.args.get('token')
    if (not P1_REQUIRE_AUTH and no_credentials
            and _p1_anonymous_allowed(request.path)):
        g.auth_source = 'p1_anonymous'
        filename = _p1_request_filename() or '-'
        # init 是每次上传的关键事件，用 WARNING 便于在日志里快速定位；
        # 分片请求量大，降为 INFO，避免刷屏。
        log = app.logger.warning if request.path == '/init_upload' else app.logger.info
        log(f"[P1] 匿名直传放行(临时): path={request.path} filename={filename} "
            f"from {request.remote_addr} mode=relaxed")
        return

    # 全局 token 未启用时，非票据请求保持放行（历史行为）。
    if not AUTH_TOKEN:
        g.auth_source = 'open'
        return

    # scheme 与 header 是否存在是排障关键：只有它们能区分
    # "acli 根本没发头"与"发了但值不对"。
    app.logger.warning(
        f"鉴权失败: {request.path} from {request.remote_addr} "
        f"scheme={scheme} header={'yes' if auth else 'no'}"
        + (f" user={basic_user}" if basic_user else ""))
    return jsonify({'error': 'Unauthorized'}), 401


@app.route('/')
def index():
    """文件上传页面"""
    log_operation('PageAccess', 'Accessed upload page')
    return render_template('upload.html', upload_token=AUTH_TOKEN)

@app.route('/static/<path:path>')
def send_static(path):
    """提供静态文件"""
    app.logger.debug(f"Serving static file: {path}")
    return send_from_directory('static', path)

@app.route('/precheck', methods=['POST'])
def precheck_archive():
    """上传前判重：同一份包已在归档目录里时，不必再从客户侧传一遍。

    两级精度，刻意不混用：
    - 带 sha256：内容级精确判定，可以据此直接劝退；
    - 不带 sha256：只按体积给"疑似"提示（certain=false），绝不据此拒绝——
      体积相同不等于内容相同，误判的代价是客户以为日志已经传上来了。

    只读接口：不建会话、不写归档目录之外的东西；唯一副作用是补齐摘要缓存。
    """
    filename = (request.form.get('filename') or '').strip()
    size_text = (request.form.get('file_size') or '').strip()
    declared = (request.form.get('sha256') or '').strip().lower()
    mtime_text = (request.form.get('client_mtime') or '').strip()

    if not validate_filename(filename):
        return jsonify({'error': 'Invalid filename'}), 400
    case_ok, case_mode, case_reason = validate_case_id(filename)
    if not case_ok:
        _log_guard('case_id_rejected', file=filename, mode=case_mode,
                   reason=case_reason, metric='case_reject_total')
        return jsonify({'error': 'Invalid case id', 'reason': case_reason}), 400
    if declared and not re.fullmatch(r'[0-9a-f]{64}', declared):
        return jsonify({'error': 'Invalid sha256'}), 400
    try:
        file_size = int(size_text)
    except (TypeError, ValueError):
        file_size = 0

    client_mtime = None
    if mtime_text:
        try:
            # 前端传毫秒（Date.getTime()），统一转 int 比较
            client_mtime = int(float(mtime_text))
        except (TypeError, ValueError):
            client_mtime = None

    # 续传探测（只读）：已经传过一部分的同名同大小文件，直接把已收分片告诉前端，
    # 让它只补缺失片。这条信息与"归档是否已存在"是两个独立维度，都要返回。
    resume = None
    if file_size > 0:
        _pending = find_resumable_session(filename, file_size, client_mtime)
        if _pending:
            resume = resume_payload(_pending)

    case_dir = os.path.join(UPLOAD_FOLDER, filename[: -len('.zip')])
    if not os.path.isdir(case_dir):
        return jsonify({'found': False, 'match': None, 'certain': False, 'resume': resume})

    if declared:
        hit = archive_index.find_duplicate(case_dir, declared)
        if hit:
            app.logger.info(
                f"预检命中(sha256): file={filename} 已存在={hit['stored_name']} "
                f"from {request.remote_addr}"
            )
            return jsonify({
                'found': True,
                'match': 'sha256',
                'certain': True,
                'resume': resume,
                'stored_name': hit['stored_name'],
                'size': hit['size'],
                'mtime': hit['mtime'],
            })
        return jsonify({'found': False, 'match': None, 'certain': False, 'resume': resume})

    if file_size > 0:
        candidates = archive_index.find_by_size(case_dir, file_size)
        if candidates:
            latest = max(candidates, key=lambda item: item['mtime'])
            app.logger.info(
                f"预检疑似(按体积): file={filename} size={file_size} "
                f"候选={len(candidates)} from {request.remote_addr}"
            )
            return jsonify({
                'found': True,
                'match': 'size',
                'certain': False,
                'resume': resume,
                'stored_name': latest['stored_name'],
                'size': latest['size'],
                'mtime': latest['mtime'],
                'candidates': len(candidates),
            })
    return jsonify({'found': False, 'match': None, 'certain': False, 'resume': resume})


def _resolve_upload_source():
    """根据鉴权通道与表单白名单字段推导可信上传来源。"""
    auth_source = getattr(g, 'auth_source', '')
    if auth_source == 'p1_ticket':
        return 'p1_ticket'
    declared = (request.form.get('upload_source') or '').strip().lower()
    if declared in ALLOWED_UPLOAD_SOURCES and declared != 'unknown':
        return declared
    if auth_source == 'p1_anonymous':
        return 'acli_direct'
    if auth_source == 'global_bearer':
        return 'p2_relay'
    return 'unknown'


def _sanitize_task_id(raw_value):
    """按白名单清洗可选 task_id，防止日志/缓存注入异常字符。"""
    text = (raw_value or '').strip()
    if not text:
        return None
    if re.fullmatch(TASK_ID_PATTERN, text):
        return text
    return None


@app.route('/init_upload', methods=['POST'])
def init_upload():
    """初始化上传，生成上传ID"""
    try:
        # 记录请求开始
        start_time = time.time()
        app.logger.info(f"Init upload request received. Form data: {request.form}")

        # 检查请求数据
        if not request.form:
            app.logger.warning("No form data provided")
            return jsonify({'error': 'No form data provided'}), 400

        filename = (request.form.get('filename') or '').strip()
        file_size = (request.form.get('file_size') or '').strip()

        if not filename or not file_size:
            app.logger.warning(f"Missing parameters. Filename: {filename}, File size: {file_size}")
            return jsonify({'error': 'Missing filename or file_size'}), 400

        try:
            file_size = int(file_size)
        except (TypeError, ValueError) as e:
            app.logger.error(f"Invalid file_size format: {file_size}. Error: {str(e)}")
            return jsonify({'error': 'Invalid file_size format'}), 400

        # 校验总大小上限（分片上传会绕开单请求的 MAX_CONTENT_LENGTH 限制，必须在此把关）
        if file_size <= 0:
            app.logger.warning(f"Invalid file size: {file_size}")
            return jsonify({'error': 'Invalid file_size'}), 400
        if file_size > MAX_FILE_SIZE:
            app.logger.warning(f"File size {file_size} exceeds limit {MAX_FILE_SIZE}")
            return jsonify({'error': f'File size exceeds limit of {MAX_FILE_SIZE} bytes'}), 400

        if not validate_filename(filename):
            app.logger.warning(f"Invalid filename format: {filename}")
            return jsonify({'error': 'Invalid filename'}), 400

        case_ok, case_mode, case_reason = validate_case_id(filename)
        if not case_ok:
            _log_guard('case_id_rejected', file=filename, mode=case_mode,
                       reason=case_reason, metric='case_reject_total')
            return jsonify({'error': 'Invalid case id', 'reason': case_reason}), 400
        app.logger.info(
            f"[GUARD] case_id_accepted file={filename} mode={case_mode}")

        # 内容判重（2026-11-06）：客户端带 sha256 时先看目录里有没有同一份。
        # 现场到 5888 的上行实测只有 ~170KB/s，545MB 要传近一小时；而本地算一次
        # sha256 只要几十秒——差别不是"多传一次"，是四十分钟的带宽。
        # force=1 表示"我知道内容一样，仍要留一份新归档"（比如需要刷 mtime），
        # 走原有流程，不拦。
        declared_sha256 = (request.form.get('sha256') or '').strip().lower()
        force = (request.form.get('force') or '').strip().lower() in ('1', 'true', 'yes')
        if declared_sha256 and not force:
            if not re.fullmatch(r'[0-9a-f]{64}', declared_sha256):
                return jsonify({'error': 'Invalid sha256'}), 400
            case_dir = os.path.join(UPLOAD_FOLDER, filename[: -len('.zip')])
            hit = (
                archive_index.find_duplicate(case_dir, declared_sha256)
                if os.path.isdir(case_dir)
                else None
            )
            if hit:
                app.logger.warning(
                    f"拒绝重复归档: file={filename} sha256={declared_sha256[:12]}… "
                    f"已存在={hit['stored_name']} size={hit['size']} "
                    f"from {request.remote_addr}"
                )
                return jsonify({
                    'error': 'duplicate_archive',
                    'reason': 'sha256_matched',
                    'stored_name': hit['stored_name'],
                    'size': hit['size'],
                    'mtime': hit['mtime'],
                }), 409

        client_mtime = (request.form.get('client_mtime') or '').strip() or None
        if client_mtime is not None:
            try:
                client_mtime = str(int(float(client_mtime)))
            except (TypeError, ValueError):
                client_mtime = None

        # 断点续传（2026-11-09）：先看有没有同一文件的未完成会话。
        # 判定与新建必须串行，否则两个并发 init 会各建一个会话，等于没续上。
        with _session_index_lock():
            # 配额检查放在会话锁内：并发 init 同一工单号时必须串行判定，
            # 否则每个请求都看到"未超限"，合起来仍能把目录撑爆。
            used_bytes, quota_ok = check_case_dir_quota(filename, file_size)
            if not quota_ok:
                _log_guard('case_dir_quota_exceeded', file=filename,
                           used=used_bytes, incoming=file_size,
                           limit=MAX_CASE_DIR_BYTES, metric='quota_reject_total')
                return jsonify({'error': 'Case directory quota exceeded',
                                'limit': MAX_CASE_DIR_BYTES}), 413

            existing = None if force else find_resumable_session(
                filename, file_size, client_mtime)

            upload_source = _resolve_upload_source()
            task_id = _sanitize_task_id(request.form.get('task_id'))
            req_trace_id = _trace_id()

            if existing is not None:
                # 复用已有会话：补齐字段后原样返回，前端据此只补缺失分片
                existing['client_mtime'] = existing.get('client_mtime') or client_mtime
                existing['chunk_total'] = _chunk_total_for(file_size)
                existing['status'] = existing.get('status') or 'uploading'
                if not existing.get('upload_source') or existing.get('upload_source') == 'unknown':
                    existing['upload_source'] = upload_source
                if not existing.get('task_id') and task_id:
                    existing['task_id'] = task_id
                existing['trace_id'] = existing.get('trace_id') or (req_trace_id if req_trace_id != '-' else None)
                save_upload_session(existing['upload_id'], existing)
                payload = resume_payload(existing)
                log_operation(
                    'UploadResume',
                    f"ID: {existing['upload_id']}, File: {filename}, Source: {existing.get('upload_source')}, "
                    f"已收: {len(payload['received_chunks'])}/{payload['chunk_total']}")
                app.logger.info(
                    f"Upload resumed. ID: {existing['upload_id']}, File: {filename}, "
                    f"Source: {existing.get('upload_source')}, "
                    f"已收={len(payload['received_chunks'])}/{payload['chunk_total']}")
                return jsonify({
                    'upload_id': existing['upload_id'],
                    'chunk_size': CHUNK_SIZE,
                    'resumed': True,
                    'received_chunks': payload['received_chunks'],
                    'received_bytes': payload['received_bytes'],
                    'file_size': file_size,
                    'chunk_total': payload['chunk_total'],
                })

            # 已经归档成功、但 complete 响应没回到浏览器的场景：直接收尾，
            # 不让客户把 2.6GB 再传一遍。
            if not force:
                finished = find_completed_session(filename, file_size)
                if finished is not None:
                    stored = os.path.basename(finished.get('target_path') or '')
                    log_operation(
                        'UploadAlreadyDone',
                        f"ID: {finished['upload_id']}, File: {filename}, 归档={stored}")
                    app.logger.info(
                        f"Upload already done. ID: {finished['upload_id']}, "
                        f"File: {filename}, target={finished.get('target_path')}")
                    return jsonify({
                        'already_done': True,
                        'upload_id': finished['upload_id'],
                        'stored_name': stored,
                        'target_path': finished.get('target_path'),
                        'digest': finished.get('digest'),
                    })

            upload_id = str(uuid.uuid4())
            temp_filepath = os.path.join(app.config['TEMP_FOLDER'], f"{upload_id}.part")

            # 创建会话数据
            session_data = {
                'upload_id': upload_id,
                'filename': filename,
                'temp_filepath': temp_filepath,
                'file_size': file_size,
                'uploaded_size': 0,
                'chunks': {},
                'status': 'uploading',
                'upload_source': upload_source,
                'task_id': task_id,
                'trace_id': req_trace_id if req_trace_id != '-' else None,
                'client_ip': request.remote_addr,
                # 下面三个字段是续传的定位依据，缺一个就只能退回"按名+体积"猜
                'chunk_total': _chunk_total_for(file_size),
                'client_mtime': client_mtime,
                'start_time': start_time,
                'last_updated': datetime.now().isoformat()
            }

            # 保存会话
            if not save_upload_session(upload_id, session_data):
                return jsonify({'error': 'Failed to initialize upload session'}), 500

        # 记录操作
        log_operation('UploadInit',
                     f"ID: {upload_id}, File: {filename}, Size: {file_size} bytes")

        app.logger.info(f"Upload initialized. ID: {upload_id}, Temp path: {temp_filepath}")

        return jsonify({
            'upload_id': upload_id,
            'chunk_size': CHUNK_SIZE,
            'resumed': False,
            'received_chunks': [],
            'received_bytes': 0,
            'file_size': file_size,
            'chunk_total': _chunk_total_for(file_size),
        })

    except Exception as e:
        app.logger.error(f"Unexpected error in init_upload: {str(e)}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/upload_chunk', methods=['POST'])
def upload_chunk():
    """上传文件分块"""
    try:
        time.time()
        app.logger.debug(f"Chunk upload request received. Form keys: {request.form.keys()}")

        # 检查必要参数
        if not request.form:
            app.logger.warning("No form data in chunk upload")
            return jsonify({'error': 'No form data provided'}), 400

        upload_id = request.form.get('upload_id')
        chunk_index = request.form.get('chunk_index')
        chunk_total = request.form.get('chunk_total')
        app.logger.debug(f"Upload parameters - upload_id: {upload_id}, chunk_index: {chunk_index}, chunk_total: {chunk_total}")

        if not all([upload_id, chunk_index, chunk_total]):
            app.logger.warning(f"Missing parameters in chunk upload. Upload ID: {upload_id}, Chunk index: {chunk_index}, Chunk total: {chunk_total}")
            return jsonify({'error': 'Missing required parameters'}), 400

        try:
            chunk_index = int(chunk_index)
            chunk_total = int(chunk_total)
        except (TypeError, ValueError) as e:
            app.logger.error(f"Invalid chunk parameters. Index: {chunk_index}, Total: {chunk_total}. Error: {str(e)}")
            return jsonify({'error': 'Invalid chunk index or total format'}), 400

        # 分片边界校验（2026-10-31）：index 必须落在 [0, chunk_total) 内。
        # 不校验的直接后果：客户端把末片算错（源包在传输期间被重新生成）时，
        # 越界分片照样落盘并被计入 chunks 数量，len(chunks)==chunk_total 会提前成立，
        # 于是在缺片的情况下触发合片 —— 产出一个"看起来成功"的坏包。
        if chunk_total <= 0 or chunk_index < 0 or chunk_index >= chunk_total:
            app.logger.warning(
                f"Invalid chunk range. upload_id={upload_id}, "
                f"index={chunk_index}, total={chunk_total}")
            return jsonify({'error': 'Invalid chunk index or total'}), 400

        if 'chunk' not in request.files:
            app.logger.warning("No chunk file in request")
            return jsonify({'error': 'No chunk file provided'}), 400

        # 加载会话（存在性 / 过期校验）
        session_data = load_upload_session(upload_id)
        if not session_data:
            app.logger.warning(f"Invalid or expired upload ID: {upload_id}")
            return jsonify({'error': 'Invalid or expired upload ID'}), 400

        # 终态会话拒绝继续收分片（2026-10-31）：done 之后重复收片会再次派发合片、
        # 产出第二份归档；failed/pending 同理。这是"不删会话"带来的必要约束。
        if session_data.get('status') in ('done', 'failed', 'pending'):
            app.logger.warning(
                f"Session already in terminal state: {upload_id}, status={session_data.get('status')}")
            return jsonify({'error': f"Upload already {session_data.get('status')}",
                            'status': session_data.get('status')}), 409

        # 分片总数一致性（2026-11-09）：续传必须沿用同一套切片参数。
        # file_size 变了（重新收集导致包大小不同）时会话里的 chunk_total 对不上，
        # 继续收片只会在合片阶段报"大小不符"，不如在这里就让客户端重新 init。
        session_total = session_data.get('chunk_total') or _chunk_total_for(
            session_data.get('file_size'))
        if int(session_total) != int(chunk_total):
            app.logger.warning(
                f"Chunk total mismatch. upload_id={upload_id}, "
                f"request={chunk_total}, session={session_total}")
            return jsonify({
                'error': 'Chunk total mismatch, please re-init upload',
                'expected': int(session_total),
            }), 409

        chunk_data = request.files['chunk']

        # 保存分块到磁盘（先写临时名再 rename：同一分片被重试或多标签页并发写时，
        # 直接 save 到目标路径会留下半截文件，而会话计数已把它当成完成）
        chunk_path = os.path.join(app.config['TEMP_FOLDER'], f"{upload_id}_chunk_{chunk_index}.part")
        tmp_chunk_path = f"{chunk_path}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            chunk_data.save(tmp_chunk_path)
            os.replace(tmp_chunk_path, chunk_path)
            app.logger.debug(f"Chunk {chunk_index} saved to {chunk_path}")
        except Exception as e:
            with suppress(OSError):
                os.remove(tmp_chunk_path)
            app.logger.error(f"Error saving chunk {chunk_index}: {str(e)}")
            return jsonify({'error': 'Failed to save chunk'}), 500

        # 单片大小上限（2026-10-31）：init 返回的 chunk_size 是 10MB，
        # 超过它的分片一定是客户端切错了。此处直接拒绝，比"照收再在合片时
        # 才发现大小不符"更早暴露问题，也避免单个请求无限膨胀。
        try:
            saved_chunk_size = os.path.getsize(chunk_path)
        except OSError:
            saved_chunk_size = 0
        if saved_chunk_size > CHUNK_SIZE:
            with suppress(OSError):
                os.remove(chunk_path)
            app.logger.warning(
                f"Chunk too large. upload_id={upload_id}, "
                f"index={chunk_index}, size={saved_chunk_size}, limit={CHUNK_SIZE}")
            return jsonify({'error': 'Chunk exceeds chunk_size'}), 413

        # 在文件锁内原子更新会话分片计数（根治并发竞态，见 update_session_chunk 注释）
        session_data, all_received = update_session_chunk(
            upload_id, chunk_index, chunk_path, chunk_total)
        if session_data is None:
            # 会话在保存分片期间过期或被清理
            with suppress(OSError):
                os.remove(chunk_path)
            return jsonify({'error': 'Invalid or expired upload ID'}), 400
        app.logger.debug(
            f"Chunk {chunk_index} recorded. "
            f"received={len(session_data['chunks'])}/{chunk_total}")

        # 所有分块上传完成：交给独立进程处理（合片 / MD5 / 移动归档）
        # 此处不做任何重活，立即返回，避免阻塞 gevent 事件循环导致心跳超时
        if all_received:
            try:
                session_data['status'] = 'pending'
                session_data['last_updated'] = datetime.now().isoformat()
                save_upload_session(upload_id, session_data)

                app.logger.info(f"All chunks received. Dispatching to background worker: {upload_id}")

                # start_new_session=True 让子进程脱离当前进程组，
                # 避免 gunicorn worker 重启时被连带终止
                subprocess.Popen(
                    [sys.executable, PROCESS_SCRIPT, upload_id],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )

                total_time = time.time() - session_data['start_time']
                app.logger.info(
                    f"Upload completed. ID: {upload_id}, "
                    f"File: {session_data['filename']}, "
                    f"Size: {session_data['file_size']} bytes, "
                    f"Time: {total_time:.2f} seconds, "
                    f"Avg speed: {(session_data['file_size'] / total_time / 1024):.2f} KB/s"
                )

                log_operation('UploadComplete',
                            f"ID: {upload_id}, File: {session_data['filename']}, "
                            f"Size: {session_data['file_size']} bytes, Status: pending")

                # 注意：此处不删除会话与分片，独立进程处理完成后自行清理；
                # 若进程异常退出，残留文件由 lifecycle_clean.py chunks 兜底回收
                return jsonify({'status': 'complete'})
            except Exception as e:
                app.logger.error(f"Error dispatching background worker: {str(e)}", exc_info=True)
                delete_upload_chunks(upload_id)
                return jsonify({'error': 'Failed to start processing'}), 500

        return jsonify({'status': 'chunk_uploaded'})

    except Exception as e:
        app.logger.error(f"Unexpected error in upload_chunk: {str(e)}", exc_info=True)
        try:
            failed_id = request.form.get('upload_id') if request.form else None
            if failed_id:
                delete_upload_chunks(failed_id)
        except Exception as ce:
            app.logger.error(f"清理失败上传的分片时出错: {str(ce)}")
        return jsonify({'error': 'Internal server error'}), 500



@app.route('/upload_status/<upload_id>')
def get_upload_status(upload_id):
    """获取上传状态"""
    try:
        app.logger.debug(f"Status check for upload ID: {upload_id}")

        session_data = load_upload_session(upload_id)
        if not session_data:
            app.logger.warning(f"Invalid or expired upload ID for status check: {upload_id}")
            return jsonify({'error': 'Invalid or expired upload ID'}), 404

        progress = (session_data['uploaded_size'] / session_data['file_size']) * 100

        app.logger.debug(
            f"Upload status. ID: {upload_id}, "
            f"Progress: {progress:.1f}%, "
            f"Uploaded: {session_data['uploaded_size']} bytes, "
            f"Total: {session_data['file_size']} bytes"
        )

        return jsonify({
            'uploaded_size': session_data['uploaded_size'],
            'file_size': session_data['file_size'],
            'progress': progress,
            # status/error（2026-10-31）：让调用方能立刻区分
            # "在传 / 处理中 / 已失败"，而不是把所有失败都等成一次超时。
            # 失败态由 process_upload.py 写回会话，会话刻意保留。
            'status': session_data.get('status') or 'uploading',
            'error': session_data.get('error'),
            # 摘要与落地信息（2026-10-31）：调用方据此校验服务端落盘字节，
            # 这是链路上唯一能证明"归档件内容正确"的锚点。
            'digest': session_data.get('digest'),
            'target_path': session_data.get('target_path'),
            'finished_at': session_data.get('finished_at'),
            # 续传核对用：服务端实际已收的分片索引与总数
            'received_chunks': sorted(int(k) for k in (session_data.get('chunks') or {})),
            'chunk_total': session_data.get('chunk_total') or _chunk_total_for(
                session_data.get('file_size')),
        })
    except Exception as e:
        app.logger.error(f"Error in status check: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

def _safe_case_dir(case_id):
    """按路径边界安全拼接工单归档目录，防止任何目录穿越。"""
    upload_root = os.path.realpath(app.config['UPLOAD_FOLDER'])
    candidate = os.path.realpath(os.path.join(upload_root, case_id))
    if not candidate.startswith(upload_root + os.sep):
        return None
    return candidate


def _parse_utc_timestamp(value):
    """将 Unix 时间戳或 ISO 字符串统一解析为 UTC Unix 秒数。"""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return parsed.astimezone(UTC).timestamp()
        return float(value)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _list_case_archives(case_id):
    """只读枚举工单目录下全部归档文件并 O(1) 结合 .digests.json 返回元数据。

    绝不触发大文件现场哈希，绝不写磁盘；按 mtime 降序排列。
    """
    case_dir = _safe_case_dir(case_id)
    if not case_dir or not os.path.isdir(case_dir):
        return []
    canonical = f'{case_id}.zip'
    cache = archive_index.load_cache(case_dir)
    items = []
    try:
        names = os.listdir(case_dir)
    except OSError:
        return []
    for name in names:
        if not (name == canonical or (name.startswith(case_id + '-') and name.endswith('.zip'))):
            continue
        path = os.path.join(case_dir, name)
        try:
            if not os.path.isfile(path):
                continue
            stat = os.stat(path)
        except OSError:
            continue
        cached = archive_index.read_cached_entry(cache, name, stat) or {}
        sha256_hex = cached.get('sha256')
        items.append({
            'stored_name': name,
            'target_path': path,
            'size': stat.st_size,
            'mtime': int(stat.st_mtime),
            'mtime_ns': stat.st_mtime_ns,
            'mtime_iso': _unix_to_utc_iso(stat.st_mtime),
            'sha256': sha256_hex,
            'upload_source': cached.get('upload_source') or 'unknown',
            'upload_id': cached.get('upload_id'),
            'task_id': cached.get('task_id'),
            'trace_id': cached.get('trace_id'),
            'finished_at': cached.get('finished_at') or _unix_to_utc_iso(stat.st_mtime),
            'archive_bind_key': archive_index.archive_bind_key(stat, sha256_hex),
        })
    items.sort(key=lambda item: (item['mtime_ns'], item['stored_name']), reverse=True)
    return items


def _scan_active_sessions_by_filename(target_filenames=None):
    """严格只读扫描 SESSION_STORE，按 filename 分组返回未过期会话列表。

    不调用 load_upload_session（避免顺手删文件）；遇到写入中的半截 JSON 直接跳过。
    """
    session_store = app.config['SESSION_STORE']
    grouped = {}
    now_ts = time.time()
    expire_sec = SESSION_EXPIRE_HOURS * 3600
    try:
        names = os.listdir(session_store)
    except OSError as exc:
        app.logger.warning(
            f'[REQ] case_status session_store_unavailable err={exc} '
            f'trace={_trace_id()} metric=case_status_scan_degraded_total'
        )
        return grouped

    for name in names:
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(session_store, name)) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        fn = data.get('filename')
        if not fn:
            continue
        if target_filenames is not None and fn not in target_filenames:
            continue
        last_ts = _parse_utc_timestamp(data.get('last_updated'))
        if last_ts is not None and (now_ts - last_ts) > expire_sec:
            continue
        grouped.setdefault(fn, []).append(data)
    return grouped


def _serialize_case_session(data, now_ts):
    """将单条会话 JSON 规范化为 /case_status 对外结构，并判定停滞状态。"""
    status = (data.get('status') or 'uploading').strip().lower()
    file_size = int(data.get('file_size') or 0)
    uploaded_size = int(data.get('uploaded_size') or 0)
    if status == 'done' and file_size > 0:
        progress = 100
    elif file_size > 0:
        progress = min(100, int(uploaded_size * 100 / file_size))
    else:
        progress = 0
    chunks = data.get('chunks')
    received_chunks = len(chunks) if isinstance(chunks, (list, dict)) else 0
    chunk_total = data.get('chunk_total') or (_chunk_total_for(file_size) if file_size > 0 else None)

    last_ts = _parse_utc_timestamp(data.get('last_updated'))
    start_ts = _parse_utc_timestamp(data.get('start_time'))
    anchor_ts = last_ts if last_ts is not None else start_ts
    idle_seconds = max(0, int(now_ts - anchor_ts)) if anchor_ts is not None else None

    stalled = idle_seconds is not None and (
        (status == 'uploading' and idle_seconds >= STALL_THRESHOLD_SECONDS)
        or (status == 'pending' and idle_seconds >= PROCESSING_STALL_THRESHOLD_SECONDS)
    )

    return {
        'upload_id': data.get('upload_id'),
        'filename': data.get('filename'),
        'status': status,
        'progress': progress,
        'uploaded_size': uploaded_size,
        'file_size': file_size,
        'received_chunks': received_chunks,
        'chunk_total': chunk_total,
        'start_time': _unix_to_utc_iso(data.get('start_time')),
        'last_updated': _unix_to_utc_iso(data.get('last_updated')),
        'finished_at': _unix_to_utc_iso(data.get('finished_at')),
        'idle_seconds': idle_seconds,
        'stalled': stalled,
        'error': data.get('error'),
        'target_path': data.get('target_path'),
        'digest': data.get('digest'),
        'upload_source': data.get('upload_source') or 'unknown',
        'task_id': data.get('task_id'),
        'trace_id': data.get('trace_id'),
    }


def _build_case_status_payload(case_id, raw_sessions, checked_at, now_ts):
    """组装单个工单的完整状态（全部归档 + 全部未过期会话 + 汇总统计）。"""
    archives = _list_case_archives(case_id)
    sorted_raw = sorted(
        raw_sessions,
        key=lambda item: float(item.get('start_time') or 0),
        reverse=True,
    )
    sessions = [_serialize_case_session(item, now_ts) for item in sorted_raw]

    active_upload_count = sum(1 for s in sessions if s['status'] == 'uploading')
    processing_count = sum(1 for s in sessions if s['status'] == 'pending')
    stalled_count = sum(1 for s in sessions if s['stalled'])
    failed_session_count = sum(1 for s in sessions if s['status'] == 'failed')

    # 不对外暴露磁盘绝对路径之外的内部敏感结构：archives 仅保留文件名与元信息
    public_archives = [
        {k: v for k, v in item.items() if k != 'target_path'}
        for item in archives
    ]
    return {
        'case_id': case_id,
        'checked_at': checked_at,
        'trace_id': _trace_id(),
        'archives': public_archives,
        'sessions': sessions,
        'summary': {
            'has_archive': len(public_archives) > 0,
            'archive_count': len(public_archives),
            'active_upload_count': active_upload_count,
            'processing_count': processing_count,
            'stalled_count': stalled_count,
            'failed_session_count': failed_session_count,
        },
    }


@app.route('/archive_status', methods=['GET'])
def archive_status():
    """按产物名回报归档事实，供平台核验 P1 直传是否真的落地。

    为什么必须由服务端回答：P1 链路上平台没有源包副本，acli 报"上传成功"
    是唯一证据。归档件是否存在、多大、什么时候落地，只能在落盘侧看。

    同名不覆盖时会改存 ``{工单号}-{时间戳}.zip``，因此这里取目录内最新一条
    并回传真实文件名——不能假设"文件名 == 工单号 + .zip"。
    """
    filename = (request.args.get('filename') or '').strip()
    if not validate_filename(filename):
        return jsonify({'error': 'Invalid filename'}), 400
    case_id = filename[: -len('.zip')]
    archives = _list_case_archives(case_id)

    if not archives:
        return jsonify({
            'found': False,
            'status': 'missing',
            'filename': filename,
        }), 200

    latest = archives[0]
    return jsonify({
        'found': True,
        'status': 'archived',
        'filename': filename,
        'stored_name': latest['stored_name'],
        'target_path': latest['target_path'],
        'size': latest['size'],
        'mtime': latest['mtime'],
        'finished_at': datetime.fromtimestamp(latest['mtime']).isoformat(),
        # O(1) 读取 .digests.json 缓存摘要，绝不现场哈希大文件
        'sha256': latest['sha256'],
        'upload_source': latest['upload_source'],
        'task_id': latest['task_id'],
        'archive_bind_key': latest['archive_bind_key'],
    }), 200


@app.route('/case_status', methods=['GET'])
def case_status():
    """完整工单状态只读查询：返回该工单的全部归档件 + 全部未过期上传会话 + 停滞判定。"""
    raw_case_id = request.args.get('case_id') or ''
    if '\r' in raw_case_id or '\n' in raw_case_id:
        return jsonify({'error': 'Invalid case_id'}), 400
    case_id = raw_case_id.strip()
    filename = f'{case_id}.zip'
    if not validate_filename(filename):
        return jsonify({'error': 'Invalid case_id'}), 400
    case_ok, case_mode, case_reason = validate_case_id(filename)
    if not case_ok:
        _log_guard('case_id_rejected', file=filename, mode=case_mode,
                   reason=case_reason, metric='case_reject_total')
        return jsonify({'error': 'Invalid case_id', 'reason': case_reason}), 400

    now_ts = time.time()
    checked_at = datetime.fromtimestamp(now_ts, tz=UTC).isoformat()
    grouped = _scan_active_sessions_by_filename({filename})
    payload = _build_case_status_payload(case_id, grouped.get(filename, []), checked_at, now_ts)
    summary = payload['summary']
    app.logger.info(
        f"[REQ] case_status case_id={case_id} archives={summary['archive_count']} "
        f"active={summary['active_upload_count']} processing={summary['processing_count']} "
        f"stalled={summary['stalled_count']} failed={summary['failed_session_count']} "
        f"trace={_trace_id()} metric=case_status_total"
    )
    return jsonify(payload), 200


@app.route('/cases/status/batch', methods=['POST'])
def cases_status_batch():
    """批量工单完整状态查询（供 HTP 后台定期对账使用，单次最多 50 个工单）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'Invalid JSON payload'}), 400
    raw_ids = body.get('case_ids')
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({'error': 'case_ids must be a non-empty list'}), 400

    # 去重保序并逐条严格校验，任意非法即拒绝（fail-closed）
    case_ids = []
    seen = set()
    for raw in raw_ids:
        if not isinstance(raw, str) or '\r' in raw or '\n' in raw:
            return jsonify({'error': 'Invalid case_id in list'}), 400
        cid = raw.strip()
        if cid in seen:
            continue
        fn = f'{cid}.zip'
        if not validate_filename(fn):
            return jsonify({'error': f'Invalid case_id: {cid}'}), 400
        case_ok, case_mode, case_reason = validate_case_id(fn)
        if not case_ok:
            _log_guard('case_id_rejected', file=fn, mode=case_mode,
                       reason=case_reason, metric='case_reject_total')
            return jsonify({'error': f'Invalid case_id: {cid}', 'reason': case_reason}), 400
        seen.add(cid)
        case_ids.append(cid)

    if len(case_ids) > BATCH_STATUS_MAX_CASES:
        return jsonify({
            'error': f'Too many case_ids, max is {BATCH_STATUS_MAX_CASES}',
            'max_cases': BATCH_STATUS_MAX_CASES,
        }), 400

    now_ts = time.time()
    checked_at = datetime.fromtimestamp(now_ts, tz=UTC).isoformat()
    target_filenames = {f'{cid}.zip' for cid in case_ids}
    grouped = _scan_active_sessions_by_filename(target_filenames)

    items = {
        cid: _build_case_status_payload(cid, grouped.get(f'{cid}.zip', []), checked_at, now_ts)
        for cid in case_ids
    }
    app.logger.info(
        f"[REQ] cases_status_batch count={len(case_ids)} "
        f"trace={_trace_id()} metric=cases_status_batch_total"
    )
    return jsonify({
        'checked_at': checked_at,
        'trace_id': _trace_id(),
        'count': len(items),
        'items': items,
    }), 200


def _unix_to_utc_iso(value):
    """把会话时间戳统一成带 +00:00 的 UTC ISO。

    平台 ``_session_matches_generation`` 会拿它和 aware 的 ``created_at``
    比较，返回 naive 字符串会直接抛 TypeError 变成 500；旧会话里若是
    naive ISO，按 5888 本机时区解释后再转 UTC，不能粗暴补 UTC。
    """
    if value is None:
        return None
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return parsed.astimezone(UTC).isoformat()
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _latest_archived(filename):
    """只读地找该工单最新归档件；会话被 24h reaper 清掉后靠它兜底。

    与 ``archive_status`` 同一套命名规则：同名件或 ``{工单号}-{时间戳}.zip``，
    取 mtime 最新。绝不写文件、绝不现算摘要（大包逐字节读会拖垮上传）。
    """
    case_id = filename[: -len('.zip')]
    archives = _list_case_archives(case_id)
    if not archives:
        return None
    latest = archives[0]
    return {
        'path': latest['target_path'],
        'size': latest['size'],
        'mtime_iso': latest['mtime_iso'],
        'sha256': latest['sha256'],
    }


@app.route('/upload_progress', methods=['GET'])
def upload_progress():
    """按产物名回报 P1 直传的真实进度（平台管理端进度代理的数据源）。

    为什么单独做端点：P1 由节点 acli 自己 ``init_upload``，平台拿不到
    ``upload_id``，产物名是两侧唯一公共键；``/upload_status`` 需要
    ``upload_id``，``/archive_status`` 只有「落地/没落地」两态，都回答
    不了「传到百分之几」。

    四条硬约束：
    1. **只读**：不创建/删除会话，不删分片，不改状态。因此绝不能调用
       ``load_upload_session``——它发现会话过期会顺手删会话与分片；
    2. **代际**：同一工单可能有多轮上传，取 ``start_time`` 最新会话，并
       转成 UTC ISO，平台据此与 ``task.created_at`` 对齐，避免把上一轮
       的 100% 当成本轮；
    3. **终态不丢**：会话被 24h reaper 清掉后，归档件仍是权威事实，兜底
       回报 ``status=done`` / ``progress=100``；否则「已交付的 P1」会被
       显示成「暂无记录」，正是 Q2026111000243 那类误判的翻版；
    4. **查不到 ≠ 失败**：两者都无才返回 ``found=false`` 且 HTTP 200，
       平台显示「暂无记录」，不写交付失败。
    """
    filename = (request.args.get('filename') or '').strip()
    if not validate_filename(filename):
        return jsonify({'error': 'Invalid filename'}), 400

    session_store = app.config['SESSION_STORE']
    best = None
    try:
        for name in os.listdir(session_store):
            if not name.endswith('.json'):
                continue
            try:
                with open(os.path.join(session_store, name)) as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                # 上传进程正在原子替换，读到半截或文件消失都只跳过本条
                continue
            if not isinstance(data, dict) or data.get('filename') != filename:
                continue
            # 只读地跳过过期会话；不调用会删文件的 load_upload_session
            last = data.get('last_updated')
            try:
                if last and (datetime.now() - datetime.fromisoformat(str(last))) > timedelta(
                        hours=SESSION_EXPIRE_HOURS):
                    continue
            except (TypeError, ValueError):
                pass
            if best is None or float(data.get('start_time') or 0) > float(
                    best.get('start_time') or 0):
                best = data
    except OSError as exc:
        # 目录暂时不可读：降级成「查不到」，绝不让管理页拿到 500
        app.logger.warning(
            f'[REQ] upload_progress session_store_unavailable err={exc} '
            f'trace={_trace_id()} metric=upload_progress_scan_degraded_total')

    if best is None:
        archived = _latest_archived(filename)
        if archived is None:
            app.logger.info(
                f'[REQ] upload_progress not_found file={filename} '
                f'trace={_trace_id()} metric=upload_progress_not_found_total')
            return jsonify({'found': False, 'filename': filename}), 200
        app.logger.info(
            f'[REQ] upload_progress archived file={filename} '
            f'trace={_trace_id()} metric=upload_progress_archived_total')
        return jsonify({
            'found': True,
            'upload_id': None,
            'filename': filename,
            'status': 'done',
            'uploaded_size': archived['size'],
            'file_size': archived['size'],
            'progress': 100,
            'received_chunks': None,
            'chunk_total': None,
            'error': None,
            'target_path': archived['path'],
            'digest': (
                {'sha256': archived['sha256'], 'size': archived['size']}
                if archived.get('sha256')
                else None
            ),
            'start_time': archived['mtime_iso'],
            'last_updated': archived['mtime_iso'],
            'finished_at': archived['mtime_iso'],
        }), 200

    file_size = int(best.get('file_size') or 0)
    uploaded_size = int(best.get('uploaded_size') or 0)
    progress = min(100, int(uploaded_size * 100 / file_size)) if file_size > 0 else 0
    chunks = best.get('chunks')
    received = len(chunks) if isinstance(chunks, (list, dict)) else 0

    app.logger.info(
        f'[REQ] upload_progress hit file={filename} status={best.get("status")} '
        f'progress={progress} trace={_trace_id()} metric=upload_progress_hit_total')
    return jsonify({
        'found': True,
        'upload_id': best.get('upload_id'),
        'filename': filename,
        'status': best.get('status'),
        'uploaded_size': uploaded_size,
        'file_size': file_size,
        'progress': progress,
        'received_chunks': received,
        'chunk_total': best.get('chunk_total'),
        'error': best.get('error'),
        'target_path': best.get('target_path'),
        'digest': best.get('digest'),
        'start_time': _unix_to_utc_iso(best.get('start_time')),
        'last_updated': best.get('last_updated'),
        'finished_at': _unix_to_utc_iso(best.get('finished_at')),
    }), 200


@app.route('/healthz', methods=['GET'])
def healthz():
    """探活端点：只回报进程存活与目录可写，不做任何重活。

    systemd / 监控探活需要它：`/` 是渲染模板的页面，用它探活既慢又会刷访问日志。
    """
    writable = all(os.access(p, os.W_OK) for p in
                   (UPLOAD_FOLDER, TEMP_FOLDER, SESSION_STORE))
    return jsonify({'status': 'ok' if writable else 'degraded',
                    'writable': writable}), (200 if writable else 503)


@app.errorhandler(404)
def not_found_error(error):
    app.logger.warning(f"404 Not Found: {request.url}")
    return jsonify({'error': 'Resource not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    redacted_headers = {
        k: ('***' if k.lower() in ('authorization', 'cookie', 'x-api-key') else v)
        for k, v in request.headers.items()
    }
    app.logger.error(
        f"500 Internal Server Error: {str(error)}\n"
        f"Request: {request.method} {request.url}\n"
        f"Headers: {redacted_headers}\n"
        f"Form data: {request.form}"
    )
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    app.logger.info("Starting upload server...")
    app.logger.info(f"Upload folder: {UPLOAD_FOLDER}")
    app.logger.info(f"Temp folder: {TEMP_FOLDER}")
    app.logger.info(f"Session store: {SESSION_STORE}")
    app.logger.info(f"Max file size: {MAX_FILE_SIZE} bytes")
    app.logger.info(f"Max case dir bytes: {MAX_CASE_DIR_BYTES} bytes")
    app.logger.info(f"Rate limits: {RATE_LIMITS} window={RATE_WINDOW_SEC}s")
    app.logger.info(
        f"Case id mode: whitelist={TICKET_WHITELIST_FILE or 'disabled'} "
        f"legacy_exempt={LEGACY_CASE_DIR_EXEMPT}")
    app.logger.info(f"Chunk size: {CHUNK_SIZE} bytes")
    app.logger.info(f"Session expire hours: {SESSION_EXPIRE_HOURS}")

    app.run(host='0.0.0.0', port=5888, debug=False)
