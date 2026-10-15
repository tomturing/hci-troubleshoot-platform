#!/usr/bin/env python3
"""infra 级直连验证：以 lease token 作密码 SSH 进隔离 hci-sim，跑硬盘坏道 3 命令，确认 route 命中并回真模拟输出。

token 经环境变量注入（不落 argv/日志）。这是把「bundle 路由正确性」与「bridge 接线」解耦的最底层验证。
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncssh

HOST = os.environ["SIM_SSH_HOST"]
PORT = int(os.environ.get("SIM_SSH_PORT", "2222"))
TOKEN = os.environ["SIM_LEASE_TOKEN"]

COMMANDS = [
    "acli --formatter json alert get -k 磁盘",
    "acli --formatter json storage asan disk list",
    "smartctl.real -a /dev/sdb",
]


async def main() -> int:
    async with asyncssh.connect(
        HOST, port=PORT, username="sim", password=TOKEN,
        client_keys=None, known_hosts=None,
    ) as conn:
        ok = 0
        for cmd in COMMANDS:
            try:
                res = await conn.run(cmd, check=False, timeout=20)
                out = (res.stdout or "") + (res.stderr or "")
                hit = bool(out.strip()) and res.exit_status == 0
                print(f"\n$ {cmd}\n  exit={res.exit_status} bytes={len(out)} route_hit={hit}")
                print("  " + out.replace("\n", "\n  ")[:400])
                ok += 1 if hit else 0
            except Exception as exc:  # noqa: BLE001
                print(f"\n$ {cmd}\n  ERROR {type(exc).__name__}: {exc}")
        print(f"\n=== 命中 {ok}/{len(COMMANDS)} 条命令 route ===")
        return 0 if ok == len(COMMANDS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
