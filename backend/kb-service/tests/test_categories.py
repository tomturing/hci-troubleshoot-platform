"""
KB Service — 分类基线管理及可视化编辑器单元测试
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.kb_category import KbCategory
from app.repositories.category_repo import CategoryRepository
from app.routes.categories import router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared.utils.category_code import is_valid_category_code

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


def mock_category(
    id_val: int, code: str, name: str, level: int, parent_id: int | None, domain: str, path_labels: list[str]
) -> KbCategory:
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

    # 模拟 SELECT KbCategory.code 加载现有 code（供解 A 序号分配避让）
    mock_codes_res = MagicMock()
    mock_codes_res.fetchall.return_value = [("虚拟机-L1",)]

    # 配置 mock_session 执行返回值流：先查父、再查现有 code
    mock_session.execute.side_effect = [mock_parent_res, mock_codes_res]

    repo = CategoryRepository(mock_db)
    category = await repo.create(
        name="FC存储",
        domain="虚拟机",
        parent_id=10,
        code=None,  # 自动生成
        keywords=["FC", "存储"],
    )

    # 验证自动推算的字段
    assert category.name == "FC存储"
    assert category.level == 2
    assert category.domain == "虚拟机"
    assert category.path_labels == ["虚拟机", "FC存储"]
    assert category.parent_id == 10
    # 解 A：自动按域分配 {域}-{序号}（本域暂无节点序号，取 001；域根 -L1 不占序号空间）
    assert category.code == "虚拟机-001"  # 自动生成

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
    updated_cat = await repo.update_parent_recursive(code="存储-001", new_parent_id=30)

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
        await repo.update_parent_recursive(code="存储-001", new_parent_id=21)


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


@pytest.mark.anyio
async def test_get_all_leaf_only_filter():
    """测试 leaf_only 参数过滤，仅返回叶子节点（无子分类的节点）"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    # 构造测试数据：
    # - 虚拟机-L1 (id=1, parent_id=None) → 中间节点，有子分类
    # - 虚拟机-001 (id=2, parent_id=1) → 叶子节点
    # - 虚拟机-002 (id=3, parent_id=1) → 叶子节点
    # - 存储-L1 (id=4, parent_id=None) → 中间节点，有子分类
    # - 存储-001 (id=5, parent_id=4) → 叶子节点
    parent_cat = mock_category(1, "虚拟机-L1", "虚拟机", 1, None, "虚拟机", ["虚拟机"])
    leaf_cat1 = mock_category(2, "虚拟机-001", "虚拟机开机失败", 2, 1, "虚拟机", ["虚拟机", "虚拟机开机失败"])
    leaf_cat2 = mock_category(3, "虚拟机-002", "虚拟机无法迁移", 2, 1, "虚拟机", ["虚拟机", "虚拟机无法迁移"])
    parent_cat2 = mock_category(4, "存储-L1", "存储", 1, None, "存储", ["存储"])
    leaf_cat3 = mock_category(5, "存储-001", "存储添加失败", 2, 4, "存储", ["存储", "存储添加失败"])

    all_cats = [parent_cat, leaf_cat1, leaf_cat2, parent_cat2, leaf_cat3]
    leaf_cats = [leaf_cat1, leaf_cat2, leaf_cat3]  # 仅叶子节点

    # Mock 返回 RowProxy mappings（模拟 text() SQL 查询结果）
    def make_row_proxy(cat):
        """将 KbCategory 对象转换为 row mapping 格式"""
        return {
            "id": cat.id,
            "code": cat.code,
            "name": cat.name,
            "domain": cat.domain,
            "level": cat.level,
            "parent_id": cat.parent_id,
            "path_labels": cat.path_labels,
            "hit_count": cat.hit_count,
            "is_active": cat.is_active,
            "keywords": [],
            "source": "manual",
            "version": "1.0",
            "created_at": None,
            "published_kbd_count": 0,
            "published_sop_count": 0,
        }

    # Mock 全量查询结果（leaf_only=False）
    mock_all_result = MagicMock()
    mock_all_result.mappings.return_value.all.return_value = [make_row_proxy(c) for c in all_cats]

    # Mock 叶子查询结果（leaf_only=True）
    mock_leaf_result = MagicMock()
    mock_leaf_result.mappings.return_value.all.return_value = [make_row_proxy(c) for c in leaf_cats]

    # 根据查询是否包含 NOT EXISTS 返回不同结果
    async def execute_side_effect(query, *args):
        query_str = str(query) if hasattr(query, "text") else ""
        # leaf_only=True 时查询包含 "NOT EXISTS"
        if "NOT EXISTS" in query_str:
            return mock_leaf_result
        return mock_all_result

    mock_session.execute = execute_side_effect

    repo = CategoryRepository(mock_db)

    # 测试 leaf_only=False（返回全部）
    all_result = await repo.get_all(leaf_only=False)
    assert len(all_result) == 5
    assert any(c.code == "虚拟机-L1" for c in all_result)  # 包含中间节点

    # 测试 leaf_only=True（仅返回叶子）
    leaf_result = await repo.get_all(leaf_only=True)
    assert len(leaf_result) == 3
    assert all("-L" not in c.code for c in leaf_result)  # 无中间节点编码
    assert all(c.code in ("虚拟机-001", "虚拟机-002", "存储-001") for c in leaf_result)


# ─── 基线解析编码契约测试（Q2026101026818 复盘：防隐形分类）──────────────────


def test_parse_baseline_yaml_rejects_nonconforming_leaf_id():
    """叶子 id 不符合 '-<纯数字>' 契约时 fail-fast（导入终止，防隐形分类）。"""
    records, errors = CategoryRepository._parse_baseline_yaml(
        [
            {"id": "虚拟机-ABC", "domain": "虚拟机", "label": "坏编码分类", "path": ["虚拟机", "坏编码分类"]},
            {"id": "虚拟机-001", "domain": "虚拟机", "label": "正常分类", "path": ["虚拟机", "正常分类"]},
        ]
    )

    assert errors, "坏 id 必须产生解析错误"
    assert any("虚拟机-ABC" in err for err in errors)
    assert all(record["code"] != "虚拟机-ABC" for record in records)
    assert any(record["code"] == "虚拟机-001" for record in records)


def test_parse_baseline_yaml_rejects_duplicate_leaf_id():
    """重复叶子 id 必须产生解析错误，同一编码只能对应一个叶节点。"""
    _records, errors = CategoryRepository._parse_baseline_yaml(
        [
            {"id": "虚拟机-001", "domain": "虚拟机", "label": "分类A", "path": ["虚拟机", "分类A"]},
            {"id": "虚拟机-001", "domain": "虚拟机", "label": "分类B", "path": ["虚拟机", "分类B"]},
        ]
    )

    assert any("重复" in err for err in errors)


def test_parse_baseline_yaml_intermediate_generates_node_form_code():
    """解 A：中间层分组由 path 派生，其 code 统一为 {域}-{序号}，绝不出现名称/层级形式。"""
    # 同一分组下两个叶子（child_count=2）→ 不触发扁平化，分组合法保留
    records, errors = CategoryRepository._parse_baseline_yaml(
        [
            {
                "id": "虚拟机-017",
                "domain": "虚拟机",
                "label": "跨集群热迁移失败",
                "path": ["虚拟机", "迁移类", "跨集群热迁移失败"],
            },
            {
                "id": "虚拟机-018",
                "domain": "虚拟机",
                "label": "冷迁移失败",
                "path": ["虚拟机", "迁移类", "冷迁移失败"],
            },
        ]
    )

    assert errors == []
    codes = {record["code"] for record in records}
    # 域根 + 1 个中间层分组 + 2 个叶子 = 4 个节点
    assert "虚拟机-L1" in codes  # 域根特殊形态
    assert "虚拟机-017" in codes and "虚拟机-018" in codes  # 叶子
    assert len(codes) == 4
    # 所有产物均为解 A 合法形态；绝不出现旧名称形式（-L2-中文）或层级形式（-L3-序号）
    assert all(is_valid_category_code(code) for code in codes)
    assert not any("-L2-" in code or "-L3-" in code for code in codes)


def test_parse_baseline_yaml_flatten_blocks_single_child_group():
    """需求2 扁平化检验：一个分组下只有 1 个子节点时拦截报错。"""
    _records, errors = CategoryRepository._parse_baseline_yaml(
        [
            {
                "id": "虚拟机-017",
                "domain": "虚拟机",
                "label": "跨集群热迁移失败",
                "path": ["虚拟机", "唯一单子分组", "跨集群热迁移失败"],
            }
        ]
    )
    assert any("扁平化" in err or "只有 1 个子节点" in err for err in errors)


# ─── 手动入口护栏测试（#1129 后续：堵住漂移入口）──────────────────────────────


@pytest.mark.anyio
async def test_create_rejects_nonconforming_code():
    """手动传入不合规 code（名称/层级/无序号形式）必须被硬拒绝，从入口堵住漂移。"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    # parent_id=None 走 level=1 分支；仅一次 SELECT KbCategory.code（返回空）
    mock_codes_res = MagicMock()
    mock_codes_res.fetchall.return_value = []
    mock_session.execute.side_effect = [mock_codes_res]

    repo = CategoryRepository(mock_db)
    with pytest.raises(ValueError, match="不符合命名规范"):
        await repo.create(
            name="坏编码分类",
            domain="虚拟机",
            parent_id=None,
            code="坏编码分类",  # 不合规：无『-数字』结尾
            keywords=[],
        )
    # 尚未触达冲突/落库，不应写入
    mock_session.add.assert_not_called()


@pytest.mark.anyio
async def test_create_accepts_valid_leaf_code():
    """手动传入合规叶子 code（『前缀-纯数字』结尾）应正常创建。"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    mock_codes_res = MagicMock()
    mock_codes_res.fetchall.return_value = []  # 099 尚未占用
    mock_session.execute.side_effect = [mock_codes_res]

    repo = CategoryRepository(mock_db)
    category = await repo.create(
        name="FC存储",
        domain="虚拟机",
        parent_id=None,
        code="虚拟机-099",  # 合规叶子形态
        keywords=[],
    )
    assert category.code == "虚拟机-099"
    assert category.level == 1
    mock_session.add.assert_called_once()


@pytest.mark.anyio
async def test_delete_emits_parent_degeneration_warning():
    """删除叶子致原父退化为『无子的中间层』时，记录退化告警（不阻断删除）。"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    target = mock_category(20, "虚拟机-005", "虚拟机开机失败", 2, 10, "虚拟机", ["虚拟机", "虚拟机开机失败"])
    old_parent = mock_category(10, "虚拟机-L2-分组", "分组", 1, None, "虚拟机", ["虚拟机", "分组"])

    res_target = MagicMock()
    res_target.scalar_one_or_none.return_value = target
    res_children = MagicMock()
    res_children.first.return_value = None  # 无子，可删
    res_sop = MagicMock()
    res_sop.first.return_value = None
    res_kbd = MagicMock()
    res_kbd.first.return_value = None
    res_parent_obj = MagicMock()
    res_parent_obj.scalar_one_or_none.return_value = old_parent
    res_has_child = MagicMock()
    res_has_child.first.return_value = None  # 父无子 → 退化

    mock_session.execute.side_effect = [
        res_target,
        res_children,
        res_sop,
        res_kbd,
        res_parent_obj,
        res_has_child,
    ]

    with patch("app.repositories.category_repo.logger") as mock_logger:
        repo = CategoryRepository(mock_db)
        result = await repo.delete(code="虚拟机-005")
        assert result is True
        warn_calls = [
            c for c in mock_logger.warning.call_args_list if c.kwargs.get("event") == "repo_category_parent_degenerated"
        ]
        assert warn_calls, "退化告警事件未触发"


@pytest.mark.anyio
async def test_update_parent_recursive_emits_parent_degeneration_warning():
    """拖拽使原父退化为『无子的中间层』时，记录退化告警（不阻断移动）。"""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    target = mock_category(20, "存储-001", "FC存储", 2, 10, "存储", ["存储", "FC存储"])
    new_parent = mock_category(30, "虚拟机-L1", "虚拟机", 1, None, "虚拟机", ["虚拟机"])
    old_parent = mock_category(10, "存储-L2-分组", "分组", 1, None, "存储", ["存储", "分组"])

    res_target = MagicMock()
    res_target.scalar_one_or_none.return_value = target
    res_cycle = MagicMock()
    res_cycle.scalar_one_or_none.return_value = None  # 新父为 L1，无环
    res_parent = MagicMock()
    res_parent.scalar_one_or_none.return_value = new_parent
    res_all_cats = MagicMock()
    res_all_cats.scalars.return_value.all.return_value = [target, new_parent, old_parent]

    mock_session.execute.side_effect = [res_target, res_cycle, res_parent, res_all_cats]

    with patch("app.repositories.category_repo.logger") as mock_logger:
        repo = CategoryRepository(mock_db)
        updated = await repo.update_parent_recursive(code="存储-001", new_parent_id=30)
        assert updated.parent_id == 30
        warn_calls = [
            c for c in mock_logger.warning.call_args_list if c.kwargs.get("event") == "repo_category_parent_degenerated"
        ]
        assert warn_calls, "退化告警事件未触发"


# ─── 导出/筛选分组信息完整性回归（本次需求：三处均清晰含分组）─────────────


def test_export_yaml_emits_only_leaves_with_grouping_path():
    """导出仅落叶子，分组层级由每条叶子的 path 数组完整承载；且分组不入列防往返污染。"""
    import yaml as _yaml
    from app.routes.categories import _generate_export_yaml

    parent = mock_category(1, "虚拟机-L1", "虚拟机", 1, None, "虚拟机", ["虚拟机"])
    grp = mock_category(2, "虚拟机-001", "迁移类", 2, 1, "虚拟机", ["虚拟机", "迁移类"])
    leaf = mock_category(3, "虚拟机-002", "跨集群迁移失败", 3, 2, "虚拟机", ["虚拟机", "迁移类", "跨集群迁移失败"])

    out = _generate_export_yaml([parent, grp, leaf])
    data = _yaml.safe_load(out)
    ids = {item["id"] for item in data["categories"]}
    # 域根与分组均不入列（否则导入侧会将其当叶子重建、打平污染）
    assert ids == {"虚拟机-002"}
    assert data["total"] == 1
    # 分组信息通过 path 清晰可还原
    assert data["categories"][0]["path"] == ["虚拟机", "迁移类", "跨集群迁移失败"]


@pytest.mark.anyio
async def test_resolve_category_subtree_codes_expands_descendants():
    """选中分组时递归展开子树，命中其后代叶子（供级联筛选）。"""
    from app.routes.admin import _resolve_category_subtree_codes

    session = AsyncMock()
    res = MagicMock()
    res.fetchall.return_value = [("虚拟机-001",), ("虚拟机-002",), ("虚拟机-003",)]
    session.execute.return_value = res

    codes = await _resolve_category_subtree_codes(session, "虚拟机-001")
    assert set(codes) == {"虚拟机-001", "虚拟机-002", "虚拟机-003"}


@pytest.mark.anyio
async def test_resolve_category_subtree_codes_falls_back_to_self():
    """查不到时回退为自身 code，保证叶子选择行为不变。"""
    from app.routes.admin import _resolve_category_subtree_codes

    session = AsyncMock()
    res = MagicMock()
    res.fetchall.return_value = []
    session.execute.return_value = res

    codes = await _resolve_category_subtree_codes(session, "虚拟机-099")
    assert codes == ["虚拟机-099"]
