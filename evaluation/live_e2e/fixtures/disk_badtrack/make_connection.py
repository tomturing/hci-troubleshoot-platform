#!/usr/bin/env python3
"""为隔离 hci-sim 沙箱签发 lease 并生成 driver `--sim` 所需的 connection.json。

第一性原理：connection 是把「命令真下发到哪个节点、用什么租约认证」这一契约喂给 headless relay，
relay 再转交真实 terminal_bridge 发起 SSH。
- host/port：隔离 hci-sim 的 SSH 端点（terminal_bridge 从此发起 SSH；默认取稳定 Service ClusterIP）；
- username：hci-sim PasswordCallback 硬性要求 == "sim"（非 lease 语义，属服务端约束，见 server.go）；
- password：runtime 现签的 lease token（含 bundle_digest/support_id/variant/node 绑定，TTL 内有效）；
- auth_type：必须为 "lease"。terminal_bridge 的 isSimulationLeaseCredential 仅当
  execution_mode=sim-ssh 且 auth_type=lease 且 password 以 htp2. 开头时，才把密码当仿真租约
  **原样传输**；否则按真实 HCI 密码追加 "sangfornetwork" 后缀，导致 hci-sim 认证失败。
- recommended_commands：SOP#4 会经 bridge 下发的三条裸命令（供 scenario 对齐/诊断展示）。

lease 用隔离 pod 内的 hci-sim 二进制现签（与 pod 同一 HCI_SIM_LEASE_HMAC_KEY，天然匹配）。
注意：`hci-sim lease` 默认读 /etc/hci-sim/fixture-manifest.json，而 bundle 挂在
/etc/hci-sim/bundles/ 下，必须显式覆盖 HCI_SIM_FIXTURE_MANIFEST，否则报「读取 fixture manifest 失败」。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

MANIFEST_IN_POD = "/etc/hci-sim/bundles/kbd-23999-fixture-manifest.json"


def _kubectl(ns: str, *args: str) -> str:
    return subprocess.run(
        ["kubectl", "-n", ns, *args], check=True, capture_output=True, text=True
    ).stdout


def mint_lease(ns: str, pod: str, ttl: str, manifest: str, variant: str) -> str:
    """在隔离 pod 内用其自有 lease key 现签 token，返回最后一行非空（htp2.<claims>.<sig>）。"""
    inner = (
        f"HCI_SIM_FIXTURE_MANIFEST={manifest} "
        f"hci-sim lease --variant {variant} "
        f"--virtual-node SIM-HCI-NODE-01 --container host --ttl {ttl}"
    )
    out = _kubectl(ns, "exec", pod, "--", "sh", "-c", inner)
    token = out.strip().splitlines()[-1].strip()
    if not token.startswith("htp"):
        raise SystemExit(f"lease 签发异常: {out[:200]}")
    return token


def resolve_host(ns: str, pod: str, svc: str) -> str:
    """默认用 Service ClusterIP（跨 ns 稳定可达、pod 重启不变）；无 svc 回落 pod IP。"""
    if svc:
        ip = _kubectl(
            ns, "get", "svc", svc, "-o", "jsonpath={.spec.clusterIP}"
        ).strip()
        if ip and ip != "None":
            return ip
    return _kubectl(ns, "get", "pod", pod, "-o", "jsonpath={.status.podIP}").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="hci-sim-live-e2e")
    ap.add_argument("--pod", default="hci-sim-diskbadtrack")
    ap.add_argument("--svc", default="hci-sim-diskbadtrack", help="留空则用 pod IP")
    ap.add_argument("--host", default=None, help="显式覆盖 SSH host")
    ap.add_argument("--port", type=int, default=2222)
    ap.add_argument("--ttl", default="4h")
    # 其他场景（如 SOP#3 虚拟机开机失败）复用同一套签发流程时，
    # manifest / variant / support_id 必须可覆盖，否则 lease 会绑到磁盘场景的 bundle 上。
    ap.add_argument("--manifest", default=MANIFEST_IN_POD, help="pod 内 fixture manifest 绝对路径")
    ap.add_argument("--variant", default="positive-realistic")
    ap.add_argument("--support-id", dest="support_id", default="23999")
    ap.add_argument(
        "--commands",
        default="acli --formatter json alert get -k 磁盘,acli --formatter json storage asan disk list,smartctl.real -a /dev/sdb",
        help="recommended_commands，逗号分隔",
    )
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "connection.json"))
    args = ap.parse_args()

    host = args.host or resolve_host(args.namespace, args.pod, args.svc)
    token = mint_lease(args.namespace, args.pod, args.ttl, args.manifest, args.variant)
    connection = {
        "support_id": args.support_id,
        "variant": args.variant,
        "recommended_commands": [c.strip() for c in args.commands.split(",") if c.strip()],
        "connection": {
            "host": host,
            "port": args.port,
            "username": "sim",
            "auth_type": "lease",
            "password": token,
            "execution_mode": "sim-ssh",
            "test_run_id": "live-e2e-diskbadtrack",
        },
    }
    Path(args.out).write_text(json.dumps(connection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"connection.json 写入 {args.out}  host={host}:{args.port}  token_len={len(token)}")


if __name__ == "__main__":
    main()
