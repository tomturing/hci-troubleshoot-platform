#!/usr/bin/env python3
"""最小复现：直连 bridge WS，用给定 auth_type 发 ssh_connect，观测 ssh_connected / ssh_error 原始回包。

隔离掉 agent/LLM，只验「bridge → hci-sim SSH」这一段，快速判定 auth_type(password vs lease) 与错误真因。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "backend"))
from evaluation.live_e2e import asserts  # noqa: E402


async def trial(bridge_url: str, origin: str, conn: dict, auth_type: str) -> None:
    c = dict(conn["connection"])
    c["auth_type"] = auth_type
    payload_conn = {**conn, "connection": c}
    async with websockets.connect(bridge_url, origin=origin, open_timeout=20) as ws:
        await ws.send(json.dumps(asserts.bridge_ssh_connect_msg(payload_conn), ensure_ascii=False))
        seen = []
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=40)
                msg = json.loads(raw)
                seen.append(msg)
                if msg.get("type") in ("ssh_connected", "ssh_error"):
                    print(f"[auth_type={auth_type}] -> {json.dumps(msg, ensure_ascii=False)[:300]}")
                    break
        except asyncio.TimeoutError:
            print(f"[auth_type={auth_type}] -> 超时，已收={[m.get('type') for m in seen]}")


async def main() -> None:
    bridge_url = sys.argv[1]
    origin = sys.argv[2]
    conn = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    for at in ("password", "lease"):
        await trial(bridge_url, origin, conn, at)


if __name__ == "__main__":
    asyncio.run(main())
