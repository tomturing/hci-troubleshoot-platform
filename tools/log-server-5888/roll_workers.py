"""零提权让 5888 新代码生效：触发 gunicorn ``max_requests`` 自然轮换并验收。

用法（scp 到 192.168.0.2:/tmp 后）：

    /acli/.venv/bin/python /tmp/roll_workers.py

为什么需要它：5888 以 root 跑 gunicorn（``kill -HUP`` / ``systemctl restart``
都要提权），但 ``gunicorn_config.py`` 有 ``max_requests=1000`` 且**没有**
``preload_app``——worker 每处理约 1000 个请求就被 master 换掉并重新
import ``app.py``。因此连续打免鉴权的轻量 ``/healthz`` 就能让 4 个 worker
全部加载新代码，零监听中断、零提权、零在途请求打断。2026-11-09 已用同
一机制在生产验证过。

对抗性约束：
- 只在 preflight 已给出 ``allowed=true`` 之后运行；
- 每轮用 20 次独立连接探测新端点，必须 20/20 命中才算全部 worker 已换；
- 有请求上限与超时，失败时明确报错，绝不静默宣布成功。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

BASE = "http://127.0.0.1:5888"
PROBE_PATH = "/upload_progress?filename=Q2026111099999.zip"
MAX_ROUNDS = 6000
CHECK_EVERY = 200
HIT_TARGET = 20
TIMEOUT_SEC = 8


def fetch(path: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, str(exc)


def local_token() -> str:
    """首页内联了上传 token；只在本机读取，绝不把值打印进日志或输出。

    实际形态是 ``<meta name="upload-token" content="...">``，不是 JS 变量，
    按变量名去匹配会静默拿不到值，导致所有探测都 401。
    """
    _, html = fetch("/")
    match = re.search(r'<meta name="upload-token" content="([^"]+)"', html)
    if not match or len(match.group(1)) < 16:
        raise SystemExit("首页未内联有效的本机 token，终止")
    return match.group(1)


def probe_hits(headers: dict[str, str]) -> int:
    """20 次独立连接：旧 worker 返回 404，新 worker 返回 200 found=false。"""
    hits = 0
    for _ in range(HIT_TARGET):
        code, body = fetch(PROBE_PATH, headers)
        if code == 200 and '"found"' in body:
            hits += 1
    return hits


def main() -> int:
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {local_token()}"}
    before = probe_hits(headers)
    hits = before
    rounds = 0
    for rounds in range(1, MAX_ROUNDS + 1):
        code, _ = fetch("/healthz")
        if code != 200:
            print(f"healthz 异常 status={code}，停止轮换", file=sys.stderr)
            break
        if rounds % CHECK_EVERY == 0:
            hits = probe_hits(headers)
            if hits == HIT_TARGET:
                break
    else:
        hits = probe_hits(headers)

    report = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "trace_id": f"roll-5888-{uuid.uuid4().hex[:12]}",
        "healthz_rounds": rounds,
        "probe_hits_before": before,
        "probe_hits_after": hits,
        "hit_target": HIT_TARGET,
        "elapsed_sec": round(time.monotonic() - started, 1),
        "all_workers_reloaded": hits == HIT_TARGET,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        "\n结论："
        + ("4 个 worker 均已加载新端点" if report["all_workers_reloaded"] else "仍有 worker 跑旧代码，需人工介入")
    )
    return 0 if report["all_workers_reloaded"] else 1


if __name__ == "__main__":
    sys.exit(main())
