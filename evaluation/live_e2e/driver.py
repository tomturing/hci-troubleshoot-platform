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

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
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
    # 真实采集（headless bridge relay）模式：SIM=real 时经 terminal_bridge 把命令真下发到 hci-sim 节点。
    sim: bool = False
    bridge_url: str | None = None
    bridge_origin: str | None = None
    connection: dict[str, Any] | None = None


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
    collected_exec_ids: set[str] = field(default_factory=set)
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
    on_exec_command: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> str:
    """POST /message 并把 SSE 解析成 typed 事件（含 text/interactive_request/metadata/tool_call）。

    返回本轮拼接出的助手文本。typed 事件形如 {"event": <类型|'message'>, "data": <obj>}。
    真实采集模式下，事件 `agent_exec_command` 到达时经 `on_exec_command` 内联回贴（命令经
    bridge 真执行 → POST /exec-result），令阻塞的 agent `blpop` 解阻塞后继续流式。
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
                    if cur_event == "agent_exec_command" and on_exec_command is not None:
                        await on_exec_command(handled)
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


async def _create_sop_execution(
    client: httpx.AsyncClient, config: DriverConfig, conversation_id: str, sop_document_id: int, root_node_id: str = "n-1"
) -> tuple[int, dict[str, Any]]:
    """POST /{conv}/sop/create 把会话直接绑进指定 SOP 执行（跳过 S0 意图分类，用于引擎采集段验真）。

    返回 (status_code, 响应体)。非 2xx 由调用方决定是否终止。
    """
    url = f"{config.conv_base}/api/conversations/{conversation_id}/sop/create"
    resp = await client.post(
        url,
        json={"sop_document_id": sop_document_id, "root_node_id": root_node_id},
        headers=_admin_headers(config),
        timeout=config.request_timeout,
    )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"raw": resp.text[:200]}
    return resp.status_code, body


async def _seed_case_environment(
    client: httpx.AsyncClient, config: DriverConfig, case_id: str, seed: dict[str, Any]
) -> list[str]:
    """向本次新建的测试工单 upsert 环境事实（幂等 PUT，签名身份 + 工单归属）。

    USE_RAW 路径下 conversation-service 会 `get_raw_environments` 取回各 env_type 的 env_data：
    cluster 型 env_data 直接作 env_info；alert 型 env_data 须为 {"alerts":[...]}（见 environment_client L625）。
    返回失败列表（空即全部写入成功）。
    """
    payloads: list[tuple[str, dict[str, Any]]] = []
    cluster = seed.get("cluster")
    if isinstance(cluster, dict) and cluster:
        payloads.append(("cluster", cluster))
    alerts = seed.get("alerts")
    if isinstance(alerts, list) and alerts:
        payloads.append(("alert", {"alerts": alerts}))

    failed: list[str] = []
    for env_type, env_data in payloads:
        url = f"{config.case_base}/api/environments/case/{case_id}/type/{env_type}"
        resp = await client.put(
            url, json={"env_data": env_data}, headers=_identity_headers(config), timeout=config.request_timeout
        )
        if resp.status_code not in (200, 201):
            failed.append(f"{env_type}:{resp.status_code}:{resp.text[:80]}")
    return failed


async def _seed_sop_variables(
    client: httpx.AsyncClient, config: DriverConfig, conversation_id: str, seed_vars: dict[str, str]
) -> list[str]:
    """在首条消息前，把易触发 LLM 自动取值（skill_call）而超时不定的派生变量直接种入 SOP 变量池。

    第一性原理：agent 变量池对已存在且非空的变量会缓存命中、不再跑 skill_call（见“失败即接管维持 pending”回归）。
    后端 `variable-response` 在 status==active（`/sop/create` 创建后即为 active）时允许不校验 pending
    直接写入任意变量（sop_execution.py active 分支）。因此预种这些变量可绕过 staging 外部模型 MaaS 超时，
    令「采集→下发→回灌→结论」链路确定性推进——同时是跨场景通用能力（任何场景都可声明 seed_variables）。
    返回失败列表（空即全部写入成功）。
    """
    failed: list[str] = []
    url = f"{config.conv_base}/api/conversations/{conversation_id}/sop/variable-response"
    for name, value in (seed_vars or {}).items():
        if not name or name.startswith("_") or value in (None, ""):
            continue
        resp = await client.post(
            url,
            json={"variable_name": name, "value": str(value), "source": "user_input"},
            headers=_admin_headers(config),
            timeout=config.request_timeout,
        )
        if resp.status_code not in (200, 201):
            failed.append(f"{name}:{resp.status_code}:{resp.text[:80]}")
    return failed


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


class BridgeRelay:
    """headless terminal_bridge WebSocket 客户端：复刻 hci-sim-smoke 协议扮演「前端+桥」那一端。

    职责：连上 bridge → ssh_connect(用 connection.json 的租约凭据) → 收 `ssh_connected`；
    之后 execute() 对每条命令发 `ssh_exec_process`，等匹配 exec_id 的 `exec_result` 取回
    stdout/stderr/exit_code——命令真的经 bridge SSH 到 hci-sim 节点采集。
    """

    def __init__(
        self,
        bridge_url: str,
        connection: dict[str, Any],
        *,
        origin: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._url = bridge_url
        self._connection = connection
        self._origin = origin
        self._timeout = timeout
        self._ws: Any = None
        self._case_id = f"SIM-KBD-{connection.get('support_id', 'live-e2e')}"

    async def connect(self) -> None:
        import websockets

        kwargs: dict[str, Any] = {"open_timeout": self._timeout, "close_timeout": self._timeout}
        if self._origin:
            kwargs["origin"] = self._origin
        self._ws = await websockets.connect(self._url, **kwargs)
        await self._send(asserts.bridge_ssh_connect_msg(self._connection))
        await self._recv_until("ssh_connected")

    async def execute(
        self, command: str, exec_id: str, container: str = "host", timeout: int = 15
    ) -> dict[str, Any]:
        await self._send(asserts.bridge_exec_msg(command, exec_id, container, timeout))
        msg = await self._recv_until("exec_result", exec_id)
        return {
            "stdout": msg.get("stdout") or "",
            "stderr": msg.get("stderr") or "",
            "exit_code": msg.get("exit_code", msg.get("exitCode", 0)),
            "error_type": msg.get("error_type"),
            "duration_ms": msg.get("duration_ms"),
            "timed_out": msg.get("timed_out", False),
            "cancelled": msg.get("cancelled", False),
        }

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _recv_until(self, expected_type: str, exec_id: str = "") -> dict[str, Any]:
        deadline = self._timeout + 20
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=deadline)
            try:
                cur = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(cur, dict):
                continue
            if cur.get("type") == "ssh_error":
                raise RuntimeError(f"bridge ssh_error: {cur.get('message', '')}")
            if cur.get("type") == expected_type and (not exec_id or cur.get("exec_id") == exec_id):
                return cur

    async def aclose(self) -> None:
        if self._ws is None:
            return
        with contextlib.suppress(Exception):
            await self._send({"type": "ssh_disconnect", "case_id": self._case_id})
        with contextlib.suppress(Exception):
            await self._ws.close()
        self._ws = None


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


def merge_observed_signals(
    live_category: str | None, live_sop_id: int | None, scanned: tuple[str | None, int | None]
) -> tuple[str | None, int | None]:
    """合并「交互链路已确证值」与「终态事件尽力扫描值」：已确证优先保留，缺失才用扫描兜底。

    第一性原理：S0 走候选澄清时 driver 已按用户点选记录 observed_category_code，但点选确认后的 SOP
    续跑终态事件未必再带 category_code 字段，_scan_terminal_signals 返回 None —— 若直接赋值会把正确值
    冲成假阴性（simrunM attempt1 即此症状）。故非空优先，仅当已确证为空时用扫描值兜底，兼容「模型直接
    确认分类、不经点选」的路径。对 sop_document_hit_id 同理。
    """
    scanned_category, scanned_sop_id = scanned
    return live_category or scanned_category, live_sop_id or scanned_sop_id


async def run_scenario(
    config: DriverConfig,
    spec: dict[str, Any],
    dump_dir: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> RunObservation:
    """驱动一次完整端到端会话，返回结构化观测（不含断言结论，断言在 smoke.py 汇总）。"""
    obs = RunObservation()
    answers: dict[str, str] = dict((spec.get("variables") or {}).get("answers") or {})
    expected_category = (spec.get("expect") or {}).get("category_code")
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

        # 1.4) 可选：向工单挂真实环境事实，消除 S0 facts=0 根因（否则 qwen 乱给候选、无法确定性进 SOP）。
        seed = spec.get("seed_environment")
        if seed:
            failed = await _seed_case_environment(client, config, obs.case_id, seed)
            if failed:
                obs.error = f"环境事实注入失败: {failed}"
                return obs
            note(f"case environment seeded (alert/cluster)")

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

        # 1.5) 可选：直连绑定 SOP 执行（跳过 flaky S0 分类，专验引擎采集→真下发→真回灌闭环）。
        bind_sop = spec.get("bind_sop_document_id")
        if bind_sop:
            code, body = await _create_sop_execution(client, config, obs.conversation_id, int(bind_sop))
            if code not in (200, 201):
                obs.error = f"直连绑定 SOP#{bind_sop} 失败 {code}: {str(body)[:200]}"
                return obs
            note(f"SOP#{bind_sop} execution bound (跳过 S0 分类)")

        # 1.6) 预种易触发 LLM 超时不定的派生变量（skill_call 产物），令链路确定性推进到结论。
        seed_vars = spec.get("seed_variables") or {}
        if seed_vars and obs.conversation_id:
            seed_failed = await _seed_sop_variables(client, config, obs.conversation_id, seed_vars)
            if seed_failed:
                note(f"预种变量部分失败: {seed_failed}")
            else:
                seeded_keys = [k for k in seed_vars if k and not k.startswith("_")]
                note(f"已预种变量以绕过 LLM 自动取值: {seeded_keys}")

        guard_before = await _snapshot_guard_metrics(client, config)

        # 真实采集：SIM=real 且已拿到 connection.json + bridge 地址时，起 headless bridge relay，
        # 把会话 SSE 上的 agent_exec_command 真下发到 hci-sim 节点并把结果回贴 /exec-result。
        relay: BridgeRelay | None = None
        if config.sim and config.bridge_url and config.connection:
            relay = BridgeRelay(config.bridge_url, config.connection, origin=config.bridge_origin)
            try:
                await relay.connect()
                note("bridge relay connected (ssh_connect ok)")
            except Exception as exc:  # noqa: BLE001
                obs.error = f"bridge relay 连接失败: {exc}"
                return obs

        async def _relay_exec(handled: dict[str, Any]) -> None:
            if relay is None:
                return
            cmd = asserts.normalize_agent_exec_command(handled)
            if not cmd["exec_id"]:
                return
            if cmd.get("risk_level") == 3:  # 高危被策略拦截，测试端不代为确认
                return
            try:
                result = await relay.execute(cmd["command"], cmd["exec_id"], cmd["container"], cmd["timeout"])
            except Exception as exc:  # noqa: BLE001
                note(f"relay 执行失败 exec_id={cmd['exec_id']}: {exc}")
                return
            payload = asserts.build_exec_result_payload(cmd, result)
            resp = await client.post(
                f"{config.conv_base}/api/conversations/{obs.conversation_id}/exec-result",
                json=payload,
                headers=_identity_headers(config),
                timeout=config.request_timeout,
            )
            if resp.status_code == 200:
                obs.collected_exec_ids.add(cmd["exec_id"])
                preview = (result["stdout"] or result["stderr"] or "").replace("\n", " ")[:80]
                note(f"collected via bridge: {cmd['command'][:40]} -> exit={result['exit_code']} out[{len(result['stdout'])}]={preview}")
            else:
                note(f"exec-result 回贴失败 {resp.status_code}: {resp.text[:120]}")

        # 3) 首条告警消息 → SSE
        content = first_message
        metadata: dict[str, Any] | None = None
        for turn in range(config.max_turns):
            events_this_turn: list[dict[str, Any]] = []
            text = await _stream_message(
                client, config, obs.conversation_id, obs.case_id, content, metadata, events_this_turn,
                on_exec_command=_relay_exec if relay else None,
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
                # 候选澄清优先：S0 若回 choice_options（模型给分类候选而非直接确认），
                # driver 扮演前端点选期望分类推进 S0→S1，绝不可当成终态误判（真实用户就选这一项）。
                options = asserts.extract_choice_options(events_this_turn)
                chosen = asserts.match_candidate_option(options, expected_category) if options else None
                if chosen:
                    content, metadata = asserts.build_category_selection(chosen)
                    obs.observed_category_code = chosen.get("code") or chosen.get("optionId") or expected_category
                    note(f"turn {turn}: 点选 S0 候选 {content} → 推进 S1")
                    continue
                if options:
                    note(f"turn {turn}: S0 候选不含期望分类 {expected_category}，判定终态")
                obs.terminal = True
                note(f"turn {turn}: 无交互卡，判定终态")
                break

            # #1149 时序：先提交并确认落库，再触发续跑消息
            last_value = ""
            for req in var_reqs:
                value = answers.get(req["variable"])
                if value is None:
                    # 缺应答不硬中断：记录后 break，落到收尾落盘段（否则丢失本轮事件证据）。
                    obs.error = f"应答脚本缺少变量 {req['variable']} 的值"
                    note(f"turn {turn}: 缺应答 {req['variable']}，停止续跑（已采命令仍计入 V017）")
                    break
                ok = await _submit_variable(client, config, obs.conversation_id, req, value)
                if not ok:
                    obs.error = f"变量提交失败 {req['variable']}"
                    break
                obs.submitted_variables[req["variable"]] = value
                last_value = value
                note(f"turn {turn}: 提交变量 {req['variable']}={value}")
            else:
                # for-else：仅当未 break（全部变量成功提交）才继续取回变量并发起续跑消息
                execution = await _get_sop_execution(client, config, obs.conversation_id)
                if execution:
                    obs.persisted_variables = execution.get("context_variables") or {}
                    obs.pending_variable_name = execution.get("pending_variable_name")

                content = last_value
                metadata = {"kind": "interactive_response"}
                continue
            # for 循环被 break（缺应答/提交失败）→ 终止会话驱动
            break

        # 4) 收尾观测：守卫、终态信号、最终提问
        if relay is not None:
            await relay.aclose()
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

        # 尽力从终态事件补全「确认分类 code / 命中 sop_document_id」，但不得覆盖已在交互链路中
        # 确证的值：S0 走候选澄清时 driver 已按「用户点选期望分类」记录 observed_category_code（第 531 行），
        # 而分类确认后的 SOP 续跑事件未必再带 category_code 字段，_scan 会返回 None 把它冲掉（假阴性）。
        obs.observed_category_code, obs.sop_document_hit_id = merge_observed_signals(
            obs.observed_category_code, obs.sop_document_hit_id, _scan_terminal_signals(obs.events)
        )
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
                        "collected_exec_ids": sorted(obs.collected_exec_ids),
                        "error": obs.error,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    return obs
