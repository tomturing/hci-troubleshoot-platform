"""跨领域 Catalog 读取和安全命令匹配辅助。

``catalogs/`` 目录下的所有 JSON 文件均由 :class:`_HotCatalog` 统一管理：
每次调用检查文件 mtime，变更后自动重载缓存，**无需重启服务**。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Generic, TypeVar

_CATALOGS_DIR: Path = Path(__file__).with_name("catalogs")

# 唯一权威路径：禁止在其他位置维护副本。
ACLI_CATALOG_PATH: Path = _CATALOGS_DIR / "acli_command_catalog.json"
RESOLUTION_CATALOG_PATH: Path = _CATALOGS_DIR / "resolution_catalog.json"

_T = TypeVar("_T")


class _HotCatalog(Generic[_T]):  # noqa: UP046
    """对单个 JSON 文件的 mtime 感知热加载缓存（进程级单例，线程安全）。

    Parameters
    ----------
    path:
        要监视的 JSON 文件路径。
    loader:
        接受已解析的 ``Any`` 数据，返回目标类型 ``_T`` 的纯函数。
        解析失败或数据类型不符时应返回合理的默认值。
    default:
        文件不存在或读取出错时的初始返回值。
    """

    def __init__(self, path: Path, loader: Any, default: _T) -> None:
        self._path = path
        self._loader = loader
        self._default = default
        self._lock = threading.Lock()
        self._cache: _T = default
        self._mtime: float = -1.0

    def load(self) -> _T:
        """返回最新数据；若文件自上次加载后未变更，直接返回内存缓存。"""
        try:
            current_mtime = self._path.stat().st_mtime
        except OSError:
            return self._cache  # 文件不存在时返回上次缓存（冷启动为 default）

        with self._lock:
            if current_mtime == self._mtime:
                return self._cache
            try:
                raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return self._cache  # 读取/解析失败：保留上次成功的缓存
            result = self._loader(raw)
            if result is not None:
                self._cache = result
            self._mtime = current_mtime
            return self._cache


# ── acli_command_catalog.json ──────────────────────────────────────────────────

def _parse_acli_catalog(raw: Any) -> tuple[dict[str, Any], ...] | None:
    commands = raw.get("commands") if isinstance(raw, dict) else None
    if not isinstance(commands, list):
        return None
    return tuple(item for item in commands if isinstance(item, dict) and isinstance(item.get("command"), str))


_acli_hot = _HotCatalog(
    path=ACLI_CATALOG_PATH,
    loader=_parse_acli_catalog,
    default=(),
)


def load_acli_catalog() -> tuple[dict[str, Any], ...]:
    """返回 aCLI 命令快照（热加载，JSON 变更后自动生效）。"""
    return _acli_hot.load()


# ── resolution_catalog.json ───────────────────────────────────────────────────

def _parse_resolution_catalog(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return raw


_resolution_hot = _HotCatalog(
    path=RESOLUTION_CATALOG_PATH,
    loader=_parse_resolution_catalog,
    default={"schema_version": 1, "catalog_version": "missing"},
)


def load_resolution_catalog() -> dict[str, Any]:
    """返回 Runtime 声明式别名和命令参数需求 Catalog（热加载，JSON 变更后自动生效）。"""
    return _resolution_hot.load()




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
