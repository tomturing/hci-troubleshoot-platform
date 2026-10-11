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


def test_export_yaml_emits_full_tree_v2():
    """导出为 v2.0 全树：域根+分组+叶子全部入列，以显式 code+parent 忠实承载结构。"""
    import yaml as _yaml
    from app.routes.categories import _generate_export_yaml

    parent = mock_category(1, "虚拟机-L1", "虚拟机", 1, None, "虚拟机", ["虚拟机"])
    grp = mock_category(2, "虚拟机-001", "迁移类", 2, 1, "虚拟机", ["虚拟机", "迁移类"])
    leaf = mock_category(3, "虚拟机-002", "跨集群迁移失败", 3, 2, "虚拟机", ["虚拟机", "迁移类", "跨集群迁移失败"])

    out = _generate_export_yaml([parent, grp, leaf])
    data = _yaml.safe_load(out)
    assert data["version"] == "2.0"
    assert data["total"] == 3
    by_code = {c["code"]: c for c in data["categories"]}
    # 全树：域根/分组/叶子均在列，parent 为父 code
    assert set(by_code) == {"虚拟机-L1", "虚拟机-001", "虚拟机-002"}
    assert by_code["虚拟机-L1"]["parent"] is None
    assert by_code["虚拟机-001"]["parent"] == "虚拟机-L1"
    assert by_code["虚拟机-002"]["parent"] == "虚拟机-001"
    assert by_code["虚拟机-002"]["path"] == ["虚拟机", "迁移类", "跨集群迁移失败"]


def test_parse_baseline_fulltree_preserves_explicit_codes():
    """v2.0 全树解析：照抄显式 code不重编号；域根 level 强制为 1。"""
    cats = [
        {"code": "虚拟机-L1", "name": "虚拟机", "domain": "虚拟机", "level": 1, "parent": None, "path": ["虚拟机"]},
        {"code": "虚拟机-001", "name": "迁移类", "domain": "虚拟机", "level": 2, "parent": "虚拟机-L1", "path": ["虚拟机", "迁移类"]},
        {"code": "虚拟机-021", "name": "坏道", "domain": "虚拟机", "level": 3, "parent": "虚拟机-001", "path": ["虚拟机", "迁移类", "坏道"]},
    ]
    records, errors = CategoryRepository._parse_baseline_fulltree(cats)
    assert errors == []
    codes = {r["code"] for r in records}
    # 021 原样保留（不被重编号为 002），拖拽重排结果无损回导
    assert codes == {"虚拟机-L1", "虚拟机-001", "虚拟机-021"}
    root = next(r for r in records if r["code"] == "虚拟机-L1")
    assert root["level"] == 1


def test_parse_baseline_fulltree_rejects_bad_inputs():
    """全树解析拒绝：非法 code 形态 / 重复 code / parent 悬空 / 域根 parent 非空。"""
    _r, e1 = CategoryRepository._parse_baseline_fulltree(
        [{"code": "虚拟机-L2-坏道", "name": "x", "domain": "虚拟机", "path": ["虚拟机", "x"]}]
    )
    assert e1
    _r, e2 = CategoryRepository._parse_baseline_fulltree(
        [
            {"code": "虚拟机-001", "name": "a", "domain": "虚拟机", "path": ["虚拟机", "a"]},
            {"code": "虚拟机-001", "name": "b", "domain": "虚拟机", "path": ["虚拟机", "b"]},
        ]
    )
    assert any("重复" in x for x in e2)
    _r, e3 = CategoryRepository._parse_baseline_fulltree(
        [{"code": "虚拟机-001", "name": "a", "domain": "虚拟机", "parent": "虚拟机-999", "path": ["虚拟机", "a"]}]
    )
    assert any("parent" in x for x in e3)
    _r, e4 = CategoryRepository._parse_baseline_fulltree(
        [{"code": "虚拟机-L1", "name": "虚拟机", "domain": "虚拟机", "parent": "虚拟机-001", "path": ["虚拟机"]}]
    )
    assert any("域根" in x for x in e4)


def test_baseline_render_parse_roundtrip_lossless():
    """render → parse 往返无损：code/path 完全保留（基线快照可回导的根基）。"""
    import yaml as _yaml
    from shared.utils.category_baseline import render_baseline_yaml

    nodes = [
        {"code": "虚拟机-L1", "name": "虚拟机", "domain": "虚拟机", "level": 1, "parent": None, "path": ["虚拟机"]},
        {"code": "虚拟机-001", "name": "迁移类", "domain": "虚拟机", "level": 2, "parent": "虚拟机-L1", "path": ["虚拟机", "迁移类"]},
        {"code": "虚拟机-002", "name": "坏道", "domain": "虚拟机", "level": 3, "parent": "虚拟机-001", "path": ["虚拟机", "迁移类", "坏道"]},
        {"code": "硬件-L1", "name": "硬件", "domain": "硬件", "level": 1, "parent": None, "path": ["硬件"]},
    ]
    out = render_baseline_yaml(nodes, generated="2026-10-11")
    data = _yaml.safe_load(out)
    records, errors = CategoryRepository._parse_baseline_fulltree(data["categories"])
    assert errors == []
    src = {n["code"]: n["path"] for n in nodes}
    got = {r["code"]: r["path_labels"] for r in records}
    assert src == got


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


# ─── 拖拽重排 resequence_domain（解A：顺序即序号，拖拽物化为 code）──────────


def _fake_resequence_session(rows: list[dict]):
    """构造一个按 SQL 分发的假 session（resequence 大量原生 SQL/DDL 均走 execute）。

    同时把执行过的 SQL 文本记录到 ``mock_db.executed_sql``，供断言 remap 覆盖列清单。
    """

    executed_sql: list[str] = []

    async def _exec(query, params=None):
        q = str(getattr(query, "text", query))
        executed_sql.append(q)
        if "SELECT id, code, parent_id FROM kb_category WHERE domain" in q:
            res = MagicMock()
            res.mappings.return_value.all.return_value = rows
            return res
        if "pg_get_constraintdef" in q:
            res = MagicMock()
            res.scalar.return_value = None  # 模拟环境无 FK/CHECK（与 staging 一致）
            return res
        return MagicMock()  # 其余 DDL/DML

    mock_db = MagicMock()
    mock_db.executed_sql = executed_sql
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.execute = _exec
    mock_db.async_session_factory.return_value = mock_session
    return mock_db


@pytest.mark.anyio
async def test_resequence_materializes_group_adjacency():
    """拖拽重排：分组随其子树整块前移，新 code 使「组紧跟子」相邻（顺序物化为序号）。"""
    # 顶层子 [A(001),B(002),C(003)]，其中 A 带一个子 004
    rows = [
        {"id": 1, "code": "虚拟机-L1", "parent_id": None},
        {"id": 2, "code": "虚拟机-001", "parent_id": 1},
        {"id": 3, "code": "虚拟机-002", "parent_id": 1},
        {"id": 4, "code": "虚拟机-003", "parent_id": 1},
        {"id": 5, "code": "虚拟机-004", "parent_id": 2},
    ]
    repo = CategoryRepository(_fake_resequence_session(rows))
    # 顶层新序：C 最先，其后 A(连子)，最后 B
    result = await repo.resequence_domain(
        domain="虚拟机",
        parent_id=1,
        sibling_order=["虚拟机-003", "虚拟机-001", "虚拟机-002"],
    )
    got = {m["old"]: m["new"] for m in result["mapping"]}
    assert result["changed"] == 4
    assert got == {
        "虚拟机-003": "虚拟机-001",  # C 前移为首
        "虚拟机-001": "虚拟机-002",  # A 紧跟其后
        "虚拟机-004": "虚拟机-003",  # A 的子随 A 相邻
        "虚拟机-002": "虚拟机-004",  # B 垫后
    }


@pytest.mark.anyio
async def test_resequence_noop_when_already_canonical():
    """树已合规（组紧跟子、序号连续）且无 sibling_order 时，重排为空操作，不落任何改写。"""
    rows = [
        {"id": 1, "code": "虚拟机-L1", "parent_id": None},
        {"id": 2, "code": "虚拟机-001", "parent_id": 1},
        {"id": 3, "code": "虚拟机-002", "parent_id": 2},  # 001 的子，紧邻
        {"id": 4, "code": "虚拟机-003", "parent_id": 1},
    ]
    repo = CategoryRepository(_fake_resequence_session(rows))
    result = await repo.resequence_domain(domain="虚拟机", parent_id=None, sibling_order=None)
    assert result == {"success": True, "domain": "虚拟机", "changed": 0, "mapping": [], "trace_id": result["trace_id"]}


@pytest.mark.anyio
async def test_resequence_rejects_sibling_order_mismatch():
    """sibling_order 与目标父实际子节点集合不一致时 fail-fast（防伪造/漏项）。"""
    rows = [
        {"id": 1, "code": "虚拟机-L1", "parent_id": None},
        {"id": 2, "code": "虚拟机-001", "parent_id": 1},
        {"id": 3, "code": "虚拟机-002", "parent_id": 1},
        {"id": 4, "code": "虚拟机-003", "parent_id": 1},
    ]
    repo = CategoryRepository(_fake_resequence_session(rows))
    with pytest.raises(ValueError, match="兄弟顺序与目标父实际子节点不一致"):
        await repo.resequence_domain(
            domain="虚拟机",
            parent_id=1,
            sibling_order=["虚拟机-001", "虚拟机-002"],  # 缺 003
        )


@pytest.mark.anyio
async def test_resequence_remaps_all_code_referencing_columns():
    """重排发生时，remap 必须覆盖全库按 code 引用分类的列（含采集映射 category_scope
    与诊断快照列），且悬空置 NULL 仅限有 FK 的 kbd/sop/conversation 列（不触碰含通配
    ``*`` 的 category_scope、也不破坏诊断历史快照）。"""
    rows = [
        {"id": 1, "code": "虚拟机-L1", "parent_id": None},
        {"id": 2, "code": "虚拟机-001", "parent_id": 1},
        {"id": 3, "code": "虚拟机-002", "parent_id": 1},
        {"id": 4, "code": "虚拟机-003", "parent_id": 1},
        {"id": 5, "code": "虚拟机-004", "parent_id": 2},
    ]
    mock_db = _fake_resequence_session(rows)
    repo = CategoryRepository(mock_db)
    result = await repo.resequence_domain(
        domain="虚拟机",
        parent_id=1,
        sibling_order=["虚拟机-003", "虚拟机-001", "虚拟机-002"],
    )
    assert result["changed"] > 0
    joined = "\n".join(mock_db.executed_sql)
    # 1) 所有按 code 引用的列均需产生 remap UPDATE
    for tbl, col in (
        ("kbd_entry", "category_id"),
        ("kbd_entry", "ai_category_id"),
        ("sop_document", "category_id"),
        ("conversation", "category_id"),
        ("offline_signal_collector_mapping", "category_scope"),
        ("diagnosis_candidate", "category_id"),
        ("diagnosis_run", "resolved_category"),
        ("diagnosis_run", "selected_category"),
        ("diagnosis_session", "resolved_category"),
        ("diagnosis_session", "selected_category"),
    ):
        assert f"UPDATE {tbl} x SET {col} = m.new_code FROM renum_map m WHERE x.{col} = m.old_code" in joined, (
            f"缺失 remap: {tbl}.{col}"
        )
    # 2) 悬空置 NULL 不得越界：category_scope / 诊断列不能被动
    assert "category_scope = NULL" not in joined
    assert "resolved_category = NULL" not in joined
    assert "selected_category = NULL" not in joined
    # 3) 有 FK 的 kbd/sop 悬空清理必须保留（重加 FK 前自校验）
    assert "UPDATE kbd_entry k SET category_id = NULL" in joined


# ─── 拖拽重排单事务原子性 move_and_resequence（消除两步分提交的部分成功态）──


class _RecordingSession:
    """一个可记录的假 session（异步上下文），只统计 commit/flush/refresh 调用次数，
    用于断言 move_and_resequence 的事务边界（唯一提交 / 失败不提交）。"""

    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0
        self.refreshes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.commits += 1

    async def flush(self):
        self.flushes += 1

    async def refresh(self, obj):
        self.refreshes += 1


@pytest.mark.anyio
async def test_move_and_resequence_rolls_back_when_resequence_fails():
    """重排校验失败时，共享事务**从未 commit**——证明换父不会单独落库，整体回滚。"""
    cat = mock_category(20, "虚拟机-001", "FC存储", 2, 10, "虚拟机", ["虚拟机", "FC存储"])
    session = _RecordingSession()
    db = MagicMock()
    db.async_session_factory.return_value = session
    repo = CategoryRepository(db)

    with (
        patch.object(CategoryRepository, "_do_update_parent", AsyncMock(return_value=cat)),
        patch.object(
            CategoryRepository,
            "resequence_domain",
            AsyncMock(side_effect=ValueError("兄弟顺序与目标父实际子节点不一致")),
        ),
    ):
        with pytest.raises(ValueError, match="兄弟顺序与目标父实际子节点不一致"):
            await repo.move_and_resequence(code="虚拟机-001", new_parent_id=10, sibling_order=["虚拟机-001"])

    assert session.commits == 0  # 关键：失败即整体回滚，无部分提交


@pytest.mark.anyio
async def test_move_and_resequence_commits_once_on_shared_session():
    """成功时：唯一一次 commit；且 resequence_domain 复用与换父**同一个 session**（ext_session）。"""
    cat = mock_category(20, "虚拟机-001", "FC存储", 2, 10, "虚拟机", ["虚拟机", "FC存储"])
    reseq_ret = {"success": True, "domain": "虚拟机", "changed": 2, "mapping": [], "trace_id": "t"}
    session = _RecordingSession()
    db = MagicMock()
    db.async_session_factory.return_value = session
    repo = CategoryRepository(db)

    with (
        patch.object(CategoryRepository, "_do_update_parent", AsyncMock(return_value=cat)),
        patch.object(CategoryRepository, "resequence_domain", AsyncMock(return_value=reseq_ret)) as mock_reseq,
    ):
        category, reseq = await repo.move_and_resequence(
            code="虚拟机-001", new_parent_id=10, sibling_order=["虚拟机-001", "虚拟机-002"]
        )

    assert session.commits == 1  # 单一提交点：换父+重排原子生效
    assert category is cat
    assert reseq == reseq_ret
    # resequence_domain 以 ext_session 复用同一 session，不会另开事务/自行 commit
    assert mock_reseq.await_args.kwargs["ext_session"] is session
