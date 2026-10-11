"""category_baseline.yaml（v2.0 全树显式码格式）的纯生成器。

单一事实源：`/export` 接口与 `scripts/refresh_category_baseline.py` 均调用本模块，
保证「线上 DB → 基线文件」的产出字节一致、可复现。

v2.0 全树格式（与 v1.0 叶子清单互斥）：
    categories:
      - code: 虚拟机-L1        # 域根固定 {域}-L1
        name: 虚拟机
        domain: 虚拟机
        level: 1
        parent: null           # 域根恒 null；其余为父节点 code
        path: [虚拟机]

只有全树（显式 code + parent）才能忠实编码拖拽重排后的**精确编码与同级顺序**；
v1.0 叶子清单会由导入侧重推导、重编号分组，无法保留人工顺序，故基线统一走 v2.0。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml

_HEADER = """\
# ─────────────────────────────────────────────────────────────────────────────
# HCI 云平台故障分类基线（category_baseline）—— 分类基线唯一权威定义
# ─────────────────────────────────────────────────────────────────────────────
# 本文件是 kb_category 分类树的【全树快照】：由线上 DB 导出，反映最新拖拽重排结果。
# ⚠️ 请勿手工编辑：改序请在分类管理页拖拽（即时物化 code），再运行
#    `uv run python scripts/refresh_category_baseline.py` 从 DB 重生成并提交本文件。
#
# 【code 命名文法（解A：角色无关统一 ID）】
#   1. 域根（每域唯一）：`{域}-L1`（如 `虚拟机-L1`）。parent 恒为 null，永不是叶子。
#   2. 其余所有节点（分组与叶子一视同仁）：`{域}-{3 位补零序号}`（如 `虚拟机-021`），
#      每域共享一个独立序号空间；同级顺序即序号（拖拽重排会整域 DFS 重编号）。
#   3. 叶子 vs 分组：纯由树结构（有无子节点）判定，与 code 形态正交。
#   4. DB 侧由 CHECK 约束 `kb_category_code_format` 强制本文法（见 desired_schema.sql）。
#
# 【字段】code=编码 / name=名称 / domain=一级域 / level=层级 / parent=父 code / path=完整路径
"""


def render_baseline_yaml(
    nodes: list[dict[str, Any]],
    *,
    generated: str | None = None,
    source: str = "kb_category 全树快照（DB 导出，解A DFS 重排）",
) -> str:
    """把分类节点列表渲染为 v2.0 全树 YAML 字符串。

    Args:
        nodes: 每项需含 `code/name/domain/level/parent/path`；`parent` 为父节点 code 或 None。
        generated: 生成日期（YYYY-MM-DD），默认今天（UTC）。
        source: 头部 source 说明。

    Returns:
        完整 YAML 文本（含注释头）。
    """
    # 排序：域→层级→code（解A 下 code 零填充，字典序==序号序==拖拽视觉序；域根 level=1 恒先）
    ordered = sorted(
        nodes,
        key=lambda n: (str(n.get("domain") or ""), int(n.get("level") or 0), str(n.get("code") or "")),
    )

    items: list[dict[str, Any]] = []
    for n in ordered:
        items.append(
            {
                "code": n.get("code"),
                "name": n.get("name"),
                "domain": n.get("domain"),
                "level": n.get("level"),
                "parent": n.get("parent"),
                "path": list(n.get("path") or []),
            }
        )

    date_str = generated or datetime.now(UTC).strftime("%Y-%m-%d")
    body = yaml.dump(
        items,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    head = (
        _HEADER
        + "\n"
        + 'version: "2.0"\n'
        + f'source: "{source}"\n'
        + f'generated: "{date_str}"\n'
        + f"\ntotal: {len(items)}\n"
        + "categories:\n"
    )
    return head + body


def nodes_from_orm(categories: list[Any]) -> list[dict[str, Any]]:
    """把 `KbCategory` ORM 列表转为 `render_baseline_yaml` 入参（解析 parent 为父 code）。

    接受任何具备 `.id/.code/.name/.domain/.level/.parent_id/.path_labels` 属性的对象。
    """
    id_to_code = {c.id: c.code for c in categories}
    out: list[dict[str, Any]] = []
    for c in categories:
        out.append(
            {
                "code": c.code,
                "name": c.name,
                "domain": c.domain,
                "level": c.level,
                "parent": id_to_code.get(c.parent_id) if c.parent_id is not None else None,
                "path": list(c.path_labels or []),
            }
        )
    return out
