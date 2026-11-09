from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# spc 快照随代码发布：跟着镜像走，不依赖运行时可写目录。
SPC_DIR = Path(__file__).resolve().parent.parent / "data" / "spc_catalog"

# 模块名到展示名。compute 不在 main.py 的 const_modules 白名单里，只能被
# sfd_main.py -s 直接使用，但对管理员展示时仍应可见。
MODULE_LABELS = {
    "other": "other（默认）",
    "system": "system（中台）",
    "host_offline": "host_offline（主机离线）",
    "vm_start": "vm_start（虚拟机开机失败）",
    "vm_suspend": "vm_suspend（虚拟机挂起）",
    "network": "network（网络）",
    "storage": "storage（虚拟存储）",
    "slowdisk": "slowdisk（卡慢盘）",
    "performance": "performance（性能）",
    "core": "core（core 收集）",
    "compute": "compute（计算，仅 -s 可用）",
}

# 能被 acli -m 接受的白名单；不在其中的模块只能走 sfd_main。
M_WHITELIST = {
    "other", "system", "slowdisk", "network", "storage",
    "vm_start", "vm_suspend", "host_offline", "performance", "core",
}

COPY_ACTION = "copy"


@dataclass(frozen=True)
class CatalogItem:
    # 一条可直接勾选的收集条目：保留 spc 的 action 与过滤条件，不做简化。

    module: str
    group: str
    action: str
    path: str
    filters: list[dict[str, Any]] = field(default_factory=list)
    date_placeholders: list[dict[str, Any]] = field(default_factory=list)
    docker: str | None = None
    interpreter: str | None = None

    @property
    def has_wildcard(self) -> bool:
        return "*" in self.path

    @property
    def needs_date_expand(self) -> bool:
        return any(str(p.get("placeholder")) in self.path for p in self.date_placeholders)

    @property
    def usable_by_config(self) -> bool:
        # 只放行字面 copy 且无 filters：带 * 或 filters 的条目放进 config 会被静默跳过。
        return self.action == COPY_ACTION and not self.has_wildcard and not self.filters

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "group": self.group,
            "action": self.action,
            "path": self.path,
            "filters": self.filters,
            "date_placeholders": self.date_placeholders,
            "docker": self.docker,
            "interpreter": self.interpreter,
            "has_wildcard": self.has_wildcard,
            "needs_date_expand": self.needs_date_expand,
            "usable_by_config": self.usable_by_config,
        }

def _read_spc(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"spc 必须是数组：{path.name}")
    return [item for item in data if isinstance(item, dict)]


def _parse_module(module: str, path: Path) -> tuple[list[CatalogItem], list[str]]:
    # note 在插件里不参与收集（init_pars 直接 continue），但对管理员是最好的分组依据。
    items: list[CatalogItem] = []
    groups: list[str] = []
    current = "未分组"
    for raw in _read_spc(path):
        note = raw.get("note")
        if note:
            current = str(note).strip() or "未分组"
            if current not in groups:
                groups.append(current)
            continue
        action = str(raw.get("action") or "").strip()
        target = str(raw.get("path") or "").strip()
        if not action or not target:
            continue
        items.append(CatalogItem(
            module=module,
            group=current,
            action=action,
            path=target,
            filters=list(raw.get("filters") or []),
            date_placeholders=list(raw.get("date_placeholders") or []),
            docker=(str(raw["docker"]).strip() if raw.get("docker") else None),
            interpreter=(str(raw["interpreter"]).strip() if raw.get("interpreter") else None),
        ))
    return items, groups


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def _load_cached() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # 快照随镜像发布，运行期不变，缓存安全。
    modules: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in sorted(SPC_DIR.glob("*.json")):
        module = path.stem
        items, groups = _parse_module(module, path)
        modules.append({
            "module": module,
            "label": MODULE_LABELS.get(module, module),
            "m_whitelisted": module in M_WHITELIST,
            "item_count": len(items),
            "config_usable_count": sum(1 for item in items if item.usable_by_config),
            "groups": [
                {"note": note, "items": [item.to_dict() for item in items if item.group == note]}
                for note in sorted(groups)
            ],
        })
        sources.append({"file": path.name, "sha256": _file_sha256(path), "items": len(items)})
    return modules, sources


def load_catalog() -> dict[str, Any]:
    # 管理端目录：模块到小节到条目。
    modules, sources = _load_cached()
    return {
        "modules": modules,
        "sources": sources,
        "total_items": sum(module["item_count"] for module in modules),
    }
