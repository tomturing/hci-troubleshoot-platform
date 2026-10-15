#!/usr/bin/env python3
"""硬盘坏道 Live E2E 专用确定性对抗 LLM 桩（无外网依赖，OpenAI 兼容）。

定位（第一性原理）：真实 LLM 输出不可复现，无法满足「防挤牙膏」与计划验收标准
『破坏缝隙能验红』。本桩把已知的坏行为**确定性地**注入到真实的 agent-service 上——
把 `LLM_BASE_URL` 指向本桩后，agent 的 S2 轮次会收到含「重复索取已提交 disk_dev」与
「凭空索要未声明 disk_sn/disk_type」的 ReasoningOutput，从而迫使引擎侧
`react_engine.filter_ungrounded_user_questions` 真实触发并被 E2E 断言捕获。

只声明支持 ReasoningOutput 结构化生成合同（request.response_format=json_object 且
messages 含统一输出约束标记 user_questions），不匹配的请求 422，避免把未实现能力判过。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 512 * 1024
# 与统一输出约束/引擎一致：只认这条结构化生成合同。
_OUTPUT_CONTRACT_MARKERS = ("user_questions", "next_state")

# 默认对抗载荷（可被 --scenario 覆盖）：复刻 Q2026101511258 的真实坏输出。
_DEFAULT_ADVERSARIAL = {
    "inject_user_questions": [
        "请正式提交 disk_dev，当前该变量槽位仍为 needs_input 状态",
        "请提供该盘的 disk_sn 与 disk_type 以便进一步判断",
    ],
    "next_state": "S2",
}


def _system_text(messages: list[Any]) -> str:
    parts: list[str] = []
    for item in messages:
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            parts.append(item["content"])
    return "\n".join(parts)


def is_reasoning_output_request(request: dict[str, Any]) -> bool:
    """判定本请求是否为「生成 ReasoningOutput」的结构化调用。"""
    response_format = request.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_object":
        return False
    messages = request.get("messages")
    if not isinstance(messages, list):
        return False
    blob = _system_text(messages)
    return all(marker in blob for marker in _OUTPUT_CONTRACT_MARKERS)


def build_completion(request: dict[str, Any], adversarial: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 ReasoningOutput 生成合同转换为携带对抗 user_questions 的 OpenAI completion。

    纯函数，无网络/IO，便于单测。非受支持合同抛 ValueError（Handler 转 422）。
    """
    payload = adversarial or _DEFAULT_ADVERSARIAL
    if not is_reasoning_output_request(request):
        raise ValueError("仅支持 ReasoningOutput 结构化生成合同（response_format=json_object + 输出约束标记）")
    reasoning_output = {
        "summary": "已收集告警与节点信息，disk_dev 已就绪，进入假设生成阶段。",
        "hypotheses": [
            {
                "hypothesis_id": "hyp-1",
                "statement": "5 号盘存在坏道，需结合 smartctl 判定是否更换",
                "confidence": 0.7,
                "supporting_fact_ids": [],
                "contradicting_fact_ids": [],
                "verification_plan": [],
            }
        ],
        "evidence_needed": [],
        "tool_requests": [],
        "unsupported_claims": [],
        # 刻意注入的历史坏行为，交由引擎过滤
        "user_questions": list(payload.get("inject_user_questions") or []),
        "next_state": str(payload.get("next_state") or "S2"),
    }
    content = json.dumps(reasoning_output, ensure_ascii=False, separators=(",", ":"))
    return {
        "id": "chatcmpl-disk-badtrack-adversarial",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def load_adversarial_from_scenario(path: str | None) -> dict[str, Any]:
    if not path:
        return dict(_DEFAULT_ADVERSARIAL)
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    turn = spec.get("stub_adversarial_turn") or {}
    return {
        "inject_user_questions": turn.get("inject_user_questions") or _DEFAULT_ADVERSARIAL["inject_user_questions"],
        "next_state": turn.get("next_state") or _DEFAULT_ADVERSARIAL["next_state"],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "hci-disk-badtrack-llm-stub/1"
    adversarial: dict[str, Any] = dict(_DEFAULT_ADVERSARIAL)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._json(200, {"status": "ok", "scope": "disk-badtrack-adversarial-llm-stub"})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # 兼容内部网关 `/v1/chat/completions` 与被 ai_client 判为外部端点时的 `/chat/completions`。
        if not re.search(r"/(?:v1/)?chat/completions/?$", self.path):
            self._json(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("请求体大小不合法")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("请求体必须是对象")
            response = build_completion(request, self.adversarial)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(422, {"error": {"message": str(exc), "type": "simulation_contract_error"}})
            return
        self._json(200, response)

    def log_message(self, _format: str, *_args: Any) -> None:
        # 请求正文含现场证据，桩日志不落正文。
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--scenario", default=os.getenv("HCI_LIVE_E2E_SCENARIO", ""))
    args = parser.parse_args()
    Handler.adversarial = load_adversarial_from_scenario(args.scenario or None)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
