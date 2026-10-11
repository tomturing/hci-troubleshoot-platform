"""写入门禁单测：KBD/SOP 只能绑定叶子分类（解A：分组节点不可直挂）。

覆盖核心可复用单元 ``is_category_leaf`` —— admin.approve_kbd_entry 与
sop_ingest.ingest_sop_document 均据此拒绝错挂到非叶子分组的绑定。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.repositories.category_repo import is_category_leaf


def _session_for(exists: bool, leaf: bool) -> AsyncMock:
    """构造返回 (cat_exists, is_leaf) 的 mock async session。"""
    session = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = {"cat_exists": exists, "is_leaf": leaf}
    session.execute.return_value = result
    return session


@pytest.mark.anyio
async def test_leaf_code_is_bindable():
    """存在且无子节点 → 可绑定叶子。"""
    assert await is_category_leaf(_session_for(exists=True, leaf=True), "虚拟机-011") is True


@pytest.mark.anyio
async def test_group_code_is_not_bindable():
    """存在但有子节点（分组）→ 拒绝，防止 KBD/SOP 错挂分组（本次治理根因）。"""
    assert await is_category_leaf(_session_for(exists=True, leaf=False), "虚拟机-009") is False


@pytest.mark.anyio
async def test_missing_code_is_not_bindable():
    """分类不存在 → 拒绝（避免悬空绑定脱离检索锚点）。"""
    assert await is_category_leaf(_session_for(exists=False, leaf=True), "虚拟机-999") is False
