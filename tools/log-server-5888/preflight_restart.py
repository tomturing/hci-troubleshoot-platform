"""5888 重启/重载硬门禁：只用 5888 主机上的直接证据，任一不过即拒绝。

用法（scp 到 192.168.0.2:/tmp 后）：

    /acli/.venv/bin/python /tmp/preflight_restart.py

为什么以本机证据为准（第一性原理）：重启真正会破坏的是**分片文件与合片
进程**，它们只存在于 5888 主机上；平台 DB 的任务状态是二级间接证据，
既可能滞后，也可能因为 503 被写错（Q2026110600940 已发生过）。因此硬
门禁主体必须是会话活跃度、分片 mtime、合片进程、ESTABLISHED 连接、
healthz 这五项，平台 DB 只作交叉验证（见同目录 README.md）。

只读：不删会话、不删分片、不发任何写请求。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

ACTIVE_WINDOW_SEC = 120
SESSION_STORE = Path(os.environ.get("ACLI_SESSION_STORE", "/tmp/acli_upload_sessions"))
TEMP_FOLDER = Path(os.environ.get("ACLI_TEMP_FOLDER", "/tmp/acli_upload_chunks"))
PORT_HEX = f"{5888:04X}"


def session_activity() -> dict:
    """会话活跃度：last_updated 优先，缺失时退回 start_time（unix 浮点）。"""
    now = time.time()
    active, newest_age, total = [], None, 0
    for path in SESSION_STORE.glob("*.json"):
        total += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 半截 JSON 说明上传进程正在写，保守地按「活跃」处理
            active.append(f"{path.name}:unreadable")
            continue
        stamp = None
        raw = data.get("last_updated")
        if raw:
            try:
                stamp = datetime.fromisoformat(str(raw)).timestamp()
            except (TypeError, ValueError):
                stamp = None
        if stamp is None and data.get("start_time"):
            try:
                stamp = float(data["start_time"])
            except (TypeError, ValueError):
                stamp = None
        if stamp is None:
            continue
        age = now - stamp
        newest_age = age if newest_age is None else min(newest_age, age)
        if age < ACTIVE_WINDOW_SEC:
            active.append(f"{path.name}:{int(age)}s")
    return {
        "ok": not active,
        "total": total,
        "active": active,
        "newest_age_sec": None if newest_age is None else int(newest_age),
        "window_sec": ACTIVE_WINDOW_SEC,
    }


def chunk_activity() -> dict:
    """分片文件新鲜度：合片进程会持有 .part 数十分钟，必须一并看。"""
    now = time.time()
    newest, part_count = None, 0
    if TEMP_FOLDER.is_dir():
        for path in TEMP_FOLDER.glob("*.part"):
            part_count += 1
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            newest = age if newest is None else min(newest, age)
    return {
        "ok": newest is None or newest >= ACTIVE_WINDOW_SEC,
        "part_count": part_count,
        "newest_age_sec": None if newest is None else int(newest),
        "window_sec": ACTIVE_WINDOW_SEC,
    }


def merge_processes() -> dict:
    """合片进程扫描：直接遍历 /proc，避免 pgrep 匹配到命令自身。"""
    hits = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, ValueError):
            continue
        if "process_upload.py" in cmd and "preflight_restart" not in cmd:
            hits.append(f"{entry.name}:{cmd.strip()[:160]}")
    return {"ok": not hits, "hits": hits}


def established_connections() -> dict:
    """/proc/net/tcp{,6} 里本地端口 5888 且 state=01(ESTABLISHED) 的连接。

    内核枚举 ``TCP_ESTABLISHED=1``、``TCP_LISTEN=10``（即 0x0A）。若把
    0A 误当 ESTABLISHED，5888 自己的监听 socket 会被算成「有客户端在传」，
    硬门禁将永远拒绝——这是实测校正过的坑，不要把状态码凭印象写。
    FIN_WAIT1(04) / TIME_WAIT(06) 只作观测，不阻塞重启。
    """
    count = 0
    states: dict[str, int] = {}
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            if parts[1].split(":")[-1].upper() != PORT_HEX:
                continue
            state = parts[3].upper()
            states[state] = states.get(state, 0) + 1
            if state == "01":
                count += 1
    return {
        "ok": count == 0,
        "count": count,
        "port": 5888,
        "states": states,
        "listen_sockets": states.get("0A", 0),
    }


def healthz() -> dict:
    """探活：确认进程还活着，否则「没有在途任务」可能只是服务挂了。"""
    try:
        with urlopen("http://127.0.0.1:5888/healthz", timeout=5) as resp:
            body = resp.read().decode("utf-8", "replace")
            return {"ok": resp.status == 200, "status": resp.status, "body": body[:200]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    checks = {
        "session_activity": session_activity(),
        "chunk_activity": chunk_activity(),
        "merge_processes": merge_processes(),
        "established_connections": established_connections(),
        "healthz": healthz(),
    }
    allowed = all(item["ok"] for item in checks.values())
    report = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "trace_id": f"preflight-5888-{uuid.uuid4().hex[:12]}",
        "session_store": str(SESSION_STORE),
        "temp_folder": str(TEMP_FOLDER),
        "checks": checks,
        "allowed": allowed,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n结论：" + ("允许重启/重载 5888" if allowed else "拒绝重启/重载 5888"))
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
