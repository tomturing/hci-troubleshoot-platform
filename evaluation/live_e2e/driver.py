"""硬盘坏道 Live E2E Smoke —— 真实会话链路驱动（httpx 异步）。

职责（第一性原理）：把「贯穿真实代码路径的会话控制链路」跑通——
case-service 建案 → conversation-service 建会话 → POST /message 流式解析 SSE →
遇 SOP 变量交互卡按应答脚本提交 `/interactive-response` →（复刻 #1149 修复时序：
**先确认后端变量池已落库、pending 清空，再触发续跑消息**）→ 循环至终态。

诚实边界：
- 本驱动只做「结构不变量 + 守卫事件」观测，绝不比较逐字文本；真实 LLM 非确定性由
  smoke.py 的 attempts 多数表决与 stub 确定性注入共同治理。
- 直连 case/conversation-service，用 HMAC 身份签名头（sign_client_identity）避开网关
  用户登录；查询 SOP 落库态与 agent /metrics 用 Bearer INTERNAL_API_TOKEN。

纯逻辑（解析/判定/断言）全在 asserts.py，本模块只负责网络编排，故脱网 docker 环境
仅能静态校验，端到端实跑须在具备全栈的宿主机执行。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from shared.security.signature import sign_client_identity

from evaluation.live_e2e import asserts

# agent-service 反幻觉守卫的 Prometheus 样本键（react_engine.py L781 触发 .inc()）。
_UNGROUNDED_QUESTION_METRIC = (
    'agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}'
)
_HARDBLOCK_NUMBERS_METRIC = (
    'agent_hallucination_detected_total{hallucination_type="ungrounded_numbers"}'
)


@dataclass
class DriverConfig:
    """驱动运行期配置（全部可由 smoke.py CLI / 环境变量注入）。"""

    case_base: str
    conv_base: str
    agent_base: str
    client_id: str
    internal_token: str
    assistant_type: str = "htp-agent"
    request_timeout: float = 180.0
    max_turns: int = 8


@dataclass
class RunObservation:
    """一次端到端驱动的结构化观测，交给 smoke.py 做不变量断言。"""

    case_id: str | None = None
    conversation_id: str | None = None
    terminal: bool = False
    observed_category_code: str | None = None
    sop_document_hit_id: int | None = None
    submitted_variables: dict[str, str] = field(default_factory=dict)
    persisted_variables: dict[str, Any] = field(default_factory=dict)
    pending_variable_name: Any = None
    final_user_questions: list[str] = field(default_factory=list)
    final_text: str = ""
    guard_events: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _identity_headers(config: DriverConfig) -> dict[str, str]:
    return sign_client_identity(config.client_id, config.internal_token)


def _admin_headers(config: DriverConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.internal_token}"}


async def _stream_message(
    client: httpx.AsyncClient,
    config: DriverConfig,
    conversation_id: str,
    case_id: str,
    content: str,
    metadata: dict[str, Any] | None,
    events_sink: list[dict[str, Any]],
) -> str:
    """POST /message 并把 SSE 解析成 typed 事件（含 text/interactive_request/metadata/tool_call）。

    返回本轮拼接出的助手文本。typed 事件形如 {"event": <类型|'message'>, "data": <obj>}。
    """
    body: dict[str, Any] = {"case_id": case_id, "role": "user", "content": content}
    if metadata is not None:
        body["metadata"] = metadata
    if config.assistant_type:
        body["assistant_type"] = config.assistant_type

    text_parts: list[str] = []
    cur_event: str | None = None
    data_lines: list[str] = []
    url = f"{config.conv_base}/api/conversations/{conversation_id}/message"

    async with client.stream(
        "POST", url, json=body, headers=_identity_headers(config), timeout=config.request_timeout
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if line == "":
                # 一个 SSE 消息块结束：分发
                if data_lines:
                    payload = "\n".join(data_lines)
                    handled, is_done, chunk = _dispatch_sse(cur_event, payload, text_parts)
                    events_sink.append({"event": cur_event or "message", "data": handled})
                    if is_done:
                        return "".join(text_parts)
                cur_event, data_lines = None, []
                continue
            if line.startswith(":"):  # 心跳/注释
                continue
            if line.startswith("event:"):
                cur_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
    return "".join(text_parts)


def _dispatch_sse(
    event_type: str | None,
    payload: str,
    text_parts: list[str],
) -> tuple[dict[str, Any], bool, str]:
    """把单条 SSE data 解析为 typed 事件字典。返回 (event_obj, is_done, text_chunk)。"""
    if payload == "[DONE]":
        return {"kind": "done"}, True, ""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return {"kind": "raw", "text": payload}, False, payload
    if event_type in (
        "interactive_request",
        "metadata",
        "tool_call",
        "tool_result",
        "diagnostic_outcome",
        "error",
    ):
        return obj, False, ""
    content = obj.get("content") if isinstance(obj, dict) else None
    if isinstance(content, str):
        text_parts.append(content)
        return obj, False, content
    return obj, False, ""


async def _submit_variable(
    client: httpx.AsyncClient,
    config: DriverConfig,
    conversation_id: str,
    req: dict[str, Any],
    value: str,
) -> bool:
    """POST /interactive-response 落变量（outcome=free_text + 原 metadata.variable_name）。"""
    body = {
        "kind": req["kind"],
        "request_id": req["request_id"],
        "acp_session_id": req["acp_session_id"],
        "outcome": {"outcome": "free_text", "text": value},
        "metadata": req.get("metadata") or {"variable_name": req.get("variable")},
    }
    url = f"{config.conv_base}/api/conversations/{conversation_id}/interactive-response"
    resp = await client.post(url, json=body, headers=_identity_headers(config), timeout=config.request_timeout)
    return resp.status_code == 200


async def _get_sop_execution(
    client: httpx.AsyncClient, config: DriverConfig, conversation_id: str
) -> dict[str, Any] | None:
    """GET /sop/execution（Bearer）读取后端变量池真值：context_variables / pending / status。"""
    url = f"{config.conv_base}/api/conversations/{conversation_id}/sop/execution"
    resp = await client.get(url, headers=_admin_headers(config), timeout=config.request_timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _snapshot_guard_metrics(client: httpx.AsyncClient, config: DriverConfig) -> dict[str, float]:
    """GET agent-service /metrics，解析反幻觉守卫计数为 {样本键: 数值}。"""
    url = f"{config.agent_base}/metrics"
    try:
        resp = await client.get(url, timeout=config.request_timeout)
        if resp.status_code != 200:
            return {}
    except httpx.HTTPError:
        return {}
    return asserts.parse_prometheus_counters(resp.text)


def _scan_terminal_signals(events: list[dict[str, Any]]) -> tuple[str | None, int | None]:
    """从全部 typed 事件里尽力提取「确认分类 code」与「命中 sop_document_id」。"""
    category: str | None = None
    sop_id: int | None = None
    for ev in events:
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        for bag in (data, data.get("metadata"), data.get("result")):
            if not isinstance(bag, dict):
                continue
            code = bag.get("category_code") or bag.get("categoryCode") or bag.get("category")
            if isinstance(code, str) and code:
                category = code
            doc = bag.get("sop_document_id")
            if isinstance(doc, int):
                sop_id = doc
    return category, sop_id


async def run_scenario(
    config: DriverConfig,
    spec: dict[str, Any],
    dump_dir: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> RunObservation:
    """驱动一次完整端到端会话，返回结构化观测（不含断言结论，断言在 smoke.py 汇总）。"""
    obs = RunObservation()
    answers: dict[str, str] = dict((spec.get("variables") or {}).get("answers") or {})
    first_message = (spec.get("ticket") or {}).get("user_first_message") or (spec.get("ticket") or {}).get("alert_text") or ""
    if not first_message:
        obs.error = "场景 spec 缺少 ticket.user_first_message/alert_text"
        return obs

    def note(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    async with httpx.AsyncClient() as client:
        # 1) 建案（client_id 由签名头裁定）
        case_payload = {
            "client_id": config.client_id,
            "title": spec.get("title") or "live-e2e disk badtrack",
            "description": first_message[:2000],
            "assistant_type": config.assistant_type,
        }
        resp = await client.post(
            f"{config.case_base}/api/cases/", json=case_payload, headers=_identity_headers(config),
            timeout=config.request_timeout,
        )
        if resp.status_code not in (200, 201):
            obs.error = f"建案失败 {resp.status_code}: {resp.text[:200]}"
            return obs
        obs.case_id = resp.json()["case_id"]
        note(f"case created: {obs.case_id}")

        # 2) 建会话
        resp = await client.post(
            f"{config.conv_base}/api/conversations/",
            params={"case_id": obs.case_id, "assistant_type": config.assistant_type},
            headers=_identity_headers(config),
            timeout=config.request_timeout,
        )
        if resp.status_code not in (200, 201):
            obs.error = f"建会话失败 {resp.status_code}: {resp.text[:200]}"
            return obs
        obs.conversation_id = resp.json()["conversation_id"]
        note(f"conversation created: {obs.conversation_id}")

        guard_before = await _snapshot_guard_metrics(client, config)

        # 3) 首条告警消息 → SSE
        content = first_message
        metadata: dict[str, Any] | None = None
        for turn in range(config.max_turns):
            events_this_turn: list[dict[str, Any]] = []
            text = await _stream_message(
                client, config, obs.conversation_id, obs.case_id, content, metadata, events_this_turn
            )
            obs.events.extend(events_this_turn)
            obs.final_text = text or obs.final_text

            var_reqs = [
                asserts.normalize_interactive_request(ev["data"])
                for ev in events_this_turn
                if ev.get("event") == "interactive_request"
            ]
            var_reqs = [r for r in var_reqs if asserts.is_variable_request(r)]

            if not var_reqs:
                obs.terminal = True
                note(f"turn {turn}: 无交互卡，判定终态")
                break

            # #1149 时序：先提交并确认落库，再触发续跑消息
            last_value = ""
            for req in var_reqs:
                value = answers.get(req["variable"])
                if value is None:
                    obs.error = f"应答脚本缺少变量 {req['variable']} 的值"
                    return obs
                ok = await _submit_variable(client, config, obs.conversation_id, req, value)
                if not ok:
                    obs.error = f"变量提交失败 {req['variable']}"
                    return obs
                obs.submitted_variables[req["variable"]] = value
                last_value = value
                note(f"turn {turn}: 提交变量 {req['variable']}={value}")

            execution = await _get_sop_execution(client, config, obs.conversation_id)
            if execution:
                obs.persisted_variables = execution.get("context_variables") or {}
                obs.pending_variable_name = execution.get("pending_variable_name")

            content = last_value
            metadata = {"kind": "interactive_response"}

        # 4) 收尾观测：守卫、终态信号、最终提问
        guard_after = await _snapshot_guard_metrics(client, config)
        if asserts.guard_metric_fired(guard_before, guard_after, _UNGROUNDED_QUESTION_METRIC):
            obs.guard_events.add("sop_user_questions_grounded_filter")
        if asserts.guard_metric_fired(guard_before, guard_after, _HARDBLOCK_NUMBERS_METRIC):
            obs.guard_events.add("hallucination_hard_blocked_ungrounded_numbers")

        if not obs.persisted_variables:
            execution = await _get_sop_execution(client, config, obs.conversation_id)
            if execution:
                obs.persisted_variables = execution.get("context_variables") or {}
                obs.pending_variable_name = execution.get("pending_variable_name")

        obs.observed_category_code, obs.sop_document_hit_id = _scan_terminal_signals(obs.events)
        obs.final_user_questions = asserts.extract_user_questions(obs.final_text)

        if dump_dir is not None:
            dump_dir.mkdir(parents=True, exist_ok=True)
            tag = f"{obs.case_id or 'na'}-{obs.conversation_id or 'na'}"
            (dump_dir / f"events-{tag}.jsonl").write_text(
                "\n".join(json.dumps(ev, ensure_ascii=False) for ev in obs.events), encoding="utf-8"
            )
            (dump_dir / f"observation-{tag}.json").write_text(
                json.dumps(
                    {
                        "case_id": obs.case_id,
                        "conversation_id": obs.conversation_id,
                        "terminal": obs.terminal,
                        "observed_category_code": obs.observed_category_code,
                        "sop_document_hit_id": obs.sop_document_hit_id,
                        "submitted_variables": obs.submitted_variables,
                        "persisted_variables": obs.persisted_variables,
                        "pending_variable_name": obs.pending_variable_name,
                        "final_user_questions": obs.final_user_questions,
                        "guard_events": sorted(obs.guard_events),
                        "error": obs.error,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    return obs
