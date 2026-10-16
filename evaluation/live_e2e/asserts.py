"""硬盘坏道 Live E2E Smoke —— 纯逻辑断言层。

本模块只含无副作用、可脱离 docker/LLM 单测的纯函数：SSE 解析、交互式请求识别、
跨层不变量断言（每条对应一个历史挤牙膏 bug）。真实网络编排见 driver.py / smoke.py。

设计立场（第一性原理）：
- live 端到端要「可复现地防回潮」，断言只打在结构不变量 + 守卫事件上，绝不断言逐字文本。
- 每个历史 bug 在此都有一条独立判据，缺一条就是又一轮挤牙膏。
"""

from __future__ import annotations

import hashlib
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


# ─────────────────────────────────────────────────────────────────────────────
# S0 候选澄清（choice_options）纯逻辑——对应真实用户在候选清单里的点选动作。
#
# conversation_service 在 S0 阶段若模型给出的是「故障分类候选」而非直接确认，会下发
# `event: metadata` 且 `data.kind == 'choice_options'`（schemaVersion=2，options[].code）。
# 前端/用户须回传 `metadata.selectedCategoryCode`（v2 优先稳定码）方能推进 S0→S1。
# driver 扮演前端，故必须复刻这一选择动作，否则会把「等待点选」误判为终态。
# ─────────────────────────────────────────────────────────────────────────────


def extract_choice_options(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 typed 事件流中提取首个 choice_options 候选清单（无则返回空列表）。

    兼容事件被包成 `{"event": "metadata", "data": {...}}` 或直接就是候选对象两种形态。
    """
    for ev in events or []:
        data = ev.get("data") if isinstance(ev, dict) else None
        if not isinstance(data, dict):
            data = ev if isinstance(ev, dict) else None
        if not isinstance(data, dict):
            continue
        kind = data.get("kind") or (data.get("metadata") or {}).get("kind")
        if kind != "choice_options":
            continue
        options = data.get("options")
        if isinstance(options, list):
            return [o for o in options if isinstance(o, dict)]
    return []


def match_candidate_option(
    options: list[dict[str, Any]], expected_code: str | None
) -> dict[str, Any] | None:
    """在候选清单中按期望稳定码匹配目标项（无匹配返回 None，绝不臆造）。"""
    if not expected_code:
        return None
    for opt in options or []:
        code = opt.get("code") or opt.get("optionId")
        if isinstance(code, str) and code == expected_code:
            return opt
    return None


def build_category_selection(option: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """由选中的候选项构造推进 S0→S1 的下一轮消息：返回 (content, metadata)。

    content 取候选展示名（贴近真实用户回话），metadata 用 v2 稳定契约 `selectedCategoryCode`。
    """
    code = option.get("code") or option.get("optionId") or ""
    label = option.get("name") or option.get("categoryName") or str(code)
    return label, {"selectedCategoryCode": code}


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
    collected_exec_ids: set[str] | None = None,
    expect_real_collection: bool = False,
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
    # V-017 真采集：真实仿真模式下必须确有命令经 bridge 下发并采集回来（而非静态 answers 蒙混）
    if expect_real_collection:
        check("V017_tool_call_collected", collected_exec_ids is not None and len(collected_exec_ids) >= 1,
              f"collected_exec_ids={sorted(collected_exec_ids or [])}")
    return {"passed": passed, "failed": failed}


# ─────────────────────────────────────────────────────────────────────────────
# 真实采集链路（headless bridge relay）纯逻辑——无网络，可脱 docker 单测。
#
# 产品里 acli/bash 命令的唯一真实路径：agent-service → Redis → SSE `agent_exec_command`
# → [前端/浏览器 → terminal_bridge → SSH → hci-sim 节点] → POST /exec-result → agent blpop。
# 本 driver 用 websockets 复刻「前端 + bridge」那一端，命令真的发到 hci-sim 节点采集，
# 从而覆盖此前静态 variables.answers 从未触碰的取值链缝隙（V-017）。以下函数负责报文构造。
# ─────────────────────────────────────────────────────────────────────────────


def normalize_agent_exec_command(data: dict[str, Any]) -> dict[str, Any]:
    """把 SSE `agent_exec_command` 事件（camelCase 线上字段）归一为 snake_case 采集请求。

    对齐 conversation-service push_agent_exec_command 的 event_data 形状：
    execId/toolName/command/builtCommand/container/riskLevel/nodeIp/timeout/traceId。
    """
    return {
        "exec_id": data.get("execId") or data.get("exec_id") or "",
        "tool_name": data.get("toolName") or data.get("tool_name") or "",
        "command": data.get("builtCommand") or data.get("command") or "",
        "original_command": data.get("originalCommand") or data.get("command") or "",
        "container": data.get("container") or "host",
        "risk_level": data.get("riskLevel") if data.get("riskLevel") is not None else data.get("risk_level"),
        "node_ip": data.get("nodeIp") or data.get("node_ip"),
        "timeout": data.get("timeout") or 15,
        "trace_id": data.get("traceId") or data.get("trace_id"),
        "raw": data,
    }


def bridge_ssh_connect_msg(connection: dict[str, Any]) -> dict[str, Any]:
    """由 diagnosis-lab connection.json 构造 terminal_bridge 的 `ssh_connect` 报文。

    对齐 hci-sim-smoke 协议：host/port/username/auth_type/password/execution_mode/test_run_id。
    """
    conn = connection.get("connection") or {}
    case_id = f"SIM-KBD-{connection.get('support_id', 'live-e2e')}"
    return {
        "type": "ssh_connect",
        "case_id": case_id,
        "host": conn.get("host"),
        "port": conn.get("port"),
        "username": conn.get("username"),
        "auth_type": conn.get("auth_type"),
        "password": conn.get("password"),
        "execution_mode": conn.get("execution_mode"),
        "test_run_id": conn.get("test_run_id"),
    }


def bridge_exec_msg(command: str, exec_id: str, container: str = "host", timeout: int = 15) -> dict[str, Any]:
    """构造 terminal_bridge 的 `ssh_exec_process` 下发报文（在已 ssh_connected 的会话上执行命令）。"""
    return {
        "type": "ssh_exec_process",
        "exec_id": exec_id,
        "command": command,
        "container": container or "host",
        "timeout": timeout,
    }


def build_exec_result_payload(
    exec_cmd: dict[str, Any],
    relay_result: dict[str, Any],
) -> dict[str, Any]:
    """把 bridge 回传的 exec_result 组装成 POST /exec-result 的 ExecResultRequest 报文。

    relay_result 取自 terminal_bridge 的 exec_result 消息：stdout/stderr/exit_code/error_type。
    严格按后端 ExecResultRequest 契约填字段：traceparent 只有符合 W3C 格式才带上，否则省略避免 422。
    """
    stdout = relay_result.get("stdout") or ""
    stderr = relay_result.get("stderr") or ""
    exit_code = int(relay_result.get("exit_code", relay_result.get("exitCode", 0)) or 0)
    merged_output = relay_result.get("output")
    if merged_output is None:
        merged_output = stdout if exit_code == 0 and not stderr else (f"{stdout}{stderr}" if stderr else stdout)

    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    payload: dict[str, Any] = {
        "exec_id": exec_cmd["exec_id"],
        "output": merged_output,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_bytes": len(stdout.encode("utf-8")),
        "stderr_bytes": len(stderr.encode("utf-8")),
        "stdout_sha256": _sha(stdout),
        "stderr_sha256": _sha(stderr),
    }
    if exec_cmd.get("trace_id"):
        payload["trace_id"] = exec_cmd["trace_id"]
    if relay_result.get("error_type"):
        payload["error_type"] = relay_result["error_type"]
    for key in ("duration_ms", "timed_out", "cancelled"):
        if key in relay_result:
            payload[key] = relay_result[key]
    return payload
