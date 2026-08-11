"""跨领域 Catalog 读取和安全命令匹配辅助。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def load_acli_catalog() -> tuple[dict[str, Any], ...]:
    """读取仓库内的 aCLI 快照；读取失败时返回空快照并由 Resolver 给出 warning。"""

    backend_root = Path(__file__).resolve().parents[2]
    # 源码树为 backend/agent-service/app，Agent 镜像将 agent-service 复制到
    # /app，因此对应 /app/app。候选路径必须显式且有限，禁止向上递归搜索。
    candidates = (
        backend_root / "agent-service" / "app" / "tools" / "acli" / "catalog" / "acli_command_catalog.json",
        backend_root / "app" / "tools" / "acli" / "catalog" / "acli_command_catalog.json",
    )
    data: Any = None
    for catalog_path in candidates:
        try:
            data = json.loads(catalog_path.read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            continue
    if data is None:
        return ()
    commands = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(commands, list):
        return ()
    return tuple(item for item in commands if isinstance(item, dict) and isinstance(item.get("command"), str))


@lru_cache(maxsize=1)
def load_resolution_catalog() -> dict[str, Any]:
    """读取 Runtime 声明式别名和命令参数需求 Catalog。"""

    path = Path(__file__).with_name("catalogs") / "resolution_catalog.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": 1, "catalog_version": "missing"}
    return data if isinstance(data, dict) else {"schema_version": 1, "catalog_version": "invalid"}


def catalog_command_paths() -> frozenset[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for item in load_acli_catalog():
        tokens = tuple(str(item["command"]).split())
        if tokens and tokens[0] == "acli":
            paths.add(tokens[1:])
    return frozenset(paths)


def command_path_known(tokens: list[str]) -> bool:
    """Catalog 采用前缀匹配：命令后的资源名和参数不影响命令路径判断。"""

    path = tuple(tokens[1:] if tokens and tokens[0] == "acli" else tokens)
    return any(len(path) >= len(candidate) and path[: len(candidate)] == candidate for candidate in catalog_command_paths())


def resolution_catalog_version() -> str:
    return str(load_resolution_catalog().get("catalog_version") or "unknown")


def log_aliases() -> dict[str, str]:
    aliases = load_resolution_catalog().get("log_aliases")
    return {str(key): str(value) for key, value in aliases.items()} if isinstance(aliases, dict) else {}


def domain_command_requirements(domain: str, command_tokens: list[str]) -> list[str]:
    rows = load_resolution_catalog().get("domain_command_requirements")
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict) or row.get("domain") != domain:
            continue
        path = row.get("path")
        if isinstance(path, list) and command_tokens[: len(path)] == path:
            required = row.get("required_options")
            return [str(item) for item in required] if isinstance(required, list) else []
    return []


def normalize_qkv_keyword(value: str | None) -> str:
    """Normalize only presentation-level differences; never infer business semantics."""

    text = str(value or "").replace("\u3000", " ").strip()
    text = re.sub(r"\s+", " ", text)
    text = text.replace("（", "(").replace("）", ")")
    return text.casefold()


def qkv_actions(query: str | None = None) -> tuple[dict[str, Any], ...]:
    rows = load_resolution_catalog().get("qkv_actions")
    if not isinstance(rows, list):
        return ()
    result = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("action_id"):
            continue
        if query and str(row.get("query") or "") != str(query):
            continue
        result.append(row)
    return tuple(result)


def resolve_qkv_action(keyword: str | None, query: str) -> dict[str, Any] | None:
    """Resolve a keyword against reviewed canonical words and aliases only."""

    normalized = normalize_qkv_keyword(keyword)
    if not normalized:
        return None
    for row in qkv_actions(query):
        words = [*row.get("canonical_keywords", []), *row.get("aliases", [])]
        if any(normalize_qkv_keyword(word) == normalized for word in words):
            canonical = list(row.get("canonical_keywords", []))
            aliases = list(row.get("aliases", []))
            return {
                "action_id": str(row["action_id"]),
                "canonical_keyword": str(canonical[0]) if canonical else str(keyword),
                "keyword_candidates": list(dict.fromkeys([*(canonical or [str(keyword)]), *aliases])),
                "negative_aliases": [str(item) for item in row.get("negative_aliases", [])],
                "matched_as": "canonical" if normalized in {normalize_qkv_keyword(item) for item in canonical} else "alias",
            }
    return None
