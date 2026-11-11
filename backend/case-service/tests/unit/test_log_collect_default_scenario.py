"""默认场景清单覆写服务的单元测试。

重点锁住两个容易静默出错的语义：
1. 未覆写时零行为变化；
2. 任一只模块被覆写时，必须为**所有勾选模块**签发清单，
   否则 bridge 会把 -m 收窄，用户勾选的其它场景被静默丢掉。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from app.schemas.log_collect_custom_scenario import CustomScenarioItem
from app.services.log_collect_custom_scenario_service import serialize_spc
from app.services.log_collect_default_scenario_service import (
    DefaultScenarioError,
    LogCollectDefaultScenarioService,
    catalog_items,
    normalize_items,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """只实现覆写查询所需的 execute；不碰数据库。"""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _FakeResult(self._rows)


def _item(path: str) -> CustomScenarioItem:
    return CustomScenarioItem(action="copy", path=path, category="log")


def _override(module: str, items: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(module_code=module, items=items, revision=2, enabled=True)


def test_catalog_items_keep_action_path_and_note():
    items = catalog_items("vm_start")
    assert items, "平台快照里必须有 vm_start 清单"
    assert all(item.get("action") and item.get("path") for item in items)
    assert any(item.get("note") for item in items), "note 分组不能丢"


def test_normalize_items_dedupes_and_rejects_empty():
    with pytest.raises(DefaultScenarioError):
        normalize_items([])
    normalized = normalize_items([_item("/var/log/a"), _item("/var/log/a"), _item("/var/log/b")])
    assert [item["path"] for item in normalized] == ["/var/log/a", "/var/log/b"]


def test_build_spc_files_no_override_is_noop():
    """没有任何覆写时返回空：默认场景行为必须与改造前完全一致。"""
    service = LogCollectDefaultScenarioService(_FakeSession([]))
    assert asyncio.run(service.build_spc_files(["vm_start", "network"])) == []
    assert asyncio.run(service.build_spc_files([])) == []
    assert asyncio.run(service.build_spc_files(["compute"])) == []


def test_build_spc_files_covers_every_selected_module():
    """只改 vm_start，仍要为 network 也签一份清单，否则 -m 会丢掉 network。"""
    items = [{"action": "copy", "path": "/var/log/vm.log", "category": "log"}]
    service = LogCollectDefaultScenarioService(_FakeSession([_override("vm_start", items)]))
    files = asyncio.run(service.build_spc_files(["vm_start", "network"]))
    assert [entry["slot"] for entry in files] == ["vm_start", "network"]
    assert files[0]["content"]
    assert files[0]["sha256"] == serialize_spc(items)[1]
    assert all(len(entry["sha256"]) == 64 for entry in files)


def test_build_spc_files_ignores_unsupported_modules():
    """compute 只能被 sfd_main -s 使用，不能出现在 -m 或槽位里。"""
    items = [{"action": "copy", "path": "/var/log/vm.log", "category": "log"}]
    service = LogCollectDefaultScenarioService(_FakeSession([_override("vm_start", items)]))
    files = asyncio.run(service.build_spc_files(["vm_start", "compute", "not_a_module"]))
    assert [entry["slot"] for entry in files] == ["vm_start"]
