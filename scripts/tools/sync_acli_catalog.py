#!/usr/bin/env python3
"""同步 aCLI 官方命令列表到本地 catalog 快照。

设计原则（第一性原理）：
- 真值只存在于运行时：``acli --help`` / ``acli acli command list`` 优先于文档页；
- 文档页（:6888/commandList）是覆盖面最广的补充来源，但已知滞后（例如仍写
  ``acli plugins log_collect create``，而实机插件是顶层命名空间）；
- 因此采用「文档保覆盖 + override 表保正确 + 可选实机校验」三段式，禁止无脑覆盖。

运行时只读取仓库内 JSON 快照，不实时访问外部站点。

用法：
    uv run python scripts/tools/sync_acli_catalog.py                 # 文档 + override
    uv run python scripts/tools/sync_acli_catalog.py --dry-run       # 只打印差异
    uv run python scripts/tools/sync_acli_catalog.py --from-node root@172.28.25.1
                                                                     # 额外做实机校验（需 ssh -tt）
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE_URL = "http://acli.sangfor.com.cn:6888/commandList"
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "backend/shared/resolution/catalogs/acli_command_catalog.json"
OVERRIDES_PATH = REPO_ROOT / "backend/shared/resolution/catalogs/acli_catalog_overrides.json"

# 只读/写/危险动词：仅看命令路径的**最后一个 token**（即实际动作），避免
# ``acli vm config get`` 中的 config 被误判为写操作。
WRITE_VERBS = frozenset(
    {
        "add",
        "apply",
        "attach",
        "backup",
        "bind",
        "clean",
        "clear",
        "clone",
        "commit",
        "copy",
        "create",
        "delete",
        "deploy",
        "destroy",
        "detach",
        "disable",
        "down",
        "downgrade",
        "edit",
        "enable",
        "export",
        "format",
        "halt",
        "import",
        "install",
        "kill",
        "migrate",
        "modify",
        "mount",
        "move",
        "poweroff",
        "purge",
        "reboot",
        "recover",
        "reload",
        "remove",
        "rename",
        "repair",
        "replace",
        "reset",
        "restart",
        "restore",
        "rollback",
        "set",
        "shutdown",
        "snapshot",
        "start",
        "stop",
        "switch",
        "sync",
        "umount",
        "unbind",
        "uninstall",
        "update",
        "upgrade",
        "upload",
        "wipe",
    }
)
# 仅「不可逆删除/销毁」进入 danger：停机类动作（reboot/shutdown/reset）保持既有
# risk=2 语义，避免同步 Catalog 时无意改变线上确认/阻断行为。
DANGER_VERBS = frozenset({"delete", "destroy", "format", "purge", "remove", "wipe"})


def _strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def fetch_html() -> str:
    with urllib.request.urlopen(SOURCE_URL, timeout=20) as response:
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


def load_overrides() -> dict[str, Any]:
    """读取修正表；不存在时返回空表（保持脚本可独立运行）。"""
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"override 表解析失败，忽略：{exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def apply_overrides(commands: dict[str, dict[str, Any]], overrides: dict[str, Any]) -> dict[str, int]:
    """应用重命名/删除，返回统计信息。重命名后若与既有命令冲突，保留先出现者。"""
    stats = {"renamed": 0, "removed": 0, "added": 0, "dropped_on_conflict": 0}

    for rule in overrides.get("remove", []):
        command = str(rule.get("command", "")).strip()
        if command and command in commands:
            commands.pop(command)
            stats["removed"] += 1

    # 显式补充：上游删减但实机/既有契约仍需保留的命令。
    for rule in overrides.get("add", []):
        command = str(rule.get("command", "")).strip()
        if not command or command in commands:
            continue
        commands[command] = {
            "command": command,
            "summary": str(rule.get("summary", "")),
            "source": "override",
            "risk_hint": str(rule.get("risk_hint", "")),
        }
        stats["added"] += 1

    rename_map: dict[str, str] = {}
    for rule in overrides.get("rename", []):
        src = str(rule.get("from", "")).strip()
        dst = str(rule.get("to", "")).strip()
        if src and dst:
            rename_map[src] = dst

    prefix_rules = [
        (str(rule["from"]), str(rule["to"]))
        for rule in overrides.get("rename_prefix", [])
        if rule.get("from") and rule.get("to")
    ]

    rebuilt: dict[str, dict[str, Any]] = {}
    for command, item in commands.items():
        target = rename_map.get(command, command)
        for src_prefix, dst_prefix in prefix_rules:
            if target.startswith(src_prefix):
                target = dst_prefix + target[len(src_prefix) :]
                break
        if target != command:
            item = {**item, "source": "override"}
            stats["renamed"] += 1
        if target in rebuilt:
            # 重命名后撞车：保留先出现者，避免覆盖已有更权威条目。
            stats["dropped_on_conflict"] += 1
            continue
        rebuilt[target] = item
    commands.clear()
    commands.update(rebuilt)
    return stats


def derive_risk(command: str) -> str:
    """按命令路径末 token 推导风险提示；无法判定时返回 read，由分类器 fail-closed 兜底。"""
    tokens = command.split()
    if not tokens:
        return "read"
    action = tokens[-1].lower()
    if action in DANGER_VERBS:
        return "danger"
    if action in WRITE_VERBS:
        return "write"
    return "read"


def annotate(commands: dict[str, dict[str, Any]], overrides: dict[str, Any]) -> None:
    """为每条命令补充 source / risk_hint。override 显式风险优先级最高。"""
    risk_overrides = overrides.get("risk") or {}
    for command, item in commands.items():
        item.setdefault("source", "doc")
        hint = risk_overrides.get(command) if isinstance(risk_overrides, dict) else None
        # 优先级：override.risk > add 条目自带 risk_hint > 按动作推导。
        item["risk_hint"] = str(hint or item.get("risk_hint") or derive_risk(command))
        # 字段顺序稳定，便于 diff 审阅。
        commands[command] = {
            "command": command,
            "summary": str(item.get("summary", "")),
            "source": str(item.get("source", "doc")),
            "risk_hint": item["risk_hint"],
        }


def _ssh(host: str, command: str, timeout: int = 30) -> str:
    """在目标节点执行只读命令。必须分配伪终端：acli 插件命令会检查 stdin 是否为 tty。"""
    result = subprocess.run(  # noqa: S603 - 参数由调用方固定，无 shell
        ["ssh", "-tt", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={min(timeout, 15)}", host, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return (result.stdout or "") + (result.stderr or "")


def probe_node(host: str) -> dict[str, Any]:
    """采集实机版本与核心命令清单，用于校验与审计；失败不影响 catalog 生成。"""
    info: dict[str, Any] = {"node": host, "acli_version": None, "hci_version": None, "core_commands": []}
    try:
        version_out = _ssh(host, "acli --version")
        match = re.search(r"^\s*(\d+\.\d+(?:\.\d+)?)\s*$", version_out, flags=re.M)
        if match:
            info["acli_version"] = match.group(1)
        hci_out = _ssh(host, "cat /sf/version")
        hci_match = re.search(r"^\s*(\d+\.\d+\.\d+)", hci_out, flags=re.M)
        if hci_match:
            info["hci_version"] = hci_match.group(1)

        listing = _ssh(host, "acli acli command list", timeout=60)
        for line in listing.splitlines():
            parts = [cell.strip() for cell in line.split("|")]
            if len(parts) < 3:
                continue
            command_cell, namespace = parts[0], parts[2]
            # 行首形如 "12  list"，去掉序号。
            command_name = re.sub(r"^\d+\s+", "", command_cell).strip()
            if not command_name or not namespace or " " in command_name:
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_.-]*", namespace):
                continue
            path = "acli " + " ".join(namespace.split(".") + [command_name])
            info["core_commands"].append(path)
    except (subprocess.SubprocessError, OSError) as exc:
        info["error"] = str(exc)
    return info


def build_payload(
    source_updated_at: str | None,
    commands: dict[str, dict[str, Any]],
    node_info: dict[str, Any] | None,
) -> dict[str, Any]:
    ordered = [commands[key] for key in sorted(commands)]
    payload: dict[str, Any] = {
        "schema_version": 2,
        "source_url": SOURCE_URL,
        "source_updated_at": source_updated_at,
        "generated_at": datetime.now(UTC).isoformat(),
        "hash": hashlib.sha256(json.dumps(ordered, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "commands": ordered,
    }
    if node_info:
        payload["node_verified_at"] = datetime.now(UTC).isoformat()
        payload["node_acli_version"] = node_info.get("acli_version")
        payload["node_hci_version"] = node_info.get("hci_version")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步 aCLI 命令快照（文档 + override + 可选实机校验）")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计与差异，不写文件")
    parser.add_argument("--from-node", default="", help="可选：root@host，用于实机校验与版本记录")
    parser.add_argument("--out", default=str(CATALOG_PATH), help="输出路径")
    args = parser.parse_args(argv)

    page = fetch_html()
    source_updated_at, doc_commands = parse_commands(page)
    if not doc_commands:
        print("未解析到任何 aCLI 命令，拒绝写入空 catalog", file=sys.stderr)
        return 1

    commands: dict[str, dict[str, Any]] = {item["command"]: {**item, "source": "doc"} for item in doc_commands}

    # 保留旧快照中文档未覆盖的条目（上游删减未必代表实机移除），交给 override 决策。
    out_path = Path(args.out)
    if out_path.exists():
        try:
            previous = json.loads(out_path.read_text(encoding="utf-8"))
            for item in previous.get("commands", []):
                command = str(item.get("command", "")).strip()
                if command and command not in commands:
                    commands[command] = {
                        "command": command,
                        "summary": str(item.get("summary", "")),
                        "source": "legacy",
                    }
        except (OSError, ValueError) as exc:
            print(f"旧快照读取失败，忽略：{exc}", file=sys.stderr)

    overrides = load_overrides()
    stats = apply_overrides(commands, overrides)
    annotate(commands, overrides)

    node_info = probe_node(args.from_node) if args.from_node else None
    payload = build_payload(source_updated_at, commands, node_info)

    risk_stat: dict[str, int] = {}
    for item in payload["commands"]:
        risk_stat[item["risk_hint"]] = risk_stat.get(item["risk_hint"], 0) + 1

    print(
        f"文档 {len(doc_commands)} 条 | 合并后 {len(payload['commands'])} 条 | "
        f"重命名 {stats['renamed']} 删除 {stats['removed']} 补充 {stats['added']} "
        f"冲突丢弃 {stats['dropped_on_conflict']} | "
        f"风险分布 {risk_stat}"
    )
    if node_info:
        print(
            f"实机校验：acli={node_info.get('acli_version')} hci={node_info.get('hci_version')} "
            f"核心命令 {len(node_info.get('core_commands', []))} 条"
        )
        catalog_paths = {tuple(item["command"].split()[1:]) for item in payload["commands"]}
        missing = sorted(
            path for path in node_info.get("core_commands", []) if tuple(path.split()[1:]) not in catalog_paths
        )
        if missing:
            print(f"实机存在但 catalog 缺失 {len(missing)} 条（示例前 10）：")
            for path in missing[:10]:
                print(f"  ! {path}")

    if args.dry_run:
        print("dry-run：未写入文件")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
