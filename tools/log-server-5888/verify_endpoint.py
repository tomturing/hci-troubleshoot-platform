"""在 5888 主机上以隔离环境验证 ``/upload_progress`` 契约（只读、零现网副作用）。

用法（把本文件 scp 到 /tmp 后在 192.168.0.2 执行）：

    /acli/.venv/bin/python /tmp/verify_endpoint.py

为什么用 Flask test_client 而不是打现网 5888：验证必须发生在**重启之前**，
此时现网 worker 还跑旧代码；用独立进程 import 改后 app.py，并把会话、
分片、归档、日志目录全部重定向到临时目录，即使端点有 bug 也不会碰到
真实上传数据。每条断言都对应平台侧 ``destination_progress`` 的真实消费方式。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

CASE = "Q2026111000243"
FILENAME = f"{CASE}.zip"
WORK = Path(tempfile.mkdtemp(prefix="upload_progress_verify_"))
SESSIONS = WORK / "sessions"
CHUNKS = WORK / "chunks"
ARCHIVES = WORK / "archives"
for directory in (SESSIONS, CHUNKS, ARCHIVES, WORK / "logs"):
    directory.mkdir(parents=True, exist_ok=True)

# 必须在 import app.py 之前设置：鉴权关掉，数据目录全部隔离
os.environ["ACLI_LOG_DIR"] = str(WORK / "logs")
os.environ["ACLI_SESSION_STORE"] = str(SESSIONS)
os.environ["ACLI_TEMP_FOLDER"] = str(CHUNKS)
os.environ["ACLI_P1_REQUIRE_AUTH"] = "0"
os.environ.pop("ACLI_UPLOAD_TOKEN", None)
os.environ.pop("ACLI_P1_TICKET_SECRET", None)

sys.path.insert(0, "/acli")
os.chdir("/acli")
import app as srv  # noqa: E402  —— 环境变量必须先于导入设置

srv.UPLOAD_FOLDER = str(ARCHIVES)
srv.SESSION_STORE = str(SESSIONS)
srv.app.config["UPLOAD_FOLDER"] = str(ARCHIVES)
srv.app.config["SESSION_STORE"] = str(SESSIONS)
CLIENT = srv.app.test_client()

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def write_session(name: str, **overrides) -> dict:
    data = {
        "upload_id": name,
        "filename": FILENAME,
        "temp_filepath": str(CHUNKS / f"{name}.part"),
        "file_size": 1000,
        "uploaded_size": 250,
        "chunks": {"0": "a", "1": "b"},
        "chunk_total": 4,
        "start_time": time.time() - 60,
        "last_updated": datetime.now().isoformat(),
    }
    data.update(overrides)
    (SESSIONS / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def clear_sessions() -> None:
    for path in SESSIONS.glob("*.json"):
        path.unlink()


def clear_archives() -> None:
    shutil.rmtree(ARCHIVES, ignore_errors=True)
    ARCHIVES.mkdir(parents=True, exist_ok=True)


def get(filename: str = FILENAME):
    return CLIENT.get(f"/upload_progress?filename={filename}")


def main() -> int:
    check("产物名白名单：非法名 400", get("bad.zip").status_code == 400)
    check("产物名白名单：空名 400", get("").status_code == 400)

    clear_sessions()
    clear_archives()
    resp = get()
    check(
        "无会话无归档 → 200 found=false（不写失败）", resp.status_code == 200 and resp.get_json().get("found") is False
    )

    write_session("uploading")
    resp = get()
    body = resp.get_json()
    check(
        "进行中 → progress=25 / received_chunks=2 / chunk_total=4",
        body["progress"] == 25 and body["received_chunks"] == 2 and body["chunk_total"] == 4,
        f"progress={body['progress']}",
    )
    check(
        "start_time 是带时区的 UTC ISO（平台代际比较不会 TypeError）",
        isinstance(body["start_time"], str) and body["start_time"].endswith("+00:00"),
        str(body["start_time"]),
    )
    check("响应带唯一调用链头 X-Request-ID", bool(resp.headers.get("X-Request-ID")))

    # 代际：同一产物两轮会话，必须取 start_time 最新的一条
    write_session("old", uploaded_size=900, start_time=time.time() - 7200)
    write_session("new", uploaded_size=100, start_time=time.time() - 10)
    check("多轮会话取 start_time 最新一轮", get().get_json()["progress"] == 10)

    clear_sessions()
    write_session(
        "done",
        status="done",
        uploaded_size=1000,
        digest={"sha256": "c" * 64, "md5": "d" * 32, "size": 1000},
        target_path=f"/sf/log/{CASE}/{FILENAME}",
        finished_at=datetime.now().isoformat(),
    )
    body = get().get_json()
    check(
        "done 会话 → status=done / progress=100 / digest 可读",
        body["status"] == "done" and body["progress"] == 100 and body["digest"]["sha256"] == "c" * 64,
    )
    check(
        "finished_at 也转成带时区 ISO", isinstance(body["finished_at"], str) and body["finished_at"].endswith("+00:00")
    )

    # 只读性：过期会话绝不能被端点删掉（load_upload_session 会删文件）
    clear_sessions()
    write_session("stale", last_updated=(datetime.now() - timedelta(hours=48)).isoformat())
    stale_path = SESSIONS / "stale.json"
    before = hashlib.sha256(stale_path.read_bytes()).hexdigest()
    get()
    check(
        "只读约束：过期会话仍在且内容不变（不调用 load_upload_session）",
        stale_path.exists() and hashlib.sha256(stale_path.read_bytes()).hexdigest() == before,
    )

    clear_sessions()
    case_dir = ARCHIVES / CASE
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / f"{CASE}-20261110120000.zip").write_bytes(b"x" * 4096)
    body = get().get_json()
    check(
        "会话被 24h reaper 清掉后归档兜底 → done/100/target_path",
        body["found"] is True
        and body["status"] == "done"
        and body["progress"] == 100
        and body["uploaded_size"] == 4096
        and body["target_path"].endswith(".zip"),
    )
    check(
        "归档兜底的 start_time 带时区，平台可做代际校验",
        isinstance(body["start_time"], str) and body["start_time"].endswith("+00:00"),
    )

    # 会话目录不可读时必须降级，不能 500
    srv.app.config["SESSION_STORE"] = str(WORK / "not-exists")
    clear_archives()
    resp = get()
    check("会话目录不可读 → 200 降级而非 500", resp.status_code == 200 and resp.get_json().get("found") is False)
    srv.app.config["SESSION_STORE"] = str(SESSIONS)

    print(f"\n结果：{len(PASSED)} PASS / {len(FAILED)} FAIL  trace={uuid.uuid4().hex[:12]}")
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(code)
