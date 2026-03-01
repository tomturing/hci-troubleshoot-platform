#!/usr/bin/env python3
"""NanoClaw compatibility runner.

This runner keeps the adapter contract stable for `/v1/chat/completions`
by forwarding one-shot requests to an OpenAI-compatible upstream.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.parse


def get_env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--session", default="nc-session")
    parser.add_argument("--model", default="nanoclaw")
    args = parser.parse_args()

    base_url = get_env("NANOCLAW_UPSTREAM_BASE_URL", "http://openclaw:18789/v1")
    token = get_env(
        "NANOCLAW_UPSTREAM_TOKEN",
        get_env("OPENCLAW_GATEWAY_TOKEN", "hci-dev-openclaw-token"),
    )
    upstream_model = get_env("NANOCLAW_UPSTREAM_MODEL", "openclaw")
    timeout_sec = int(get_env("NANOCLAW_UPSTREAM_TIMEOUT_SEC", "60"))

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        print("invalid upstream url scheme", file=sys.stderr)
        return 2

    host = parsed.hostname or ""
    if not host:
        print("invalid upstream host", file=sys.stderr)
        return 2
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path_prefix = parsed.path.rstrip("/")
    url_path = f"{path_prefix}/chat/completions"

    payload = {
        "model": upstream_model,
        "messages": [{"role": "user", "content": args.prompt}],
        "stream": False,
        "user": f"nanoclaw:{args.session}",
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port=port, timeout=timeout_sec)
    try:
        conn.request("POST", url_path, body=json.dumps(payload), headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
    finally:
        conn.close()

    if resp.status < 200 or resp.status >= 300:
        print(f"upstream error status={resp.status} body={raw[:400]}", file=sys.stderr)
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"upstream returned non-json: {raw[:200]}", file=sys.stderr)
        return 1

    content = ""
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = str(message.get("content") or "").strip()

    if not content:
        print(f"upstream empty response: {raw[:400]}", file=sys.stderr)
        return 1

    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
