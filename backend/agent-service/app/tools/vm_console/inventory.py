"""qkv_vm_console 目标定位与 Inventory 校验（设计文档 §3.1）。

执行前必须同时满足：
1. HOST 来自平台 Inventory 验证后的宿主机节点标识；不接受未经验证的用户主机名；
2. VM_ID 是精确 VMID（数值），不接受模糊 VM 名称；
3. Inventory 显示该 VMID 当前归属该宿主机；
4. 任一环节无法验证均不得执行截图（fail-closed）。

验证来源优先级：平台上下文/已执行生产者变量 > 受控对象查询（SCP 云端清单）>
用户显式填写并经 Inventory 校验。本模块只做"来源可信性"校验，不改变授权关系
（租户/工单授权由上游会话上下文保证）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from shared.observability.logger import get_logger
from shared.schemas.acquirer_args import (
    VM_CONSOLE_HOST_LITERAL_PATTERN,
    VM_CONSOLE_HOST_PLACEHOLDER,
    VM_CONSOLE_VM_ID_LITERAL_PATTERN,
    VM_CONSOLE_VM_ID_PLACEHOLDER,
)

logger = get_logger("vm-console-inventory")


@dataclass
class TargetVerification:
    """目标验证快照（随 vm_console_capture.target_verification 入库）。"""

    verified: bool
    host_node_id: str = ""
    vm_id: str = ""
    source: str = ""  # scp_inventory | sim_inventory | unresolved
    detail: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def validate_target_shape(host: str, vm_id: str) -> tuple[bool, str]:
    """静态形态校验：占位符必须已被变量池解析为字面量。"""

    host = str(host or "").strip()
    vm_id = str(vm_id or "").strip()
    if not host or host == VM_CONSOLE_HOST_PLACEHOLDER:
        return False, "HOST 未解析：缺少可信宿主机来源（TARGET_CONTEXT_MISSING）"
    if not vm_id or vm_id == VM_CONSOLE_VM_ID_PLACEHOLDER:
        return False, "VM_ID 未解析：缺少可信 VMID 来源（TARGET_CONTEXT_MISSING）"
    if not VM_CONSOLE_HOST_LITERAL_PATTERN.fullmatch(host):
        return False, f"宿主机标识不安全: {host}"
    if not VM_CONSOLE_VM_ID_LITERAL_PATTERN.fullmatch(vm_id):
        return False, f"VMID 不是精确数值标识: {vm_id}"
    return True, ""


def _sim_inventory() -> dict[str, str]:
    """仅用于仿真/开发环境的显式 Inventory 映射（生产环境必须为空）。

    格式：``VM_ID=HOST,VM_ID=HOST``。该映射是受控 fixture，不是对校验的绕过：
    未配置时一律走 SCP 真实清单校验。
    """

    raw = os.getenv("VM_CONSOLE_SIM_INVENTORY", "").strip()
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for pair in raw.split(","):
        if "=" in pair:
            vm, host = pair.split("=", 1)
            if vm.strip() and host.strip():
                mapping[vm.strip()] = host.strip()
    return mapping


async def verify_vm_target(
    host: str,
    vm_id: str,
    *,
    scp_client: Any | None = None,
) -> TargetVerification:
    """校验 VMID 当前归属目标宿主机；任一环节不可验证即 fail-closed。"""

    ok, reason = validate_target_shape(host, vm_id)
    if not ok:
        return TargetVerification(verified=False, host_node_id=str(host or ""), vm_id=str(vm_id or ""), reason=reason)

    host = host.strip()
    vm_id = vm_id.strip()

    # 仿真 Inventory（仅显式配置时生效；生产为空）。
    sim_mapping = _sim_inventory()
    if vm_id in sim_mapping:
        expected_host = sim_mapping[vm_id]
        if expected_host == host:
            return TargetVerification(
                verified=True, host_node_id=host, vm_id=vm_id, source="sim_inventory",
                detail={"note": "VM_CONSOLE_SIM_INVENTORY fixture"},
            )
        return TargetVerification(
            verified=False, host_node_id=host, vm_id=vm_id, source="sim_inventory",
            reason=f"VMID 归属不匹配：期望 {expected_host}，实际 {host}（TARGET_OWNERSHIP_MISMATCH）",
        )

    # SCP 云端清单：VM → host_name 权威事实源。
    if scp_client is not None:
        try:
            payload = await scp_client.get_vm_list(limit=200)
            vms = (payload or {}).get("data") or (payload or {}).get("items") or []
            for vm in vms:
                if not isinstance(vm, dict):
                    continue
                if str(vm.get("id") or "") == vm_id:
                    host_name = str(vm.get("host_name") or "").strip()
                    if host_name and (host_name == host or host_name.lower() == host.lower()):
                        return TargetVerification(
                            verified=True, host_node_id=host, vm_id=vm_id, source="scp_inventory",
                            detail={"vm_name": vm.get("name"), "cluster": vm.get("cluster_name")},
                        )
                    return TargetVerification(
                        verified=False, host_node_id=host, vm_id=vm_id, source="scp_inventory",
                        reason=(
                            f"VMID {vm_id} 当前归属 {host_name or '<未知>'}，与目标 {host} 不一致"
                            "（迁移竞争窗口内需要重新确认；TARGET_OWNERSHIP_MISMATCH）"
                        ),
                    )
            return TargetVerification(
                verified=False, host_node_id=host, vm_id=vm_id, source="scp_inventory",
                reason=f"Inventory 中不存在 VMID {vm_id}（TARGET_OWNERSHIP_MISMATCH）",
            )
        except Exception as exc:
            logger.warning("vm_console_inventory_scp_failed", error=str(exc), vm_id=vm_id)
            return TargetVerification(
                verified=False, host_node_id=host, vm_id=vm_id, source="scp_inventory",
                reason=f"Inventory 查询失败，不能验证目标归属: {exc}",
            )

    # 无任何可信 Inventory 来源：fail-closed。
    return TargetVerification(
        verified=False, host_node_id=host, vm_id=vm_id, source="unresolved",
        reason="缺少可信 Inventory 来源（SCP 未配置），无法验证 VMID 归属（TARGET_CONTEXT_MISSING）",
    )
