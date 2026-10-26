"""SOP 变量包服务。

变量包解决的是"同一份通用变量被复制粘贴到多个 SOP"导致的漂移问题：
SOP#1/#2/#3 曾各存一份 22 个同名变量，三份全部是
``tool_call + acquisition_tool=NULL``，一次整改要改 66 处、漏两处就复发。

设计约束
--------
1. **包不是运行时依赖**。展开后变量写入 ``sop_document.variable_schema``，
   agent-service 仍只读扁平数组，运行时零改动。
2. **SOP 自有定义优先**。同名变量以 SOP 内声明为准（允许局部覆盖），
   冲突通过 ``conflicts`` 显式返回，不静默覆盖。
3. **漂移可检测**。``detect_drift`` 比对包内定义与 SOP 实际定义，
   供 CI 与运维脚本发现"包升级了但 SOP 没同步"。
"""

from __future__ import annotations

from typing import Any

from shared.models.sop_variable import VAR_NAME_RE, validate_variable_schema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sop_document import SopDocument, SopVariablePack


class VariablePackError(ValueError):
    """变量包不存在或不可用。"""


async def list_packs(session: AsyncSession, *, active_only: bool = True) -> list[SopVariablePack]:
    stmt = select(SopVariablePack).order_by(SopVariablePack.pack_name)
    if active_only:
        stmt = stmt.where(SopVariablePack.is_active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_pack(session: AsyncSession, pack_name: str) -> SopVariablePack:
    result = await session.execute(select(SopVariablePack).where(SopVariablePack.pack_name == pack_name))
    pack = result.scalar_one_or_none()
    if pack is None:
        raise VariablePackError(f"变量包 '{pack_name}' 不存在")
    if not pack.is_active:
        raise VariablePackError(f"变量包 '{pack_name}' 已停用")
    return pack


def expand_pack_variables(
    pack_variables: list[dict[str, Any]],
    own_variables: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """把包内变量合入 SOP 自有变量。

    规则：
      - 包变量按原顺序在前，SOP 自有变量在后；
      - 同名时以 **SOP 自有定义** 为准（局部覆盖），并把变量名记入 conflicts；
      - ``deprecated`` 的包变量不注入（避免给新 SOP 塞历史包袱）。

    Returns:
        (合并后的变量列表, 被 SOP 覆盖的变量名列表)
    """
    own_by_name = {item.get("name"): item for item in own_variables if isinstance(item, dict) and item.get("name")}

    merged: list[dict[str, Any]] = []
    conflicts: list[str] = []
    seen: set[str] = set()

    for item in pack_variables:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name or not VAR_NAME_RE.match(str(name)):
            continue
        if item.get("deprecated"):
            continue
        if name in own_by_name:
            # 交给后面的自有定义，避免重复声明
            conflicts.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        merged.append(dict(item))

    for item in own_variables:
        if isinstance(item, dict) and item.get("name") in seen:
            continue
        merged.append(item)

    return merged, conflicts


async def apply_pack(
    session: AsyncSession,
    document: SopDocument,
    pack_name: str,
    *,
    overwrite: bool = False,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """把变量包应用到指定 SOP，返回 (新变量列表, 冲突变量名, 包引用记录)。

    ``overwrite=True`` 时包内定义覆盖 SOP 同名变量（用于"包升级后强制对齐"）；
    默认 False 保留 SOP 自有定义，只补充缺失变量。
    """
    pack = await get_pack(session, pack_name)

    own_variables = list(document.variable_schema or [])
    if overwrite:
        # 强制对齐：去掉与包同名的自有定义，交由包定义接管
        pack_names = {
            item.get("name") for item in (pack.variables or []) if isinstance(item, dict) and item.get("name")
        }
        own_variables = [
            item for item in own_variables if not (isinstance(item, dict) and item.get("name") in pack_names)
        ]
        merged, conflicts = expand_pack_variables(pack.variables or [], own_variables)
    else:
        merged, conflicts = expand_pack_variables(pack.variables or [], own_variables)

    # 合并结果必须通过契约校验，否则拒绝落库
    report = validate_variable_schema(merged, mode="save")
    report.raise_if_invalid()

    reference = {"pack": pack.pack_name, "version": pack.version}
    return merged, conflicts, reference


def merge_pack_reference(existing: list[dict[str, Any]] | None, reference: dict[str, Any]) -> list[dict[str, Any]]:
    """更新 applied_packs（同名包只保留最新版本）。"""
    current = [item for item in (existing or []) if isinstance(item, dict)]
    current = [item for item in current if item.get("pack") != reference.get("pack")]
    current.append(reference)
    return current


async def detect_drift(session: AsyncSession, document: SopDocument) -> list[dict[str, Any]]:
    """检测 SOP 引用的变量包是否落后于包定义。

    返回漂移项列表，每项形如::

        {"pack": "hci_common_vt_diag", "variable": "qemu_log_path",
         "reason": "strategy_mismatch", "expected": "path_template", "actual": "tool_call"}

    用于 CI 门禁与运维巡检：包升级后未同步的 SOP 会被点名。
    """
    refs = [item for item in (document.applied_packs or []) if isinstance(item, dict)]
    if not refs:
        return []

    own_by_name = {
        item.get("name"): item
        for item in (document.variable_schema or [])
        if isinstance(item, dict) and item.get("name")
    }

    drifts: list[dict[str, Any]] = []
    for ref in refs:
        pack_name = ref.get("pack")
        if not pack_name:
            continue
        try:
            pack = await get_pack(session, pack_name)
        except VariablePackError:
            drifts.append({"pack": pack_name, "variable": None, "reason": "pack_missing"})
            continue

        if int(ref.get("version") or 0) != pack.version:
            drifts.append(
                {
                    "pack": pack_name,
                    "variable": None,
                    "reason": "version_outdated",
                    "expected": pack.version,
                    "actual": ref.get("version"),
                }
            )

        for pack_var in pack.variables or []:
            if not isinstance(pack_var, dict) or pack_var.get("deprecated"):
                continue
            name = pack_var.get("name")
            actual = own_by_name.get(name)
            if actual is None:
                drifts.append({"pack": pack_name, "variable": name, "reason": "variable_missing"})
                continue
            for field in ("acquisition_strategy", "acquisition_tool", "output_path", "expression"):
                expected_value = pack_var.get(field)
                actual_value = actual.get(field)
                if expected_value != actual_value:
                    drifts.append(
                        {
                            "pack": pack_name,
                            "variable": name,
                            "reason": f"{field}_mismatch",
                            "expected": expected_value,
                            "actual": actual_value,
                        }
                    )

    return drifts


__all__ = [
    "VariablePackError",
    "apply_pack",
    "detect_drift",
    "expand_pack_variables",
    "get_pack",
    "list_packs",
    "merge_pack_reference",
]
