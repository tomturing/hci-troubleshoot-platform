#!/usr/bin/env python3
"""Signal v2 仿真专用、无外网依赖的最小 OpenAI 兼容响应器。

它不是通用 LLM 模拟器，只接受全量样例中 ``Use%`` 百分比原文取值合同，
以及 VM 控制台样例的受控截图观察合同。不匹配的请求直接 422，避免仿真
误把未实现的模型能力判为通过。
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_REQUEST_BYTES = 128 * 1024


def build_completion(request: dict[str, Any]) -> dict[str, Any]:
    """把受支持的 AI 处理请求转换为 OpenAI chat completion。"""

    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages 必须是非空数组")
    user_message = next(
        (item for item in reversed(messages) if isinstance(item, dict) and item.get("role") == "user"),
        None,
    )
    if user_message is None:
        raise ValueError("缺少 user message")
    user_content = user_message.get("content")
    if isinstance(user_content, list):
        text_parts = [
            str(item.get("text") or "")
            for item in user_content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        image_parts = [
            item
            for item in user_content
            if isinstance(item, dict) and item.get("type") == "image_url"
        ]
        if not any("虚拟机控制台画面观察器" in item for item in text_parts) or len(image_parts) != 1:
            raise ValueError("仅支持 Signal v2 VM 控制台受控截图观察合同")
        image_url = ((image_parts[0].get("image_url") or {}).get("url") or "")
        if not isinstance(image_url, str) or not image_url.startswith("data:image/png;base64,"):
            raise ValueError("VM 控制台观察必须包含单张内联 PNG")
        content = json.dumps(
            {
                "display_state": "normal",
                "summary": "仿真控制台显示正常启动画面",
                "ocr_text": ["HCI Signal v2 simulation"],
                "visible_indicators": ["boot-screen-visible"],
                "confidence": 0.99,
                "needs_human_review": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return _completion(content, "chatcmpl-signal-v2-vision-sim")
    if not isinstance(user_content, str):
        raise ValueError("user message content 类型不受支持")
    try:
        contract = json.loads(user_content)
    except json.JSONDecodeError as exc:
        raise ValueError("user message 不是 AI 处理合同 JSON") from exc

    instruction = str(contract.get("instruction") or "")
    if contract.get("mode") != "extract" or contract.get("output_type") != "array":
        raise ValueError("仅支持 extract/array 合同")
    if "Use%" not in instruction or "百分比" not in instruction:
        raise ValueError("仅支持 Signal v2 样例的 Use% 百分比原文取值")
    candidates = contract.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates 必须是非空数组")

    output: list[float | int] = []
    evidence: list[dict[str, str]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        ref = candidate.get("ref")
        content = candidate.get("content")
        if not isinstance(ref, str) or not isinstance(content, str):
            continue
        matches = re.findall(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)%", content)
        if not matches:
            continue
        for raw in matches:
            value = float(raw)
            output.append(int(value) if value.is_integer() else value)
        evidence.append({"ref": ref, "quote": content})

    if not output or not evidence:
        raise ValueError("候选原文中没有百分比数值")
    signal_result = {
        "status": "success",
        "output": output,
        "evidence": evidence,
        "reason": "仿真桩按受限合同从带百分号的原文中逐字提取 Use% 数值",
    }
    content = json.dumps(signal_result, ensure_ascii=False, separators=(",", ":"))
    return _completion(content, "chatcmpl-signal-v2-extract-sim")


def _completion(content: str, completion_id: str) -> dict[str, Any]:
    return {
        "id": completion_id,
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


class Handler(BaseHTTPRequestHandler):
    server_version = "hci-semantic-llm-stub/1"

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._json(200, {"status": "ok", "scope": "semantic-signal-v2-simulation-only"})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("请求体大小不合法")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("请求体必须是对象")
            response = build_completion(request)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(422, {"error": {"message": str(exc), "type": "simulation_contract_error"}})
            return
        self._json(200, response)

    def log_message(self, _format: str, *_args: Any) -> None:
        # 请求正文可能含现场证据；仿真桩日志不记录正文。
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8009)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
