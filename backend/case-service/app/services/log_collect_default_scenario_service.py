"""默认场景清单覆写服务。

第一性原理：平台只保存「被改过的模块」，没改过的模块继续用随镜像发布的快照。
这样零种子、恢复默认即删行，不会把 11 份大清单复制进数据库；同时任何一次
下发都能明确回答「这份清单来自快照还是管理员覆写」。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.log_collect import LogCollectDefaultScenario
from ..schemas.log_collect_custom_scenario import CustomScenarioItem
from .log_collect_custom_scenario_service import serialize_spc
from .log_collect_spc_catalog import MODULE_LABELS, M_WHITELIST, load_catalog

MAX_MANIFEST_ITEMS = 200


class DefaultScenarioError(ValueError):
    """模块或清单条目不合法。"""


def catalog_items(module: str) -> list[dict[str, Any]]:
    """把快照目录拍平成平台条目结构，note 分组保留便于编辑器展示。"""
    for entry in load_catalog()["modules"]:
        if entry["module"] != module:
            continue
        items: list[dict[str, Any]] = []
        for group in entry["groups"]:
            for raw in group["items"]:
                items.append({
                    "action": raw["action"],
                    "path": raw["path"],
                    "category": "log",
                    "note": group["note"],
                    "filters": raw.get("filters") or [],
                    "date_placeholders": raw.get("date_placeholders") or [],
                    "docker": raw.get("docker"),
                    "interpreter": raw.get("interpreter"),
                })
        return items
    return []


def normalize_items(items: list[CustomScenarioItem]) -> list[dict[str, Any]]:
    """去重保序并限长；路径合法性已在 schema 层校验。"""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        data = item.model_dump(exclude_none=True)
        if data["path"] in seen:
            continue
        seen.add(data["path"])
        result.append(data)
    if not result:
        raise DefaultScenarioError("清单至少需要一条收集项")
    if len(result) > MAX_MANIFEST_ITEMS:
        raise DefaultScenarioError(f"清单条数不能超过 {MAX_MANIFEST_ITEMS}")
    return result


class LogCollectDefaultScenarioService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _overrides(self) -> dict[str, LogCollectDefaultScenario]:
        result = await self.session.execute(
            select(LogCollectDefaultScenario).where(LogCollectDefaultScenario.enabled.is_(True))
        )
        return {row.module_code: row for row in result.scalars().all()}

    @staticmethod
    def _items(module: str, override: LogCollectDefaultScenario | None) -> tuple[list[dict[str, Any]], bool, int]:
        if override is not None:
            return list(override.items or []), True, int(override.revision)
        return catalog_items(module), False, 0

    async def list_manifests(self, *, full: bool) -> list[dict[str, Any]]:
        """列出所有可被 -m 使用的模块清单；full=True 给管理端完整条目。"""
        overrides = await self._overrides()
        manifests: list[dict[str, Any]] = []
        for entry in load_catalog()["modules"]:
            module = entry["module"]
            if module not in M_WHITELIST:
                continue
            items, customized, revision = self._items(module, overrides.get(module))
            _content, sha, _size = serialize_spc(items)
            row: dict[str, Any] = {
                "module": module,
                "label": MODULE_LABELS.get(module, module),
                "customized": customized,
                "revision": revision,
                "item_count": len(items),
                "sha256": sha,
            }
            if full:
                row["items"] = [
                    CustomScenarioItem.model_validate(item).model_dump(exclude_none=True)
                    for item in items
                ]
            else:
                row["preview"] = [
                    {
                        "path": str(item.get("path", "")),
                        "category": str(item.get("category", "log")),
                        "note": item.get("note"),
                    }
                    for item in items
                ]
            manifests.append(row)
        return manifests

    async def build_spc_files(self, modules: list[str] | None) -> list[dict[str, str]]:
        """任一勾选模块被覆写 → 所有勾选模块都按平台清单下发。

        只下发被改过的模块会让 bridge 把 -m 收窄成该模块，用户勾选的其它场景
        会被静默丢掉；因此要么全部按平台清单走，要么完全不启用（零行为变化）。
        """
        selected: list[str] = []
        for raw in modules or []:
            module = str(raw).strip()
            if module in M_WHITELIST and module not in selected:
                selected.append(module)
        if not selected:
            return []
        overrides = await self._overrides()
        if not any(module in overrides for module in selected):
            return []
        files: list[dict[str, str]] = []
        for module in selected:
            items, _, _ = self._items(module, overrides.get(module))
            if not items:
                continue
            content, sha, _size = serialize_spc(items)
            files.append({"slot": module, "content": content, "sha256": sha})
        return files

    async def save(
        self,
        module: str,
        items: list[CustomScenarioItem],
        actor: str | None,
        trace_id: str | None,
    ) -> dict[str, Any]:
        """保存覆写：revision 递增，历史任务仍可解释当时用的是哪一版。"""
        if module not in M_WHITELIST:
            raise DefaultScenarioError(f"不支持的默认场景：{module}")
        normalized = normalize_items(items)
        content, sha, size = serialize_spc(normalized)
        row = await self.session.get(LogCollectDefaultScenario, module)
        if row is None:
            row = LogCollectDefaultScenario(module_code=module, revision=1)
            self.session.add(row)
        else:
            row.revision = int(row.revision) + 1
        row.items = normalized
        row.item_count = len(normalized)
        row.config_sha256 = sha
        row.config_bytes = size
        row.enabled = True
        row.updated_by = actor
        row.trace_id = trace_id
        await self.session.commit()
        return {
            "module": module,
            "revision": int(row.revision),
            "item_count": len(normalized),
            "sha256": sha,
            "bytes": size,
            "content": content,
        }

    async def reset(self, module: str) -> bool:
        """恢复默认：删掉覆写行，此后该模块重新使用镜像快照。"""
        if module not in M_WHITELIST:
            raise DefaultScenarioError(f"不支持的默认场景：{module}")
        row = await self.session.get(LogCollectDefaultScenario, module)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.commit()
        return True
