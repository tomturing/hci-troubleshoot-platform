"""根因信号「完整闭环」评估器 — 单一事实源纯函数。

设计依据：docs/solution/knowledge-base/根因信号抽取闭环方案.md。

核心不变量（可执行根因的真实定义）：
    一条 executable 根因 C 在 KBD K 内「可执行」当且仅当
        requires(C) ⊆ Produces(K) ∪ DeclaredExternal(K) ∪ ContextWhitelist
    且其命令/判定不含随现场变化的未参数化字面量，且采集能力存在于能力目录。

判据与关键信号轨**同源**：requires 由 ``derive_signal_requires`` 计算，可用来源由
``collect_produced_vars``（diagnostic 阶段 produces 并集）∪ ``collect_declared_external``
（verification_contract.variables）聚合——与 ``signal_schema._collect_variable_sources``
完全一致的口径，杜绝「关键轨一套、根因轨另一套」的隐性漂移。

三态分级（每条 executable 根因必产出一态）：
    closed            requires 全覆盖 + 无写死实体 + 能力具备
    dangling_requires 有变量无来源且非白名单
    hardcoded_literals 命令/判定含随现场变化字面量
    capability_missing tool 不在能力目录

不可闭环**不自动降级 text**（评审决策②），而是交调用方判「拒绝候选」（对齐关键信号
``rejected_candidates``）：本纯函数只负责如实标 ``reject`` 与 ``reason_code``，落库/收口/
发布阻断由 §7 的三处门禁接入（抽取装配 / 人工编辑 / 发布）在后续 PR 完成。

HOST 不误伤（评审决策①）：qfk_* 的 ``host`` 为可选字段，**省略 host 时 args 里根本没有
{{HOST}} 占位符**，故 derive_signal_requires 不会把 HOST 列入 requires，本地操作类根因天然
``closed``（运行时走 env_context["node_ip"] 兜底）；只有**显式写了 {{HOST}}** 才要求闭环，
否则按悬空判拒绝候选。本模块无需为 HOST 特判——省略即无需求，写了即须有源，逻辑自洽。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from shared.schemas.signal_output import (
    collect_declared_external,
    collect_signal_produces,
    derive_signal_requires,
    is_diagnostic_phase,
)

# ─── 闭包状态与拒绝码 ──────────────────────────────────────────────────────────
CLOSURE_CLOSED = "closed"
CLOSURE_DANGLING = "dangling_requires"
CLOSURE_LITERAL = "hardcoded_literals"
CLOSURE_CAPABILITY = "capability_missing"
# text 形态无「可执行」语义，不设闭包义务
CLOSURE_NOT_APPLICABLE = "not_applicable"

REJECT_REASON_CODES = frozenset(
    {
        "ROOT_CLOSURE_DANGLING",
        "ROOT_CLOSURE_LITERAL_NOT_PARAMETERIZED",
        "ROOT_CLOSURE_CAPABILITY_MISSING",
    }
)

_STATUS_REASON_CODE: dict[str, str] = {
    CLOSURE_DANGLING: "ROOT_CLOSURE_DANGLING",
    CLOSURE_LITERAL: "ROOT_CLOSURE_LITERAL_NOT_PARAMETERIZED",
    CLOSURE_CAPABILITY: "ROOT_CLOSURE_CAPABILITY_MISSING",
}

# 多问题并存时的严重度优先级（越靠前越致命：能力缺失直接跑不动）
_STATUS_PRIORITY: tuple[str, ...] = (CLOSURE_CAPABILITY, CLOSURE_DANGLING, CLOSURE_LITERAL)

# ─── 随现场变化实体的紧口径黑名单（确定性正则，不依赖 LLM 自检）────────────────
# 先剥离 {{...}} 占位符，再在剩余字面量上匹配；命中即视为「该参数化而未参数化」。
# 紧口径 + 稳定值豁免（见各正则），防止把固定命令/固定目录/固定日志路径误判为写死实体。
_PLACEHOLDER_STRIP_RE = re.compile(r"\{\{[^}]*\}\}")

_ENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # 网络接口名：eth0/bond1/channel4/vlan100（现场可变），但排除 ethtool 等命令词（要求 eth 后紧跟数字且成词边界）
    ("interface", re.compile(r"\b(?:eth|bond|channel|vlan)\d+\b"), "INTERFACE"),
    # IPv4：四段点分十进制；每段 0-255。裸版本号（如 6.11.1，三段）不命中。
    (
        "ipv4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"),
        "IP",
    ),
    # 磁盘设备：/dev/sdX、/dev/nvmeXnY、/dev/vdX、/dev/hdX、/dev/xvdX（现场随盘变化）；
    # 刻意不含 /dev/null、/dev/zero 等稳定特殊设备，避免误判。
    ("device", re.compile(r"/dev/(?:x?vd|sd|nvme|hd|cciss)\w*"), "DEVICE"),
    # 进程号：--pid=1234 / -p 1234 语境过于宽泛，暂不纳入自动参数化（易误判普通数字），留待能力预检/人工。
)


def _iter_string_fields(node: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    """递归产出 (字段路径, 字符串值)，覆盖 dict/list 嵌套。"""
    if isinstance(node, str):
        yield prefix, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_string_fields(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _iter_string_fields(value, f"{prefix}[{idx}]")


def detect_hardcoded_literals(signal: dict[str, Any]) -> list[dict[str, str]]:
    """扫描 executable 根因的 ``acquire.args`` 与 ``match``，检出随现场变化的未参数化字面量。

    仅扫命令/判定实际内容（args + match），且**先剥离 {{VAR}} 占位符**——稳定命令（cat/ps/lsblk）、
    固定目录（/sf/cfg/if.d/）、固定日志路径不命中紧口径实体正则，故不误判。
    返回 [{field, value, entity, suggested_var}]，供调用方回喂「该变量化实体 + 建议标准变量名」。
    """
    findings: list[dict[str, str]] = []
    targets: list[tuple[str, Any]] = [
        ("acquire.args", (signal.get("acquire") or {}).get("args")),
        ("match", signal.get("match")),
    ]
    for base, node in targets:
        for field_path, raw in _iter_string_fields(node, base):
            stripped = _PLACEHOLDER_STRIP_RE.sub(" ", raw)
            for entity, pattern, suggested in _ENTITY_PATTERNS:
                for hit in pattern.findall(stripped):
                    findings.append(
                        {
                            "field": field_path,
                            "value": hit if isinstance(hit, str) else str(hit),
                            "entity": entity,
                            "suggested_var": suggested,
                        }
                    )
    return findings


def _build_available_sources(
    key_signals: Any, verification_contract: Any, context_whitelist: Iterable[str]
) -> tuple[set[str], dict[str, tuple[str, str]]]:
    """构造闭包可用来源集合，并记录每个变量的来源类别，供 resolved_by 追溯。

    优先级：producer（具体信号）> external（verification_contract 声明）> context（运行上下文白名单）。
    返回 (available_set, origin_map)，origin_map[var] = ("producer"|"external"|"context", ref)。
    """
    origin: dict[str, tuple[str, str]] = {}
    # 逐诊断信号标注产出来源（保留具体 signal_id 作为 ref，便于可追溯闭环来源）
    for index, sig in enumerate(key_signals or [], start=1):
        if not isinstance(sig, dict) or not is_diagnostic_phase(sig):
            continue
        sig_id = str(sig.get("id") or f"signal_{index:03d}")
        for var in collect_signal_produces(sig):
            origin.setdefault(var, ("producer", sig_id))
    for var in collect_declared_external(verification_contract):
        origin.setdefault(var, ("external", "verification_contract"))
    for var in (str(name).strip().upper() for name in context_whitelist if str(name).strip()):
        origin.setdefault(var, ("context", "context_whitelist"))
    return set(origin), origin


def evaluate_root_closure(
    root_signals: Any,
    key_signals: Any = None,
    *,
    verification_contract: Any = None,
    context_whitelist: Iterable[str] = (),
    capability_catalog: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """对每条根因信号判定闭环状态（纯函数，无副作用、可离线单测）。

    Args:
        root_signals: 归一化后的根因信号数组（含 match_type/acquire/match）。
        key_signals: 该 KBD 的 signals_json 信号数组（提供 produces 池，可为空）。
        verification_contract: KBD 的验证契约（提供外部变量声明，可为 None）。
        context_whitelist: 运行上下文可兜底的变量名（默认空；须显式枚举并注释来源）。
        capability_catalog: 可执行能力目录（默认取 ACQUIRER_CATALOG 的 qfk_* 集合）。

    Returns:
        与 root_signals 等长的记录列表，每条：
        {index, match_type, closure_status, requires, resolved_by, unresolved,
         literals, reject, reason_code}
    """
    catalog = _resolve_capability_catalog(capability_catalog)
    available, origin = _build_available_sources(key_signals, verification_contract, context_whitelist)

    results: list[dict[str, Any]] = []
    for index, signal in enumerate(root_signals or [], start=1):
        if not isinstance(signal, dict):
            continue
        match_type = str(signal.get("match_type") or "")
        if match_type != "executable":
            # text/未知形态：无闭包义务，原样保留
            results.append(
                {
                    "index": index,
                    "match_type": match_type,
                    "closure_status": CLOSURE_NOT_APPLICABLE,
                    "requires": [],
                    "resolved_by": [],
                    "unresolved": [],
                    "literals": [],
                    "reject": False,
                    "reason_code": None,
                }
            )
            continue

        tool = str(((signal.get("acquire") or {}).get("tool")) or "")
        requires = sorted({str(v).strip().upper() for v in derive_signal_requires(signal) if str(v).strip()})
        unresolved = [var for var in requires if var not in available]
        literals = detect_hardcoded_literals(signal)
        capability_ok = tool in catalog

        resolved_by = [
            {"var": var, "by": origin[var][0], "ref": origin[var][1]} for var in requires if var in origin
        ]

        status = CLOSURE_CLOSED
        if not capability_ok:
            status = CLOSURE_CAPABILITY
        elif unresolved:
            status = CLOSURE_DANGLING
        elif literals:
            status = CLOSURE_LITERAL

        reject = status != CLOSURE_CLOSED
        results.append(
            {
                "index": index,
                "match_type": match_type,
                "tool": tool,
                "closure_status": status,
                "requires": requires,
                "resolved_by": resolved_by,
                "unresolved": unresolved,
                "literals": literals,
                "capability_ok": capability_ok,
                "reject": reject,
                "reason_code": _STATUS_REASON_CODE.get(status),
            }
        )
    return results


def _resolve_capability_catalog(capability_catalog: Iterable[str] | None) -> set[str]:
    """能力目录：显式传入优先；缺省取 ACQUIRER_CATALOG 内可执行 qfk_* 消费原语。"""
    if capability_catalog is not None:
        return {str(t).strip() for t in capability_catalog if str(t).strip()}
    # 延迟导入，避免纯函数在无 FastAPI 依赖环境下也不可加载（离线试点/单测）
    from app.routes.extract_signals import ACQUIRER_CATALOG

    return {name for name in ACQUIRER_CATALOG if name.startswith("qfk_") and name != "qfk_var"}


def _reason_text(record: dict[str, Any]) -> str:
    """为拒绝候选生成可读原因（供审核 UI 逐条呈现）。"""
    status = record.get("closure_status")
    if status == CLOSURE_CAPABILITY:
        return f"采集能力缺失：工具 {record.get('tool')!r} 不在能力目录内"
    if status == CLOSURE_DANGLING:
        return f"变量悬空无来源：{', '.join(record.get('unresolved') or [])}"
    if status == CLOSURE_LITERAL:
        fields = sorted({f["field"] for f in record.get("literals") or []})
        return f"含随现场变化字面量，需参数化：{', '.join(fields)}"
    return "不可闭环"


def apply_closure_fields(
    root_signals: Any, results: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把闭包结果就地写回根因信号副本（§6 契约扩字段的产出原语）。

    仅为 executable 信号附 ``closure_status``/``requires``/``resolved_by``；text 不加（无闭包义务）。
    不改变传入对象（返回浅拷贝列表）；落库与三处门禁接入（§7）由后续 PR 调用本原语完成。
    """
    by_index = {r["index"]: r for r in results}
    out: list[dict[str, Any]] = []
    for index, signal in enumerate(root_signals or [], start=1):
        if not isinstance(signal, dict):
            continue
        record = by_index.get(index)
        annotated = dict(signal)
        if record is not None and record.get("match_type") == "executable":
            annotated["closure_status"] = record["closure_status"]
            annotated["requires"] = record["requires"]
            annotated["resolved_by"] = record["resolved_by"]
        out.append(annotated)
    return out


def split_accepted_and_rejected(
    root_signals: Any, results: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按闭包判定拆分 accepted / rejected（§5.4 拒绝候选，与 ``signals_json.rejected_candidates`` 同构）。

    不可闭环的 executable **不进 accepted、也不转 text**，而是连同 reason/reason_code 落入 rejected 审计快照。
    返回 (accepted_signals, rejected_candidates)。
    """
    by_index = {r["index"]: r for r in results}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, signal in enumerate(root_signals or [], start=1):
        if not isinstance(signal, dict):
            continue
        record = by_index.get(index)
        if record is not None and record.get("reject"):
            rejected.append(
                {
                    "candidate": signal,
                    "reason": _reason_text(record),
                    "reason_code": record["reason_code"],
                    "closure_status": record["closure_status"],
                    "unresolved": record["unresolved"],
                    "literals": record["literals"],
                }
            )
        else:
            accepted.append(signal)
    return accepted, rejected


def summarize_rejections(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """把评估结果汇总为拒绝候选审计信息（供 §7 三处门禁落 root_signal_rejected_json）。

    返回 {total, closed, rejected:[{index, reason_code, unresolved, literals, tool}], all_rejected}。
    executable 与 text 均计入 total；all_rejected 仅当存在 executable 候选且无一 closed 时为 True。
    """
    results = list(results)
    executable = [r for r in results if r.get("match_type") == "executable"]
    closed = sum(1 for r in executable if r.get("closure_status") == CLOSURE_CLOSED)
    rejected = [
        {
            "index": r["index"],
            "reason_code": r.get("reason_code"),
            "closure_status": r.get("closure_status"),
            "unresolved": r.get("unresolved") or [],
            "literals": r.get("literals") or [],
            "tool": r.get("tool"),
        }
        for r in results
        if r.get("reject")
    ]
    return {
        "total": len(results),
        "executable_total": len(executable),
        "closed": closed,
        "rejected": rejected,
        # 存在 executable 候选但无一闭环 → 全拒（调用方据此在收口处显式抛 ROOT_SIGNAL_ALL_REJECTED）
        "all_rejected": bool(executable) and closed == 0,
    }
