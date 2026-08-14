"""hci-sim 编译控制面使用的最小策略契约。

这不是第二套 Tool/命令规则。Tool Contract 仍由 Signal Schema 生成；本文件只冻结
Fixture Compiler 与 Runtime 共同必须遵守的安全边界，以便 Bundle 在编译时记录可追溯
的 policy revision，并在该边界变化后被判定为 stale。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any


def hci_sim_policy_contract() -> dict[str, Any]:
    """返回参与 revision 计算的、稳定排序的策略事实。"""

    return {
        "schema_version": 1,
        "execution_mode": "sim-ssh",
        "bundle_read_status": "published",
        "route_match": "exact_route_key",
        "synthetic_route_source": "published_kbd_signal+shared_resolution_runtime+active_tool_revision",
        "route_tool_binding": ["tool_revision", "tool_checksum"],
        "real_hci_fallback": False,
        "lease_binding": [
            "test_run_id",
            "scenario_id",
            "support_id",
            "kbd_revision",
            "bundle_digest",
            "variant",
            "virtual_node_id",
            "container",
            "execution_mode",
        ],
    }


@lru_cache(maxsize=1)
def current_hci_sim_policy_revision() -> str:
    """返回 hci-sim 安全边界的内容指纹，不依赖部署时的 active 指针。"""

    encoded = json.dumps(hci_sim_policy_contract(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
