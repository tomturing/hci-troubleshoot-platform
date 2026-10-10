"""分类 code 形态契约（跨服务共享）。

叶子分类 code 约定以 ``-<纯数字>`` 结尾（如 ``虚拟机-015``、``硬件-L3-002``）；
中间层分组节点的 code 由 path 派生、末段可为中文（如 ``虚拟机-L2-虚拟机迁移``）。
"叶子 vs 中间层"由 kb-service 的 ``leaf_only`` 查询语义（是否存在子节点）区分，
code 形态仅作辅助探针，**不是**叶子资格的权威判据。

契约背景（2026-10-10 Q2026101026818 复盘）：
    中间层节点在数据漂移（子分类被删除/拖拽）后退化为"叶子形态"，此时若下游
    仍按 code 形态静默剔除，节点会从 S0 候选与 Prompt 中"隐形"——管理页可见、
    AI 永远不可选。因此双向夹住漂移窗口：
    - 消费侧（agent-service S0）：不合规 code 的叶子**放行 + 告警**，不静默剔除；
    - 导入侧（kb-service 导入 / seed_categories 脚本）：**叶节点 id** 不合规即
      fail-fast 终止导入；中间层 code 属已知派生形态，仅告警不阻断。
    注意：不合规 code 可能被 kbd_entry.category_id 引用，**禁止**通过改名/重建
    的方式"修复"已有节点，数据修复一律走基线重导（upsert，code 不变）。
"""

from __future__ import annotations

import re
from typing import Any

# 叶子节点 code 形态：前缀（中文/字母/数字/连字符）+ ``-<纯数字>`` 结尾。
# 与 data-pipeline/kbd/seed_categories.py 的内联副本保持一致（该脚本独立运行，
# 不依赖 backend/shared），修改时必须同步两处。
LEAF_CODE_RE = re.compile(r"^[一-鿿A-Za-z0-9-]+-\d+$")


def is_leaf_code(code: object) -> bool:
    """判断 code 是否符合叶子分类形态（前缀-纯数字结尾）。"""
    return isinstance(code, str) and LEAF_CODE_RE.match(code) is not None


def find_nonconforming_codes(items: list[dict[str, Any]]) -> list[str]:
    """从分类条目列表（``[{code, name}, ...]``）中找出不符合叶子形态的 code。"""
    return [str(item.get("code")) for item in items if not is_leaf_code(item.get("code"))]
