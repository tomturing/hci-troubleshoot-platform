#!/usr/bin/env python3
"""CLI -> OpenAI-compatible adapter.

Expose minimal endpoints:
- POST /v1/chat/completions
- GET  /v1/models
- GET  /health

The adapter runs a local command and maps output into OpenAI chat responses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
LOG_PREFIX_RE = re.compile(r"^(DEBUG|INFO|WARN|WARNING|ERROR|TRACE|\d{4}-\d{2}-\d{2}T)")


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class AdapterConfig:
    host: str = env_str("ADAPTER_HOST", "0.0.0.0")
    port: int = env_int("ADAPTER_PORT", 43101)

    bearer_token: str = env_str("ADAPTER_BEARER_TOKEN", "")
    default_model: str = env_str("ADAPTER_MODEL", "cli-adapter")

    cmd_json: str = env_str("ADAPTER_CMD_JSON", "")
    cmd_shell: str = env_str("ADAPTER_CMD", "")
    timeout_sec: int = env_int("ADAPTER_TIMEOUT_SEC", 120)

    prompt_mode: str = env_str("ADAPTER_PROMPT_MODE", "last_user")
    max_prompt_chars: int = env_int("ADAPTER_MAX_PROMPT_CHARS", 12000)
    stream_chunk_chars: int = env_int("ADAPTER_STREAM_CHUNK_CHARS", 64)

    response_regex: str = env_str("ADAPTER_RESPONSE_REGEX", "")
    response_prefix: str = env_str("ADAPTER_RESPONSE_PREFIX", "")


CFG = AdapterConfig()


def json_error(message: str, error_type: str = "adapter_error", code: str = "internal_error") -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
        }
    }


def parse_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                # OpenAI-style multimodal payload: {"type":"text","text":"..."}
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                elif isinstance(item.get("text"), str):
                    parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def build_prompt(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""

    mode = (CFG.prompt_mode or "last_user").strip().lower()
    if mode == "transcript":
        lines: list[str] = []
        for item in messages:
            role = str(item.get("role", "user"))
            text = parse_content(item.get("content", "")).strip()
            if text:
                lines.append(f"{role}: {text}")
        prompt = "\n".join(lines)
    else:
        # default: last user message
        prompt = ""
        for item in reversed(messages):
            if str(item.get("role", "")).lower() == "user":
                prompt = parse_content(item.get("content", "")).strip()
                break
        if not prompt:
            prompt = parse_content(messages[-1].get("content", "")).strip()

    if len(prompt) > CFG.max_prompt_chars:
        prompt = prompt[-CFG.max_prompt_chars :]
    return prompt


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def extract_response(stdout: str, stderr: str) -> str:
    out = strip_ansi(stdout or "")
    err = strip_ansi(stderr or "")
    merged = (out + "\n" + err).strip()

    # Optional regex extractor (group 1 if present, else full match)
    if CFG.response_regex:
        try:
            m = re.search(CFG.response_regex, merged, re.MULTILINE | re.DOTALL)
            if m:
                if m.groups():
                    return m.group(1).strip()
                return m.group(0).strip()
        except re.error:
            pass

    candidates: list[str] = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if LOG_PREFIX_RE.match(s):
            continue
        if "Interactive mode" in s or "Goodbye!" in s:
            continue
        candidates.append(s)

    if not candidates:
        candidates = [ln.strip() for ln in merged.splitlines() if ln.strip()]

    if not candidates:
        return ""

    answer = candidates[-1]
    prefix = CFG.response_prefix.strip()
    if prefix and answer.startswith(prefix):
        answer = answer[len(prefix) :].strip()

    # PicoClaw default logo prefix
    if answer.startswith("🦞 "):
        answer = answer[2:].strip()

    return answer


def render_cmd(template: str, prompt: str, session: str, model: str) -> str:
    return template.replace("{prompt}", prompt).replace("{session}", session).replace("{model}", model)


def run_cli(prompt: str, session: str, model: str) -> str:
    if not CFG.cmd_json and not CFG.cmd_shell:
        raise RuntimeError("ADAPTER_CMD_JSON / ADAPTER_CMD 未配置")

    if CFG.cmd_json:
        raw = json.loads(CFG.cmd_json)
        if not isinstance(raw, list) or not raw:
            raise RuntimeError("ADAPTER_CMD_JSON 必须是非空 JSON 数组")
        args = [render_cmd(str(part), prompt, session, model) for part in raw]
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=CFG.timeout_sec,
        )
    else:
        cmd = render_cmd(CFG.cmd_shell, prompt, session, model)
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=CFG.timeout_sec,
        )

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit={proc.returncode}"
        raise RuntimeError(f"CLI 执行失败: {detail}")

    answer = extract_response(proc.stdout or "", proc.stderr or "")
    if not answer:
        raise RuntimeError("CLI 返回为空，未提取到可用回复")
    return answer


def make_completion_response(model: str, content: str) -> dict[str, Any]:
    now = int(time.time())
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    usage_prompt = max(1, len(content) // 4)
    usage_completion = max(1, len(content) // 4)
    return {
        "id": cid,
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage_prompt,
            "completion_tokens": usage_completion,
            "total_tokens": usage_prompt + usage_completion,
        },
    }


def stream_chunks(model: str, content: str) -> list[bytes]:
    now = int(time.time())
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    chunk_size = max(1, CFG.stream_chunk_chars)

    payloads: list[dict[str, Any]] = []
    payloads.append(
        {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )

    for i in range(0, len(content), chunk_size):
        segment = content[i : i + chunk_size]
        payloads.append(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": segment}, "finish_reason": None}],
            }
        )

    payloads.append(
        {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )

    raw: list[bytes] = []
    for item in payloads:
        raw.append(("data: " + json.dumps(item, ensure_ascii=False) + "\n\n").encode("utf-8"))
    raw.append(b"data: [DONE]\n\n")
    return raw


class AdapterHandler(BaseHTTPRequestHandler):
    server_version = "cli-openai-adapter/0.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _require_auth(self) -> bool:
        if not CFG.bearer_token:
            return True
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {CFG.bearer_token}"
        if auth == expected:
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            json_error("Unauthorized", error_type="invalid_request_error", code="unauthorized"),
        )
        return False

    def _read_json(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep logs concise and stderr-only.
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            payload = {
                "status": "ok",
                "adapter": "cli-openai-adapter",
                "model": CFG.default_model,
                "cmd_configured": bool(CFG.cmd_json or CFG.cmd_shell),
            }
            self._send_json(HTTPStatus.OK, payload)
            return

        if self.path == "/v1/models":
            payload = {
                "object": "list",
                "data": [
                    {
                        "id": CFG.default_model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "cli-openai-adapter",
                    }
                ],
            }
            self._send_json(HTTPStatus.OK, payload)
            return

        self._send_json(HTTPStatus.NOT_FOUND, json_error("Not Found", code="not_found"))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send_json(HTTPStatus.NOT_FOUND, json_error("Not Found", code="not_found"))
            return

        if not self._require_auth():
            return

        try:
            body = self._read_json()
        except json.JSONDecodeError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                json_error("Invalid JSON body", error_type="invalid_request_error", code="invalid_json"),
            )
            return

        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                json_error(
                    "'messages' must be a non-empty array", error_type="invalid_request_error", code="invalid_messages"
                ),
            )
            return

        model = str(body.get("model") or CFG.default_model)
        stream = bool(body.get("stream", False))
        session = str(body.get("user") or f"adapter-{uuid.uuid4().hex[:8]}")

        prompt = build_prompt(messages)
        if not prompt:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                json_error(
                    "No prompt extracted from messages", error_type="invalid_request_error", code="empty_prompt"
                ),
            )
            return

        try:
            answer = run_cli(prompt=prompt, session=session, model=model)
        except subprocess.TimeoutExpired:
            self._send_json(
                HTTPStatus.GATEWAY_TIMEOUT,
                json_error("CLI execution timeout", code="timeout"),
            )
            return
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                json_error(str(exc), code="cli_error"),
            )
            return

        if not stream:
            self._send_json(HTTPStatus.OK, make_completion_response(model=model, content=answer))
            return

        frames = stream_chunks(model=model, content=answer)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in frames:
            self.wfile.write(frame)
            self.wfile.flush()


def main() -> None:
    server = ThreadingHTTPServer((CFG.host, CFG.port), AdapterHandler)
    print(
        f"cli-openai-adapter listening on http://{CFG.host}:{CFG.port} "
        f"(model={CFG.default_model}, cmd={'json' if CFG.cmd_json else 'shell' if CFG.cmd_shell else 'unset'})"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
