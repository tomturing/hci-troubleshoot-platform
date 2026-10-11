"""分类 code 命名文法（解 A「角色无关统一 ID」）契约回归测试。

守护要点：
- 叶子与分组共享同一「普通节点」文法 {域}-{纯数字序号}；域根为 {域}-L1。
- 名称形式（{域}-L级-中文）与层级形式（{域}-L级-序号）一律判为非法并给出改进建议。
- 序号分配器按域连续、零撞号。
- 扁平化检验命中「单子节点分组」。
"""

from shared.utils.category_code import (
    code_kind,
    find_flatten_candidates,
    find_nonconforming_codes,
    is_domain_root_code,
    is_node_code,
    is_valid_category_code,
    next_node_code,
    validate_code,
)


def test_node_and_domain_root_are_valid():
    # baseline 叶子与 manual 迁移目标叶子
    assert code_kind("虚拟机-017") == "node"
    assert code_kind("存储-047") == "node"
    # 域根
    assert code_kind("虚拟机-L1") == "domain_root"
    assert is_domain_root_code("存储-L1") is True
    assert is_valid_category_code("网络-003") is True


def test_name_form_and_level_form_are_invalid():
    # 名称形式分组（把中文名编进 ID）
    assert code_kind("虚拟机-L2-虚拟机集群内或跨集群迁移失败") == "invalid"
    assert code_kind("硬件-L2-CPU") == "invalid"
    # 层级形式（把可变角色编进 ID）
    assert code_kind("存储-L3-001") == "invalid"
    assert code_kind("虚拟机-L2-002") == "invalid"
    assert is_node_code("存储-L3-001") is False


def test_is_node_code_not_fooled_by_embedded_level():
    # {域}-L级-序号 结尾也是数字，必须被域前缀不含连字符的规则挡下
    assert is_node_code("硬件-L3-002") is False


def test_validate_code_gives_actionable_suggestion():
    msg = validate_code("虚拟机-L2-跨集群迁移失败")
    assert msg is not None
    assert "人类名称" in msg and "纯序号" in msg

    msg2 = validate_code("存储-L3-001")
    assert msg2 is not None
    assert "层级" in msg2

    assert validate_code("虚拟机-017") is None
    assert validate_code("虚拟机-L1") is None


def test_next_node_code_continues_domain_max():
    existing = ["虚拟机-001", "虚拟机-054", "存储-046", "虚拟机-L1"]
    assert next_node_code("虚拟机", existing) == "虚拟机-055"
    # 域根 L1 不参与序号空间
    assert next_node_code("平台", ["平台-L1"]) == "平台-001"


def test_find_nonconforming_flags_only_bad_leaves_and_skips_roots():
    items = [
        {"code": "虚拟机-017", "name": "ok"},
        {"code": "存储-L3-001", "name": "level form"},
        {"code": "网络-L1", "name": "domain root"},
    ]
    bad = find_nonconforming_codes(items)
    assert "存储-L3-001" in bad
    assert "网络-L1" not in bad  # 域根合法，不得计入漂移
    assert "虚拟机-017" not in bad


def test_flatten_detects_single_child_group():
    nodes = [
        {"code": "虚拟机-L1", "parent_code": None},
        {"code": "虚拟机-060", "parent_code": "虚拟机-L1"},  # 单子分组，应扁平化
        {"code": "虚拟机-061", "parent_code": "虚拟机-060"},  # 唯一子节点
        {"code": "虚拟机-070", "parent_code": "虚拟机-L1"},
        {"code": "虚拟机-071", "parent_code": "虚拟机-070"},
        {"code": "虚拟机-072", "parent_code": "虚拟机-070"},  # 双子分组，可保留
    ]
    result = find_flatten_candidates(nodes)
    single = {r["code"] for r in result}
    assert single == {"虚拟机-060"}
    assert next(r for r in result if r["code"] == "虚拟机-060")["only_child_code"] == "虚拟机-061"
