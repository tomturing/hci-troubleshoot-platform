"""关键信号输出契约的无副作用辅助函数。"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)(?:\.[A-Z0-9_]+)*\}\}")


def derive_signal_requires(signal: dict[str, Any]) -> list[str]:
    """从 QFK 的采集参数和文本提取规则推导输入变量。

    qkv_vm_console 的 host/vm_id 参数同样引用 {{HOST}}/{{VM_ID}} 占位符；
    qkv_effect 的期望锚点（观测参数、matcher 阈值等）也可引用变量占位符；
    与显式 requires 合并扫描，兜底前端漏同步导致的变量依赖缺失。
    """

    acquire = signal.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    explicit_requires = list((signal.get("orchestrate") or {}).get("requires") or [])
    if not tool.startswith(("qfk_", "qkv_")):
        return explicit_requires

    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.extend(_PLACEHOLDER_RE.findall(value))
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(acquire.get("args") or {})
    if tool.startswith("qfk_"):
        # Matcher 的数值阈值同样可能引用上游变量，不能只扫描第一步取值配置。
        collect(signal.get("match") or {})
        for produce in ((signal.get("orchestrate") or {}).get("produces") or []):
            if isinstance(produce, dict):
                collect(produce.get("extract") or {})
        return sorted(set(values))
    if tool.startswith("qkv_"):
        orchestrate = signal.get("orchestrate") or {}
        # QKV parser 先按标准 name 投影本信号内部记录；alias 仅在后处理结束、
        # 写入跨信号变量池时生效，不能冒充 output_processing 的本地字段。
        local = {
            str(item.get("name") or "").strip().upper()
            for item in (orchestrate.get("produces") or [])
            if isinstance(item, dict)
            if str(item.get("name") or "").strip()
        }
        derived: set[str] = set()
        for item in orchestrate.get("output_processing") or []:
            if not isinstance(item, dict):
                continue
            collect(item.get("input"))
            # 断言判断的 matcher 同样可能引用上游变量
            if item.get("mode") == "assert":
                collect(item.get("match"))
            target = str(item.get("name") or item.get("target_variable") or "").strip().upper()
            if item.get("mode") == "derive" and target:
                derived.add(target)
        # QKV 后处理的 input 优先引用本信号的 produces/前序 derive，只有其余
        # 占位符才是外部依赖，避免把同一 Signal 内部流水线误报为上游输入。
        return sorted(set(values) - local - derived | set(explicit_requires))
    # qkv_vm_console / qkv_effect：显式 requires 与占位符扫描合并，
    # 保证 HOST/VM_ID 与期望锚点变量依赖不缺失。
    return sorted(set(explicit_requires) | set(values))


def sync_signal_requires(signal: dict[str, Any]) -> list[str]:
    """就地同步 ``orchestrate.requires`` 并返回推导结果。"""

    requires = derive_signal_requires(signal)
    tool = str((signal.get("acquire") or {}).get("tool") or "")
    if tool.startswith("qfk_") or tool.startswith("qkv_"):
        signal.setdefault("orchestrate", {})["requires"] = requires
    return requires


def is_diagnostic_phase(signal: dict[str, Any]) -> bool:
    """信号是否处于 diagnostic 阶段（phase 缺省视为 diagnostic）。

    仅 diagnostic 阶段的产出才对同阶段消费者可用；remediation 产出在排障时尚不存在。
    与 ``signal_schema._collect_variable_sources`` 同源口径。
    """

    orchestrate = signal.get("orchestrate") or {}
    return str(orchestrate.get("phase") or "diagnostic") == "diagnostic"


def collect_signal_produces(signal: dict[str, Any]) -> set[str]:
    """单信号产出的变量名集合（统一大写）。

    - ``orchestrate.produces`` 元素：dict 取 ``alias or name``（alias 优先，与跨信号变量池写入时机一致），
      非 dict 直接取字面量；
    - ``qkv_*`` 生产者：产出 END 时按平台规范自动派生 DATE（qfk_log 自然日轮转必须）；
    - ``output_processing`` 的 ``derive`` 目标（``name`` 规范字段 / ``target_variable`` 历史兼容）。

    与 ``signal_schema._collect_variable_sources`` 完全同源，根因闭包与关键信号闭包共用此单一事实源。
    """

    orchestrate = signal.get("orchestrate") or {}
    produces: set[str] = set()
    for item in orchestrate.get("produces") or []:
        if isinstance(item, dict):
            effective = str(item.get("alias") or "").strip().upper() or str(item.get("name") or "").strip().upper()
            if effective:
                produces.add(effective)
        elif str(item).strip():
            produces.add(str(item).strip().upper())
    if str((signal.get("acquire") or {}).get("tool") or "").startswith("qkv_"):
        if "END" in produces:
            produces.add("DATE")
        for item in orchestrate.get("output_processing") or []:
            if isinstance(item, dict) and item.get("mode") == "derive":
                target = str(item.get("name") or item.get("target_variable") or "").strip().upper()
                if target:
                    produces.add(target)
    return produces


def collect_produced_vars(signals: Any) -> set[str]:
    """聚合 diagnostic 阶段全部信号的产出变量并集（大写）。

    闭包可用来源的「生产者」部分；remediation 阶段与非 dict 元素跳过。
    """

    produced: set[str] = set()
    for signal in signals or []:
        if isinstance(signal, dict) and is_diagnostic_phase(signal):
            produced |= collect_signal_produces(signal)
    return produced


def collect_declared_external(verification_contract: Any) -> set[str]:
    """从 ``verification_contract.variables`` 取显式外部声明变量集（大写）。

    闭包可用来源的「外部声明」部分（严格口径，不含无 Contract 历史数据的兼容兜底）。
    """

    contract = verification_contract or {}
    return {str(name).strip().upper() for name in (contract.get("variables") or {}) if str(name).strip()}
