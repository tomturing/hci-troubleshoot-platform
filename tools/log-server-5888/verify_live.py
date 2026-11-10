"""5888 现网 /upload_progress 端到端只读验证（worker 生效后运行）。

用法：``/acli/.venv/bin/python /tmp/verify_live.py``

与 ``verify_endpoint.py`` 的分工：后者在**重启前**的隔离进程里验证代码逻辑，
本脚本在**现网真实 worker** 上验证鉴权、真实会话/归档、调用链与只读性。
令牌只从本机首页读取，绝不打印。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:5888"
SESSIONS = Path("/tmp/acli_upload_sessions")
SAMPLE = "Q2026111000243.zip"
ABSENT = "Q2026111099999.zip"
TRACE = f"verify-live-{uuid.uuid4().hex[:12]}"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def fetch(path: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
    except Exception as exc:
        return -1, str(exc), {}


def local_token() -> str:
    _, html, _ = fetch("/")
    match = re.search(r'<meta name="upload-token" content="([^"]+)"', html)
    if not match or len(match.group(1)) < 16:
        raise SystemExit("首页未内联有效的本机 token，终止")
    return match.group(1)


def snapshot() -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in SESSIONS.glob("*.json")}


def main() -> int:
    before = snapshot()
    headers = {
        "Authorization": f"Bearer {local_token()}",
        "X-Request-ID": TRACE,
    }

    code, body, resp_headers = fetch(f"/upload_progress?filename={SAMPLE}", headers)
    check("真实 P1 样本 200", code == 200, f"status={code}")
    payload = json.loads(body) if code == 200 else {}
    check(
        "样本 found=true / status=done / progress=100",
        payload.get("found") is True and payload.get("status") == "done" and payload.get("progress") == 100,
        f"status={payload.get('status')} progress={payload.get('progress')}",
    )
    check(
        "start_time 带 +00:00（平台代际比较安全）",
        isinstance(payload.get("start_time"), str) and payload["start_time"].endswith("+00:00"),
        str(payload.get("start_time")),
    )
    check(
        "target_path 是 5888 实际落盘绝对路径",
        str(payload.get("target_path") or "").startswith("/"),
        str(payload.get("target_path"))[:80],
    )
    check(
        "唯一调用链：请求带 X-Request-ID，响应回写同值",
        resp_headers.get("X-Request-ID") == TRACE,
        str(resp_headers.get("X-Request-ID")),
    )

    code, body, _ = fetch(f"/upload_progress?filename={ABSENT}", headers)
    absent = json.loads(body) if code == 200 else {}
    check("不存在的工单 → 200 found=false（不写失败）", code == 200 and absent.get("found") is False, f"status={code}")

    code, _, _ = fetch("/upload_progress?filename=bad.zip", headers)
    check("非法产物名 → 400", code == 400, f"status={code}")

    code, _, _ = fetch(f"/upload_progress?filename={ABSENT}")
    check("无凭据 → 401（端点未绕过鉴权）", code == 401, f"status={code}")

    check("只读性：会话目录文件集合与内容 sha256 前后不变", before == snapshot())

    code, body, _ = fetch("/healthz")
    check("验证窗口内 healthz 仍 200", code == 200 and '"ok"' in body)

    print(
        f"\n结果：{len(PASSED)} PASS / {len(FAILED)} FAIL  trace={TRACE} at={datetime.now().astimezone().isoformat()}"
    )
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
