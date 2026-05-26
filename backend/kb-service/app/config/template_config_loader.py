"""
KB Service — SOP 模板规则配置加载器

使用 YAML 配置文件（sop_template_rules.yaml）驱动校验行为：
  - 关键词等效表（diagnosis / solution / variables / prerequisites）
  - 标准话术词（偏差时报 error）
  - 校验级别（error / warning）

Python 层（sop_template.py）负责模型结构定义，不在此文件重复。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).parent / "sop_template_rules.yaml"


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    """加载并缓存原始 YAML，服务启动后只读一次。"""
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def reload_config() -> None:
    """清除缓存，强制下次调用时重新读取配置文件（测试/热更新用）。"""
    _load_raw.cache_clear()


# ──────────────────────────────────────────────────────────────────────────────
# 公共访问器
# ──────────────────────────────────────────────────────────────────────────────


def get_keywords(section_type: str) -> frozenset[str]:
    """返回指定段落类型的关键词集合。

    section_type: "diagnosis" | "solution" | "variables" | "prerequisites"
    """
    raw = _load_raw()
    return frozenset(raw.get("keywords", {}).get(section_type, []))


def get_standard_heading(section_type: str) -> str:
    """返回指定段落类型的标准话术词。"""
    raw = _load_raw()
    return raw.get("standard_headings", {}).get(section_type, "")


def get_validation_level(rule_key: str) -> str:
    """返回指定规则的校验级别（"error" 或 "warning"）。

    默认返回 "warning"（宽松降级，避免配置缺失时误报 error）。
    """
    raw = _load_raw()
    return raw.get("validation_levels", {}).get(rule_key, "warning")


def get_prerequisite_type_keywords() -> dict[str, list[str]]:
    """返回前置检查类型识别关键词 {"filter": [...], "sequence": [...]}。"""
    raw = _load_raw()
    return raw.get("prerequisite_type_keywords", {"filter": [], "sequence": []})


def get_binary_outcome_patterns() -> list[str]:
    """返回暗示二元结果（2 个子节点）的前置检查关键词列表。"""
    raw = _load_raw()
    return raw.get("binary_outcome_patterns", [])
