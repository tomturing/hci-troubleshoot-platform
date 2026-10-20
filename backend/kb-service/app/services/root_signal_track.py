"""根因信号链路的服务端装配（与关键信号链路完全解耦）。

第一性原理：两条候选策略（关键信号 / 根因信号）要能「分别单独使用 + 并存 + 事后
淘汰一条」，装配层就必须满足两条硬约束：

1. **只做纯新增**：本模块只产出 ``root_*`` 前缀的新字段，绝不读写 ``signals`` /
   ``execution_issues`` / ``diagnosis_capability`` / ``recommendable`` / ``executable``
   这些关键信号链路字段。删除本模块只会让 ``root_*`` 字段消失，关键链路输出字节不变。
2. **自己的准入规则**：根因信号是否可执行，由本模块按根因信号自身的门禁版本与
   校验状态判定，不复用关键信号的 ``_execution_issues``（避免继承关键信号的
   语义画像 / 发布章 / 工具契约版本等只属于关键信号轨的规则）。

调用点在 ``playbooks.py``，仅一行 ``**build_root_track_payload(kbd)`` 展开。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from shared.schemas.signal_schema import validate_publishable_signals_json

from app.models.kbd_entry import KbdEntry

# 根因信号参与运行时判定所需的最低门禁版本。抽取侧写入 provenance.gate_version；
# 缺失或低于该值视为 legacy（未经「最少充分集」校验，可能含恒真判据），
# 只留库供审核，不参与根因链路的采集与判定。
ROOT_SIGNAL_RUNTIME_GATE_VERSION = 2

# 根因 executable 信号沿用关键信号 keyword/regex 的取数契约：match.extract 指向
# stdout 全文（rows=all·cardinality=all），从而复用运行时 evaluate_matcher 的确定性求值。
# 形态必须与关键信号完全一致（`rows:{mode}` + `value_mode`），否则
# validate_publishable_signals_json 会驳回，令全部根因 KBD 退化为不可执行。
_ROOT_WHOLE_STDOUT_EXTRACT: dict[str, Any] = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}

# 根因简单 match 的 mode 词汇表 → 运行时 CDD Matcher 的 mode 词汇表。
# 必须显式翻译：运行时的 else 分支会落 "or"，把 "all" 原样透传会静默变成 OR 误判。
_ROOT_MODE_MAP = {"all": "and", "and": "and", "any": "or", "or": "or", "not": "not"}


def root_signals_of(entry: KbdEntry) -> list[dict[str, Any]]:
    """读取 root_signal_json 的 v2 顶层数组（兼容 v1 单对象与非法形态）。"""
    raw = getattr(entry, "root_signal_json", None) or []
    if isinstance(raw, dict):
        raw = [raw]
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def root_gate_version(root: dict[str, Any]) -> int:
    """根因信号的门禁版本；缺失/非法视为 0（legacy，不参与运行时）."""
    provenance = root.get("provenance") or {}
    try:
        return int(provenance.get("gate_version") or 0)
    except (TypeError, ValueError):
        return 0


def is_runtime_eligible(root: dict[str, Any]) -> bool:
    """根因信号是否具备参与运行时采集/判定的资格。

    两道闸门缺一不可：
    - ``gate_version >= 2``：必须经「最少充分集」门禁（否则存量信号可能含恒真判据，
      在融合语义下会退化为万能匹配）；
    - ``provenance.gate_status == "passed"``：逐条门禁必须通过（warning 只留库供审核）。
    """
    if str(root.get("match_type") or "") != "executable":
        return False
    if root_gate_version(root) < ROOT_SIGNAL_RUNTIME_GATE_VERSION:
        return False
    return str((root.get("provenance") or {}).get("gate_status") or "") == "passed"


def _root_signal_runtime_id(root: dict[str, Any]) -> str:
    """为根因信号派生稳定运行时 id：优先自带 id，否则以 signal_text 指纹派生（幂等、跨请求稳定）。"""
    base = str(root.get("id") or root.get("signal_id") or "").strip()
    if base:
        return base
    digest = hashlib.sha256(str(root.get("signal_text") or "").encode("utf-8")).hexdigest()[:12]
    return f"root_{digest}"


def _match_to_cdd(match: dict[str, Any]) -> dict[str, Any] | None:
    """把根因简单 match（{type, mode, expected:[关键词]}）转成运行时 CDD Matcher 契约。

    根因抽取轨的 match 只存关键词列表，而运行时 evaluate_matcher 需要 pattern /
    expected 布尔 / extract 的完整契约，二者不可直接透传。仅覆盖可无损还原的
    keyword / regex：

    - keyword 单词或 any 语义：keyword matcher（逐行任一/同现即可表达）；
    - keyword 多词 all（要求全部关键词出现且允许跨行）：逐行 records 的 and 只认
      同行同现，无法表达跨行，故改用 lookahead 正则跑整段输出（regex 分支以
      predicate_text 全文 + re.DOTALL 求值，天然跨行）；
    - state/boolean/threshold/exists：根因简单形不携带 value/operator 等数值契约，
      无法无损还原，保守返回 None 丢弃（试点实测 executable 根因 100% 为 keyword）。
    """
    mtype = str(match.get("type") or "")
    raw_expected = match.get("expected")
    if isinstance(raw_expected, list):
        tokens = [str(item).strip() for item in raw_expected if str(item).strip()]
    elif raw_expected in (None, ""):
        tokens = []
    else:
        tokens = [str(raw_expected).strip()]
    if not tokens:
        return None
    mode = str(match.get("mode") or "all").lower()
    if mtype == "keyword":
        if len(tokens) >= 2 and mode in {"all", "and"}:
            pattern = "".join(f"(?=.*{re.escape(token)})" for token in tokens)
            return {
                "type": "regex",
                "pattern": pattern,
                "expected": True,
                "extract": dict(_ROOT_WHOLE_STDOUT_EXTRACT),
            }
        return {
            "type": "keyword",
            "pattern": tokens,
            "mode": _ROOT_MODE_MAP.get(mode, "or"),
            "expected": True,
            "extract": dict(_ROOT_WHOLE_STDOUT_EXTRACT),
        }
    if mtype == "regex":
        return {
            "type": "regex",
            "pattern": tokens[0],
            "expected": True,
            "extract": dict(_ROOT_WHOLE_STDOUT_EXTRACT),
        }
    return None


def normalize_root_for_runtime(root: dict[str, Any]) -> dict[str, Any] | None:
    """把单条 executable 根因信号归一为可编译进 CDD 计划的运行时信号 dict。

    与关键信号链路的 ``normalize_root_for_runtime`` 语义一致，但**不带任何轨道路由
    标记**：运行时计划由根因链路独立编译，不需要 ``track`` 字段（避免在共享内核里
    引入轨道概念）。多余键（role / provenance 细节 / distinctiveness /
    prompt_revision）由 KBDStep(extra=ignore) 与 compile_signal_plan 忽略。
    """
    acquire = root.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    if not tool:
        return None
    cdd_match = _match_to_cdd(root.get("match") or {})
    if cdd_match is None:
        return None
    return {
        "id": _root_signal_runtime_id(root),
        "acquire": {"tool": tool, "args": acquire.get("args") or {}},
        "match": cdd_match,
        "provenance": {"category": "backend"},
    }


def display_anchors_of(entry: KbdEntry) -> list[dict[str, Any]]:
    """text 根因信号作为诊断报告「人工确认锚点」的展示载荷。

    绝不进 root_signals / plan / 门禁：无 acquire、无现场取证，只把结论侧关键词
    交给运维人工核对。
    """
    anchors: list[dict[str, Any]] = []
    for root in root_signals_of(entry):
        if str(root.get("match_type") or "") != "text":
            continue
        match = root.get("match") or {}
        expected = match.get("expected")
        if isinstance(expected, list):
            keywords = [str(item) for item in expected if str(item).strip()]
        elif expected in (None, ""):
            keywords = []
        else:
            keywords = [str(expected)]
        anchors.append(
            {
                "signal_text": str(root.get("signal_text") or ""),
                "keywords": keywords,
                "match_type": str(match.get("type") or ""),
            }
        )
    return anchors


def _execution_issues(normalized: list[dict[str, Any]]) -> list[str]:
    """根因链路自己的执行门禁。

    只回答一个问题：这些根因信号能否被编译成确定性的采集+判定计划。
    关键信号的语义画像、专家发布章、工具契约版本等规则属于关键信号轨，
    此处一律不继承。
    """
    if not normalized:
        return ["未配置可执行根因信号"]
    issues: list[str] = []
    document = {"schema_version": 2, "signals": normalized}
    try:
        validate_publishable_signals_json(document)
    except Exception as exc:  # jsonschema 的 str() 会带整段 Schema，message 才可操作
        issues.append(f"root_signal 契约校验失败: {str(getattr(exc, 'message', exc))}")
    seen_ids: set[str] = set()
    for index, signal in enumerate(normalized, start=1):
        signal_id = str(signal.get("id") or "").strip()
        if not signal_id:
            issues.append(f"root_signal[{index}] 缺少 signal_id")
        elif signal_id in seen_ids:
            issues.append(f"root_signal_id 重复: {signal_id}")
        else:
            seen_ids.add(signal_id)
        if not (signal.get("acquire") or {}).get("tool"):
            issues.append(f"{signal_id or f'root_signal[{index}]'} 缺少 acquire.tool")
        if not isinstance(signal.get("match"), dict):
            issues.append(f"{signal_id or f'root_signal[{index}]'} 缺少确定性 matcher")
    return issues


def build_root_track_payload(entry: KbdEntry) -> dict[str, Any]:
    """产出根因链路的全部 ``root_*`` 字段（纯新增，不触碰关键信号字段）。

    返回字段：
    - ``root_signals``：运行时形态（executable + 门禁通过），供根因链路独立编译计划；
    - ``root_display_anchors``：text 轨人工核对锚点；
    - ``root_execution_issues`` / ``root_executable`` / ``root_recommendable`` /
      ``root_diagnosis_capability``：根因链路自己的准入与能力判定；
    - ``root_snapshot_id``：根因信号集合的稳定指纹，使根因链路的计划快照可复现；
    - ``root_signal_stats``：存量分布（总数/可执行/text/被门禁拦下），用于审核与赛马观测。

    设计取舍：``root_signals`` 为空时仍返回完整字段（``root_executable=False``），
    让消费方无需区分「字段缺失」与「无根因信号」。
    """
    all_roots = root_signals_of(entry)
    normalized: list[dict[str, Any]] = []
    gated_out = 0
    for root in all_roots:
        if not is_runtime_eligible(root):
            if str(root.get("match_type") or "") == "executable":
                gated_out += 1
            continue
        signal = normalize_root_for_runtime(root)
        if signal is None:
            gated_out += 1
            continue
        normalized.append(signal)

    anchors = display_anchors_of(entry)
    issues = _execution_issues(normalized)
    executable = not issues
    # 快照指纹必须覆盖归一后的**完整内容**（id + acquire + match）：只取 id 时，
    # 关键词改了但 signal_text 未变会得到同一个快照 id，让「计划快照」失真。
    material = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_id = "rootsig-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16] if normalized else "rootsig-none"

    if executable:
        capability = "executable"
    elif anchors:
        capability = "reference_only"
    else:
        capability = "none"

    return {
        "root_signals": normalized,
        "root_display_anchors": anchors,
        "root_execution_issues": issues,
        "root_executable": executable,
        "root_recommendable": executable or bool(anchors),
        "root_diagnosis_capability": capability,
        "root_snapshot_id": snapshot_id,
        "root_signal_stats": {
            "total": len(all_roots),
            "executable": len(normalized),
            "text": len(anchors),
            "gated_out": gated_out,
        },
    }
