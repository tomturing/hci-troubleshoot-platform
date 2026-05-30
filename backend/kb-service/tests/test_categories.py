"""
KB Service — 分类基线管理及可视化编辑器单元测试
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.models.kb_category import KbCategory
from app.repositories.category_repo import CategoryRepository
from app.routes.categories import router
from fastapi import FastAPI
from fastapi.testclient import TestClient

_VALID_TOKEN = "hci-dev-internal-token"
_AUTH_HEADER = {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def mock_category(id_val: int, code: str, name: str, level: int, parent_id: int | None, domain: str, path_labels: list[str]) -> KbCategory:
    cat = KbCategory()
    cat.id = id_val
    cat.code = code
    cat.name = name
    cat.level = level
    cat.parent_id = parent_id
    cat.domain = domain
    cat.path_labels = path_labels
    cat.is_active = True
    cat.hit_count = 0
    return cat


# ──────────────────────────────────────────────────────────────────────────────
# Repository Unit Tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_category_creation_with_parent():
    """测试带有父节点的子分类创建，自动推算 level/path/domain"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    parent_cat = mock_category(10, "虚拟机-L1", "虚拟机", 1, None, "虚拟机", ["虚拟机"])

    # 模拟执行 SELECT 查询父节点
    mock_parent_res = MagicMock()
    mock_parent_res.scalar_one_or_none.return_value = parent_cat

    # 模拟执行 SELECT 查询候选 code 是否冲突（返回 None 表示无冲突）
    mock_conflict_res = MagicMock()
    mock_conflict_res.scalar_one_or_none.return_value = None

    # 配置 mock_session 执行返回值流
    mock_session.execute.side_effect = [mock_parent_res, mock_conflict_res]

    repo = CategoryRepository(mock_db)
    category = await repo.create(
        name="FC存储",
        domain="虚拟机",
        parent_id=10,
        code=None,  # 自动生成
        keywords=["FC", "存储"]
    )

    # 验证自动推算的字段
    assert category.name == "FC存储"
    assert category.level == 2
    assert category.domain == "虚拟机"
    assert category.path_labels == ["虚拟机", "FC存储"]
    assert category.parent_id == 10
    assert category.code == "虚拟机-L2-001"  # 自动生成

    mock_session.add.assert_called_once()
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_update_parent_recursive_cascade():
    """测试拖拽分类关系，递归级联更新子孙节点的 level/path_labels/domain"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    # 目标被拖拽节点：存储 L2 (id=20, parent_id=10)
    target_cat = mock_category(20, "存储-001", "FC存储", 2, 10, "存储", ["存储", "FC存储"])
    # 新父节点：虚拟机 L1 (id=30)
    new_parent = mock_category(30, "虚拟机-L1", "虚拟机", 1, None, "虚拟机", ["虚拟机"])

    # 子孙节点：FC存储下属的 L3 (id=21, parent_id=20)
    child_cat = mock_category(21, "存储-002", "存储添加失败", 3, 20, "存储", ["存储", "FC存储", "存储添加失败"])

    # Mock DB 查询序列：
    # 1. 查找当前要拖动的节点 (target_cat)
    res_target = MagicMock()
    res_target.scalar_one_or_none.return_value = target_cat

    # 2. 查找新父节点 (new_parent)
    res_parent = MagicMock()
    res_parent.scalar_one_or_none.return_value = new_parent

    # 3. 递归防环查找：一路向上查询 target_cat.parent_id=30 -> None (无环)
    res_cycle_check = MagicMock()
    res_cycle_check.scalar_one_or_none.return_value = None

    # 4. 加载所有分类以在内存中级联递归更新子孙
    res_all_cats = MagicMock()
    res_all_cats.scalars.return_value.all.return_value = [target_cat, child_cat, new_parent]

    mock_session.execute.side_effect = [res_target, res_cycle_check, res_parent, res_all_cats]

    repo = CategoryRepository(mock_db)
    updated_cat = await repo.update_parent_recursive(
        code="存储-001",
        new_parent_id=30
    )

    assert updated_cat.parent_id == 30
    assert updated_cat.level == 2
    assert updated_cat.domain == "虚拟机"
    assert updated_cat.path_labels == ["虚拟机", "FC存储"]

    # 级联更新子孙节点断言
    assert child_cat.level == 3
    assert child_cat.domain == "虚拟机"
    assert child_cat.path_labels == ["虚拟机", "FC存储", "存储添加失败"]

    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_update_parent_circular_dependency_prevention():
    """测试拖拽时防环校验，拖入自身或自身的子孙节点时被阻断"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    # 拖拽的节点 FC存储 (id=20)
    target_cat = mock_category(20, "存储-001", "FC存储", 2, 10, "存储", ["存储", "FC存储"])
    # 试图把 FC存储 拖入它的子孙节点 存储添加失败 (id=21)
    # 这会形成循环：21 -> 20 -> 21
    res_target = MagicMock()
    res_target.scalar_one_or_none.return_value = target_cat

    # 环检测中，查找新父节点 21 的 parent_id 得到 20（触发环错误）
    res_cycle = MagicMock()
    res_cycle.scalar_one_or_none.return_value = 20

    mock_session.execute.side_effect = [res_target, res_cycle]

    repo = CategoryRepository(mock_db)
    with pytest.raises(ValueError, match="无法将节点拖拽到其自身的子分类下"):
        await repo.update_parent_recursive(
            code="存储-001",
            new_parent_id=21
        )


@pytest.mark.anyio
async def test_delete_constraints_active_references_blocked():
    """测试分类删除阻断限制（含有子分类或 SOP/KBD 引用时禁止删除）"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    target_cat = mock_category(20, "存储-001", "FC存储", 2, 10, "存储", ["存储", "FC存储"])

    # 1. 查找当前节点
    res_target = MagicMock()
    res_target.scalar_one_or_none.return_value = target_cat

    # 2. 查询是否有子分类（返回包含一条记录，表示有子分类）
    res_children = MagicMock()
    res_children.first.return_value = (21,)

    mock_session.execute.side_effect = [res_target, res_children]

    repo = CategoryRepository(mock_db)
    with pytest.raises(ValueError, match="无法删除该分类：该分类下包含子分类"):
        await repo.delete(code="存储-001")
