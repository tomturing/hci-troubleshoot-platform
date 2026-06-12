#!/usr/bin/env python3
"""
同步 aCLI 官方命令列表到本地 catalog 快照。

运行时只读取仓库内 JSON 快照，不实时访问外部站点。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SOURCE_URL = "http://acli.sangfor.com.cn:6888/commandList"
CATALOG_PATH = Path(__file__).resolve().parents[2] / "backend/agent-service/app/tools/acli/catalog/acli_command_catalog.json"


def _strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def fetch_html() -> str:
    with urllib.request.urlopen(SOURCE_URL, timeout=15) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_commands(page: str) -> tuple[str | None, list[dict[str, str]]]:
    updated_match = re.search(r"更新时间:\s*([0-9-]+)", page)
    source_updated_at = updated_match.group(1) if updated_match else None

    commands: list[dict[str, str]] = []
    for row in re.findall(r"<tr>(.*?)</tr>", page, flags=re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
        if len(cells) < 2:
            continue
        command = _strip_tags(cells[0])
        summary = _strip_tags(cells[1])
        if command.startswith("acli "):
            commands.append({"command": " ".join(command.split()), "summary": " ".join(summary.split())})

    deduped = {item["command"]: item for item in commands}
    return source_updated_at, [deduped[key] for key in sorted(deduped)]


def main() -> int:
    page = fetch_html()
    source_updated_at, commands = parse_commands(page)
    if not commands:
        print("未解析到任何 aCLI 命令，拒绝写入空 catalog", file=sys.stderr)
        return 1

    payload = {
        "source_url": SOURCE_URL,
        "source_updated_at": source_updated_at,
        "generated_at": datetime.now(UTC).isoformat(),
        "hash": hashlib.sha256(json.dumps(commands, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "commands": commands,
    }

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {CATALOG_PATH}，命令数：{len(commands)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
