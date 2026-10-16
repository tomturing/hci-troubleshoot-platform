"""硬盘坏道 Live E2E Smoke —— 纯逻辑断言层。

本模块只含无副作用、可脱离 docker/LLM 单测的纯函数：SSE 解析、交互式请求识别、
跨层不变量断言（每条对应一个历史挤牙膏 bug）。真实网络编排见 driver.py / smoke.py。

设计立场（第一性原理）：
- live 端到端要「可复现地防回潮」，断言只打在结构不变量 + 守卫事件上，绝不断言逐字文本。
- 每个历史 bug 在此都有一条独立判据，缺一条就是又一轮挤牙膏。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 变量样 token：小写字母开头、含至少一个下划线分段（与 react_engine._VAR_TOKEN_RE 对齐）。
_VAR_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
# 结论中的定量数值（含小数/整数），用于「无溯源数值」反幻觉判据。
_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:%|ms|秒|分钟|TB|GB|字节|℃|°C)?")


def iter_sse_events(raw_lines: list[str]) -> list[dict[str, Any]]:
    """把 SSE 原始行解析成事件字典列表。

    兼容 `data: {...}` / `data: [DONE]` / 纯文本 `event:` 行；无法解析的 data 行忽略。
    返回的每个元素为一条 JSON 事件（[DONE] 与非法 JSON 不产出）。
    """
    events: list[dict[str, Any]] = []
    for line in raw_lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _nested_get(event: dict[str, Any], *keys: str) -> Any:
    cur: Any = event
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def find_interactive_request(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从事件流中定位首个「交互式变量请求」卡片。

    对齐 conversation-service：metadata.kind == 'interactive_request'，事件载荷含
    request_id / acp_session_id / 变量名。返回归一化后的请求描述，供 driver 提交应答。
    """
    for event in events:
        kind = _nested_get(event, "kind") or _nested_get(event, "metadata", "kind")
        if kind != "interactive_request":
            continue
        event_body = event.get("event") if isinstance(event.get("event"), dict) else event
        return {
            "kind": kind,
            "request_id": event_body.get("request_id") or event_body.get("requestId"),
            "acp_session_id": event_body.get("acp_session_id") or event_body.get("acpSessionId"),
            "variable": event_body.get("variable_name") or event_body.get("variable") or event_body.get("name"),
            "raw": event_body,
        }
    return None


_VARIABLE_REQUEST_KINDS = ("variable_input", "variable_confirm")


def normalize_interactive_request(data: dict[str, Any]) -> dict[str, Any] | None:
    """把 SSE 线上 `event: interactive_request` 的 data（camelCase 字段）归一化为应答所需描述。

    线上字段（见 conversation_service.py L919）：requestId / acpSessionId / kind / metadata，
    变量名位于 `metadata.variable_name`（react_engine.py L1941）。纯函数，便于脱网单测。
    """
    if not isinstance(data, dict):
        return None
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    variable = (
        meta.get("variable_name")
        or data.get("variable_name")
        or data.get("variable")
        or data.get("name")
    )
    return {
        "kind": data.get("kind"),
        "request_id": data.get("requestId") or data.get("request_id"),
        "acp_session_id": data.get("acpSessionId") or data.get("acp_session_id"),
        "variable": variable,
        "metadata": meta or None,
        "raw": data,
    }


def is_variable_request(req: dict[str, Any] | None) -> bool:
    """判断归一化后的交互请求是否为 SOP 变量输入/确认卡（需 driver 用应答脚本落值）。"""
    return bool(req) and req.get("kind") in _VARIABLE_REQUEST_KINDS


def guard_metric_fired(before: dict[str, float], after: dict[str, float], key: str) -> bool:
    """比较 agent-service /metrics 前后计数，判断某守卫是否「真的触发过」。

    `key` 为 Prometheus 采样键（如 'agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}'）。
    只有 after-before 增量为正才算触发（断言守卫本身，而非只看输出像不像）。
    """
    return (after.get(key, 0.0) - before.get(key, 0.0)) > 0


def parse_prometheus_counters(text: str) -> dict[str, float]:
    """把 Prometheus 文本解析为 {完整样本行键: 数值}，仅取 counter/gauge 采样行。

    保留 label，键形如 `name{label="v"}`，便于 guard_metric_fired 精确比对。
    """
    series: dict[str, float] = {}
    for line in (text or "").splitlines():
        row = line.strip()
        if not row or row.startswith("#"):
            continue
        head, _, value = row.rpartition(" ")
        if not head:
            continue
        try:
            series[head] = float(value)
        except ValueError:
            continue
    return series


def variable_outcome(value: str) -> dict[str, Any]:
    """构造变量输入的 free_text 应答 outcome（对齐 InteractiveResponseBody.outcome）。"""
    return {"outcome": "free_text", "text": value}


def user_questions_grounded(
    questions: list[str],
    resolved_variables: set[str],
    declared_variables: set[str],
) -> tuple[list[str], list[str]]:
    """独立复算「提问落地性」判据，用于在 E2E 侧断言引擎过滤确实生效。

    与 react_engine.filter_ungrounded_user_questions 同规则：
    - 命中已就绪变量名 → 视为重复索取（应被丢弃）；
    - 引用任何不在（已就绪 ∪ 已声明）内的变量样 token → 凭空索要（应被丢弃）。
    无声明集时不作判据（护栏），返回 (全部保留, [])。
    """
    if not declared_variables:
        return list(questions or []), []
    known = resolved_variables | declared_variables
    kept: list[str] = []
    dropped: list[str] = []
    for question in questions or []:
        tokens = set(_VAR_TOKEN_RE.findall(question or ""))
        if tokens & resolved_variables or any(tok not in known for tok in tokens):
            dropped.append(question)
        else:
            kept.append(question)
    return kept, dropped


def extract_user_questions(text: str) -> list[str]:
    """从助手回复文本中尽力抽取 user_questions 列表。

    优先按 ReasoningOutput JSON 解析；解析失败则回退为空（driver 会用守卫事件兜底判定）。
    """
    # 直接是 JSON
    candidates = [text]
    # 被 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidates.insert(0, fence.group(1))
    # 首个大括号片段
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for blob in candidates:
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("user_questions"), list):
            return [str(q) for q in obj["user_questions"]]
    return []


def conclusion_ungrounded_numbers(conclusion: str, evidence_text: str) -> list[str]:
    """结论里出现在、但在证据文本中找不到来源的数值（反幻觉判据）。"""
    evidence = evidence_text or ""
    ungrounded: list[str] = []
    for match in _NUMBER_RE.finditer(conclusion or ""):
        number = match.group(1)
        if number not in evidence:
            ungrounded.append(number)
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for number in ungrounded:
        if number not in seen:
            seen.add(number)
            unique.append(number)
    return unique


def evaluate_invariants(
    *,
    observed_category_code: str | None,
    expected_category_code: str | None,
    sop_document_hit_id: int | None,
    expected_sop_id: int | None,
    submitted_variables: dict[str, str],
    persisted_variables: dict[str, Any],
    final_user_questions: list[str],
    resolved_variables: set[str],
    declared_variables: set[str],
    guard_events: set[str],
    expect_grounding_filter: bool,
) -> dict[str, list[str]]:
    """汇总跨层不变量断言，返回 {passed:[...], failed[...]}。每条对应一个历史 bug 缝隙。"""
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        (passed if ok else failed).append(f"{name}{(' — ' + detail) if detail and not ok else ''}")

    # S0：确认分类命中期望叶子
    check("S0_category_confirmed", bool(expected_category_code) and observed_category_code == expected_category_code,
          f"observed={observed_category_code} expected={expected_category_code}")
    # S1：命中期望 SOP 根
    check("S1_sop_hit", expected_sop_id is not None and sop_document_hit_id == expected_sop_id,
          f"hit={sop_document_hit_id} expected={expected_sop_id}")
    # #1149 后端真值：提交的变量必须已落库
    missing_persist = [k for k in submitted_variables if k not in persisted_variables]
    check("VarSubmit_persisted", not missing_persist, f"未落库={missing_persist}")
    # #1151：最终 user_questions 不含对已就绪变量的重复索取、不含未声明 token
    _kept, dropped = user_questions_grounded(final_user_questions, resolved_variables, declared_variables)
    check("UserQuestions_grounded", not dropped, f"仍有未 grounded 提问={dropped}")
    # #1151 守卫确曾触发（stub 注入坏输出时，引擎侧过滤事件必须出现）
    if expect_grounding_filter:
        check("Guard_user_questions_filter_fired", "sop_user_questions_grounded_filter" in guard_events,
              f"guard_events={sorted(guard_events)}")
    return {"passed": passed, "failed": failed}
