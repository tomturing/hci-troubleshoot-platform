"""分类 code 命名文法契约（跨服务共享，唯一事实源）。

═══════════════════════════════════════════════════════════════════════════
设计基线（2026-10 分类基线叶子治理，解 A「角色无关统一 ID」）
═══════════════════════════════════════════════════════════════════════════

核心原则：**「叶子 / 分组」是结构角色（由是否存在子节点决定，会随增删子节点
而变），绝不能编码进「不可变」的 code。** code 仅是稳定外键标识，被
kbd_entry.category_id / ai_category_id、sop_document.category_id、
conversation.category_id 等下游长期引用；分组关系由 parent_id / level /
path_labels / name 维护，与 code 文法正交。

因此全库只承认两种合法 code 文法：

1. 域根（L1 技术域）：``{域}-L1``      —— 每域唯一，parent_id 恒为 NULL，
   永不为叶子，属结构特殊节点（编码 L1 不算编码「可变角色」），予以保留。
2. 其余所有节点（分组与叶子一视同仁）：``{域}-{纯数字序号}``
   —— 例：``虚拟机-017``、``存储-047``。每域一个共享序号空间；
   叶子 / 分组的区分完全交给 kb-service 的 ``leaf_only``（NOT EXISTS）结构判定。

**废弃**旧「分组 ``{域}-L{级}-名称`` / ``{域}-L{级}-{序号}``」文法：
- ``{域}-L{级}-名称``（如 ``虚拟机-L2-虚拟机集群内或跨集群迁移失败``）把人类
  名称编码进不可变 ID，改名即变 code，违背不可变铁律 → 必须迁移。
- ``{域}-L{级}-{序号}``（如 ``存储-L3-001``）把可变层级角色编码进 ID，
  一旦节点角色随结构变化即失配（历史上 ``虚拟机-047``「叶子形 code 却是分组」
  即此类漂移）→ 必须迁移为 ``{域}-{序号}``。

叶子资格的**唯一权威**是结构性判定 ``NOT EXISTS (子节点)``；本模块的正则仅作
写入 / 导入 / 巡检时的**形态合规门禁与漂移探针**，不作为「谁是叶子」的判据。
"""

from __future__ import annotations

import re
from typing import Any

# 域前缀：中文或字母开头，可含中文/字母/数字；不含连字符（连字符是结构分隔符）。
_PREFIX = r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]*"

# 合法「普通节点」code：{域}-{纯数字序号}
NODE_CODE_RE = re.compile(rf"^{_PREFIX}-\d+$")
# 合法「域根」code：{域}-L1
DOMAIN_ROOT_CODE_RE = re.compile(rf"^{_PREFIX}-L1$")

# ── 向后兼容别名 ───────────────────────────────────────────────────────────
# 历史代码以 LEAF_CODE_RE 命名；解 A 下叶子与分组共享同一「普通节点」文法，
# 故 LEAF_CODE_RE == NODE_CODE_RE（叶子形态即普通节点形态）。
LEAF_CODE_RE = NODE_CODE_RE
# 序号位数（零填充宽度）；历史数据为 3 位（如 017）。
SEQ_PAD_WIDTH = 3


def is_domain_root_code(code: object) -> bool:
    """是否为合法的域根（L1 技术域）code。"""
    return isinstance(code, str) and DOMAIN_ROOT_CODE_RE.match(code) is not None


def is_node_code(code: object) -> bool:
    """是否为合法的「普通节点」code（{域}-{纯数字序号}）。"""
    return isinstance(code, str) and NODE_CODE_RE.match(code) is not None


def is_valid_category_code(code: object) -> bool:
    """是否为合法 code（域根或普通节点二选一）。"""
    return is_domain_root_code(code) or is_node_code(code)


def is_leaf_code(code: object) -> bool:
    """判断 code 是否符合「普通节点」形态。

    注意：解 A 下「普通节点」形态同时覆盖叶子与分组，**不能**据此判定谁是叶子；
    叶子资格必须由结构判定（NOT EXISTS 子节点）。本函数保留仅作形态合规探针
    与历史调用方兼容。
    """
    return is_node_code(code)


def split_domain(code: str) -> str:
    """取 code 的域前缀（首个连字符之前的部分）。"""
    return code.split("-", 1)[0] if isinstance(code, str) and "-" in code else ""


def code_kind(code: str) -> str:
    """返回 code 归类：domain_root / node / invalid。"""
    if is_domain_root_code(code):
        return "domain_root"
    if is_node_code(code):
        return "node"
    return "invalid"


def next_node_code(domain: str, existing_codes: Any) -> str:
    """为指定域分配下一个「普通节点」code：该域 {域}-{序号} 的最大序号 +1。

    Args:
        domain: 域前缀（如 虚拟机）
        existing_codes: 可迭代的现有 code 集合（含全部来源，用于探测最大序号）
    """
    max_seq = 0
    for code in existing_codes:
        if not isinstance(code, str):
            continue
        m = NODE_CODE_RE.match(code)
        if m and split_domain(code) == domain:
            seq = int(code.rsplit("-", 1)[1])
            if seq > max_seq:
                max_seq = seq
    return f"{domain}-{max_seq + 1:0{SEQ_PAD_WIDTH}d}"


def validate_code(code: object) -> str | None:
    """校验单个 code，返回中文错误说明；合规返回 None。

    给出「违规点 + 规范解释 + 改进建议」，供导入 / 创建入口友好拦截。
    """
    if not isinstance(code, str) or not code:
        return "code 不能为空"
    if is_valid_category_code(code):
        return None
    # 命中历史违规形态，给出定向建议
    dom = split_domain(code)
    if re.match(rf"^{_PREFIX}-L\d+-\d+$", code):
        return (
            "code '" + code + "' 把可变层级编码进了不可变 ID（旧分组文法）。"
            "应改为纯序号形式：'" + dom + "-<序号>'（叶子/分组统一，层级由结构判定，不写进 ID）。"
        )
    if re.match(rf"^{_PREFIX}-L\d+-[^0-9]+$", code):
        return (
            "code '" + code + "' 把人类名称编码进了不可变 ID（改名即变 code，破坏外键稳定性）。"
            "分组名称请放进 name 字段，code 改用纯序号：'" + dom + "-<序号>'。"
        )
    return (
        "code '" + code + "' 不符合命名规范。合法形式仅两种："
        "域根 '{域}-L1'，或普通节点 '{域}-<纯数字序号>'（如 虚拟机-017）。"
    )


def find_nonconforming_codes(items: list[dict[str, Any]]) -> list[str]:
    """从分类条目列表（``[{code, name}, ...]``）中找出「普通节点」形态不合规的 code。

    仅对**非域根**条目告警：域根 ``{域}-L1`` 是合法特殊形态，不应被计入漂移。
    供消费侧（S0）与巡检定位数据漂移使用。
    """
    bad: list[str] = []
    for item in items:
        code = item.get("code")
        if not isinstance(code, str) or not code:
            continue
        if is_domain_root_code(code):
            continue
        if not is_node_code(code):
            bad.append(code)
    return bad


def find_flatten_candidates(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """导入扁平化校验：找出「只有 1 个子节点」的中间分组节点。

    规则（需求2）：一个分组（非域根、有子）若直接子节点数恰好为 1，则应干掉
    该分组层级，由唯一子节点顶替其位置。只有域根（L1）、叶子（无子）以及
    子节点数 ≥ 2 的分组可保留。

    Args:
        nodes: 条目列表，每项含 ``code`` 与 ``parent_code``（域根 parent_code 为 None）。

    Returns:
        需扁平化删除的分组节点条目列表（附 ``only_child_code`` 供顶替）。
    """
    child_count: dict[str, int] = {}
    only_child: dict[str, str] = {}
    for n in nodes:
        pc = n.get("parent_code")
        if pc:
            child_count[pc] = child_count.get(pc, 0) + 1
            only_child[pc] = n.get("code", "")
    result: list[dict[str, Any]] = []
    for n in nodes:
        code = n.get("code")
        if not code or is_domain_root_code(code):
            continue
        if child_count.get(code, 0) == 1:
            result.append({**n, "only_child_code": only_child.get(code, "")})
    return result
