#!/usr/bin/env python3
"""Native NanoClaw one-shot runner.

This invokes Claude Agent SDK directly (native runtime), keeps session mapping,
and prints plain assistant text for the OpenAI adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from pathlib import Path


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value


def safe_group_name(session: str) -> str:
    raw = (session or "nanoclaw-default").strip()[:80]
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-")
    return cleaned or "nanoclaw-default"


def read_sessions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}


def write_sessions(path: Path, sessions: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--session", default="nc-api-default")
    parser.add_argument("--model", default="nanoclaw")
    args = parser.parse_args()

    timeout_sec = int(env("NANOCLAW_NATIVE_TIMEOUT_SEC", "300"))
    state_dir = Path(env("NANOCLAW_STATE_DIR", "/home/node/.nanoclaw-native"))
    workspace_root = Path(env("NANOCLAW_WORKSPACE_ROOT", "/workspace"))
    runner_user = env("NANOCLAW_RUNNER_USER", "node").strip() or "node"
    runner_home = env("NANOCLAW_RUNNER_HOME", "/home/node")
    query_script = env("NANOCLAW_QUERY_SCRIPT", "/app/nanoclaw_native_query.mjs")

    state_file = state_dir / "sessions.json"
    sessions = read_sessions(state_file)
    logical_session = args.session.strip() or "nc-api-default"
    resume_session = sessions.get(logical_session)

    group_folder = safe_group_name(logical_session)
    cwd = workspace_root / "group" / group_folder
    cwd.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        cwd.chmod(0o777)
        state_dir.chmod(0o777)
    except Exception:
        pass

    query_cmd_parts = [
        "node",
        query_script,
        "--prompt",
        args.prompt,
        "--cwd",
        str(cwd),
    ]
    if resume_session:
        query_cmd_parts += ["--resume", resume_session]

    query_cmd = " ".join(shlex.quote(part) for part in query_cmd_parts)
    cmd = ["su", "-s", "/bin/sh", runner_user, "-c", query_cmd]
    child_env = os.environ.copy()
    child_env["HOME"] = runner_home

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        timeout=timeout_sec,
    )

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
        raise RuntimeError(f"nanoclaw sdk query failed: {detail[-1200:]}")

    output = (proc.stdout or "").strip()
    if not output:
        raise RuntimeError("nanoclaw sdk query empty output")

    last_line = output.splitlines()[-1].strip()
    try:
        payload = json.loads(last_line)
    except json.JSONDecodeError:
        raise RuntimeError(f"nanoclaw sdk invalid output: {output[-1200:]}")

    if not isinstance(payload, dict):
        raise RuntimeError(f"nanoclaw sdk invalid payload type: {type(payload).__name__}")

    answer = str(payload.get("answer") or "").strip()
    new_session_id = str(payload.get("sessionId") or "").strip()

    if new_session_id:
        sessions[logical_session] = new_session_id
        write_sessions(state_file, sessions)

    if not answer:
        raise RuntimeError(f"nanoclaw sdk empty answer: {json.dumps(payload, ensure_ascii=False)}")

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
