#!/usr/bin/env python3
"""硬盘坏道真仿真 isolated-sandbox fixture：生成 hci-sim v2.0 realistic Bundle。

第一性原理：
- hci-sim 只服务「route_key 精确匹配」的命令，route_key = (Tool, AcquisitionKey, Argv, Node, Container)，
  其中 Tool=argv[0]、AcquisitionKey=argv[0]:argv[1]、Argv 必须是 NormalizeArgv 后的形态（--k=v→--k v）。
- 硬盘坏道 SOP#4 真正经 bridge 下发到节点采集的裸命令只有 3 条（余为 skill_call/json_extract，不落节点）：
    1) alert_logs : acli --formatter json alert get -k 磁盘
    2) asan_disks : acli --formatter json storage asan disk list
    3) smart_info : smartctl.real -a /dev/{disk_dev}   （disk_dev 由 asan_disks 输出确定性解析为 sdb）
- bundle.digest 由 Go 结构体 json.Marshal 计算，Python 无法可靠复刻；此处留空占位，
  由 `hci-sim manifest-digest`（镜像二进制）算出后回填，并写入 HCI_SIM_REQUIRED_BUNDLES。

设计要点：三条命令的模拟输出构成自洽的坏道证据链——alert 指向 /dev/sdb（槽位/SN），
asan 列表把该盘标记 health=warning、reallocated/pending 非零，smartctl 给出 SMART FAILED 计数，
使 disk_dev→/dev/sdb 的 json_extract 与 smart_info 的 route 严格对齐（最满链路）。
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "kbd-23999-fixture-manifest.json"

SUPPORT_ID = "23999"
VARIANT = "positive-realistic"
NODE = "SIM-HCI-NODE-01"
CONTAINER = "host"

# bundle.digest 由 `hci-sim manifest-digest`（Go 结构体 canonical JSON）权威算出；
# ComputeBundleDigest 会先清空 bundle.digest/signature 再哈希，故回填该值不影响其自身。
BUNDLE_DIGEST = "sha256:b77e5ebc6ae40a9ce5106a25a01559c1960ec4ef5f150e3636f8351fa031ecc4"

# alert get 磁盘：命中一条物理盘介质异常告警，明确指向 /dev/sdb（槽位 SLOT-6 / SN）。
ALERT_STDOUT = json.dumps(
    {
        "data": [
            {
                "alert_id": "ALT-2026-7788",
                "name": "物理磁盘介质异常",
                "severity": "重要",
                "object": "10.0.34.7",
                "cluster": "HCI-Cluster-A",
                "component": "硬盘",
                "description": "物理盘 /dev/sdb（槽位 SLOT-6, SN WCT1ABCD2233）检测到坏道增长，"
                "SMART Reallocated_Sector_Ct=42、Current_Pending_Sector=17",
                "occurred_at": "2026-07-30 10:00:12",
            }
        ]
    },
    ensure_ascii=False,
) + "\n"

# storage asan disk list：仅列该一块故障盘，device=/dev/sdb，使 disk_dev 解析无歧义。
ASAN_STDOUT = json.dumps(
    {
        "data": [
            {
                "host": "10.0.34.7",
                "device": "/dev/sdb",
                "slot": "SLOT-6",
                "sn": "WCT1ABCD2233",
                "model": "ST8000NM0055",
                "capacity": "7.28TB",
                "role": "数据盘",
                "health": "warning",
                "reallocated_sectors": 42,
                "pending_sectors": 17,
                "media_errors": 9,
                "power_on_hours": 45123,
            }
        ]
    },
    ensure_ascii=False,
) + "\n"

# smartctl.real -a /dev/sdb：真实 smartctl 输出摘录（SMART overall FAILED + 坏道相关属性）。
SMART_STDOUT = (
    "smartctl 7.2 2020-12-30 r5155 [x86_64-linux-5.10.0] (local build)\n"
    "Copyright (C) 2002-20, Bruce Allen, Christian Franke, www.smartmontools.org\n"
    "\n"
    "=== START OF INFORMATION SECTION ===\n"
    "Model Family:     Seagate Enterprise Capacity 3.5 HDD (HV)\n"
    "Device Model:     ST8000NM0055-1QQ117\n"
    "Serial Number:    WCT1ABCD2233\n"
    "LU WWN Device Id: 5 000c50 0a1b2c3d4\n"
    "User Capacity:    8,001,563,222,016 bytes [8.00 TB]\n"
    "Rotation Rate:    7200 rpm\n"
    "Form Factor:      3.5 inches\n"
    "=== START OF SMART DATA SECTION ===\n"
    "SMART overall-health self-assessment test result: FAILED!\n"
    "\n"
    "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE\n"
    "  5 Reallocated_Sector_Ct   0x0033   078   078   036    Pre-fail  Always       -       42\n"
    "187 Reported_Uncorrect      0x0032   094   094   000    Old_age   Always       -       9\n"
    "197 Current_Pending_Sector  0x0012   094   094   000    Old_age   Always       -       17\n"
    "198 Offline_Uncorrectable   0x0010   094   094   000    Old_age   Offline      -       9\n"
    "\n"
    "SMART Error Log Version: 1\n"
    "ATA Error Count: 9 (device had unrecovered read errors / bad sectors)\n"
)


def _route(rid: str, signal_id: str, argv: list[str], stdout: str) -> dict:
    """按 hci-sim route_key 语义构造 route：Tool=argv[0]，AcquisitionKey=argv[0]:argv[1]。"""
    return {
        "id": rid,
        "signal_id": signal_id,
        "variant": VARIANT,
        "route_key": {
            "tool": argv[0],
            "acquisition_key": f"{argv[0]}:{argv[1]}",
            "argv": argv,
            "node": NODE,
            "container": CONTAINER,
        },
        "result": {"exit_code": 0, "stdout": stdout},
        "stream": {"chunk_bytes": 96},
        "fault": {"type": "none"},
    }


def build() -> dict:
    routes = [
        _route(
            "sig-001-alert-disk-23999",
            "sig_001",
            ["acli", "--formatter", "json", "alert", "get", "-k", "磁盘"],
            ALERT_STDOUT,
        ),
        _route(
            "sig-002-asan-disk-list-23999",
            "sig_002",
            ["acli", "--formatter", "json", "storage", "asan", "disk", "list"],
            ASAN_STDOUT,
        ),
        _route(
            "sig-003-smart-23999",
            "sig_003",
            ["smartctl.real", "-a", "/dev/sdb"],
            SMART_STDOUT,
        ),
    ]
    manifest = {
        "schema_version": "2.0",
        "bundle": {"digest": BUNDLE_DIGEST, "status": "published"},
        "kbd": {
            "support_id": SUPPORT_ID,
            "revision": 1,
            "checksum": "sha256:" + ("0" * 64),
        },
        "contracts": {
            "tool_revision": "cf416e14cccccf8b5427cf3ae9e6e4d69eb56566ef49260399ce9ac12bf27c44",
            "policy_revision": "01065eed83edb5dcdc203f57b78c1446cca0a37bd204ec2ec0849230a9b2e451",
        },
        "limits": {
            "max_routes": 32,
            "max_output_bytes_per_command": 8192,
            "max_bundle_bytes": 65536,
        },
        "variables": {},
        "routes": routes,
    }
    return manifest


def main() -> None:
    manifest = build()
    OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"生成 {OUT}")
    print("下一步：用 hci-sim 镜像跑 `hci-sim manifest-digest --manifest <file>` 算 digest，回填 bundle.digest。")


if __name__ == "__main__":
    main()
