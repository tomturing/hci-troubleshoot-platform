"""专家编辑的 Signal 证据角色与 Agent 执行契约同步。

``signal.role`` 是专家可见、可编辑的唯一角色事实来源；
``verification_contract.evidence_policy`` 是提供给 Agent 的派生投影。两者若由
不同操作分别维护，删除或改角色后就会出现 Contract 指向不存在 Signal 的幽灵
引用。因此所有专家保存路径都必须经过本模块的无副作用归一化。
"""

from __future__ import annotations

import copy
from typing import Any

EVIDENCE_ROLES = ("must", "should", "exclude", "context")


def reconcile_verification_contract(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """按存活 Signal 的稳定 id + role 重建 ``evidence_policy``。

    保留 Contract 的案例范围、变量声明等非证据策略字段。为兼容早期数据，缺失的
    ``signal.role`` 优先从旧 policy 回填，仍无法判定时按保守的 ``must`` 处理。
    返回 canonical 文档和可直接映射到专家 UI 的影响摘要；不会修改传入对象。
    """

    result = copy.deepcopy(document)
    raw_signals = result.get("signals")
    if not isinstance(raw_signals, list):
        return result, _empty_impact()

    previous_contract = result.get("verification_contract")
    previous_contract = previous_contract if isinstance(previous_contract, dict) else {}
    previous_policy = previous_contract.get("evidence_policy")
    previous_policy = previous_policy if isinstance(previous_policy, dict) else {}
    previous_by_id = {
        str(signal_id): role
        for role in EVIDENCE_ROLES
        for signal_id in (previous_policy.get(role) or [])
        if isinstance(signal_id, str) and signal_id.strip()
    }

    policy: dict[str, list[str]] = {role: [] for role in EVIDENCE_ROLES}
    for signal in raw_signals:
        if not isinstance(signal, dict):
            continue
        signal_id = str(signal.get("id") or "").strip()
        if not signal_id:
            # 稳定 ID 仍由发布门禁负责。草稿阶段不凭空生成 ID，避免服务器生成的
            # 随机标识让专家无法追溯其来源。
            continue
        role = str(signal.get("role") or previous_by_id.get(signal_id) or "must").lower()
        if role not in EVIDENCE_ROLES:
            role = "must"
        signal["role"] = role
        policy[role].append(signal_id)

    has_contract = bool(previous_contract) or bool(raw_signals)
    if not has_contract:
        return result, _empty_impact()

    try:
        previous_minimum = max(0, int(previous_policy.get("minimum_should", 0)))
    except (TypeError, ValueError):
        previous_minimum = 0
    minimum_should = min(previous_minimum, len(policy["should"]))

    contract = {
        key: copy.deepcopy(value)
        for key, value in previous_contract.items()
        if key != "evidence_policy"
    }
    contract.setdefault("schema_version", 1)
    contract["evidence_policy"] = {
        **policy,
        "minimum_should": minimum_should,
        "on_missing_must": "inconclusive",
    }
    result["verification_contract"] = contract

    before = {
        role: [str(item) for item in previous_policy.get(role) or [] if isinstance(item, str)]
        for role in EVIDENCE_ROLES
    }
    return result, {
        "before": {role: len(before[role]) for role in EVIDENCE_ROLES},
        "after": {role: len(policy[role]) for role in EVIDENCE_ROLES},
        "removed_references": sorted(
            {
                signal_id
                for role in EVIDENCE_ROLES
                for signal_id in before[role]
                if signal_id not in {item for values in policy.values() for item in values}
            }
        ),
        "minimum_should_before": previous_minimum,
        "minimum_should_after": minimum_should,
    }


def expert_editor_issues(document: dict[str, Any]) -> list[dict[str, str]]:
    """返回专家可处理的发布前问题，绝不暴露 Schema 内部字段路径。"""

    signals = document.get("signals") if isinstance(document, dict) else []
    contract = document.get("verification_contract") if isinstance(document, dict) else {}
    # 尚未进入 Expert/LLM v2 Contract 的历史只读记录仍按既有发布规则验证；首次
    # 专家保存会补全 Contract。不能因为页面只读校验而把这类兼容记录误报为需修改。
    if not isinstance(contract, dict) or not isinstance(contract.get("evidence_policy"), dict):
        return []
    policy = contract["evidence_policy"]
    issues: list[dict[str, str]] = []
    if signals and not (policy.get("must") or []):
        issues.append(
            {
                "code": "NO_MUST_SIGNAL",
                "severity": "error",
                "message": "发布前请至少保留一条“必要证据”。",
                "action": "将一条可执行关键信号的“证据作用”改为“必要证据”，或新增一条必要证据。",
            }
        )
    return issues


def _empty_impact() -> dict[str, Any]:
    return {
        "before": {role: 0 for role in EVIDENCE_ROLES},
        "after": {role: 0 for role in EVIDENCE_ROLES},
        "removed_references": [],
        "minimum_should_before": 0,
        "minimum_should_after": 0,
    }
