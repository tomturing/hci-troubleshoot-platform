"""
KBD 数据模型（知识库文档 Knowledge Base Document）— 关键信号（signals_json）迭代

设计约定（v2）：
  - signals 字段来自 kb-service 返回的 "signals" key（原 "steps" key 已废弃）
  - 每条 signal 为 dict，包含 acquirer（采集器）、acquirer_args（参数模板）、matcher（判定契约）
  - KBDStep 从 signal dict 按需构建，tool_name = acquirer，expected_pattern 由 matcher 序列化
  - tool_args_template 中占位符统一 {{VAR}} 大写（ADR-2）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# 期望模式前缀常量，供判断逻辑识别
PATTERN_REGEX_PREFIX = "__REGEX__:"
PATTERN_CONTAINS_PREFIX = "__CONTAINS__:"
PATTERN_MATCHER_PREFIX = "__MATCHER__:"


def _matcher_to_expected(matcher: dict[str, Any] | None) -> str:
    """将 Matcher dict 序列化为 __REGEX__:/__CONTAINS__:/__MATCHER__: 格式，兼容原有 CDD judge。"""
    if not matcher or not isinstance(matcher, dict):
        return ""
    mtype = matcher.get("type", "")
    pattern = matcher.get("pattern", "")
    if not pattern:
        return ""
    if mtype == "regex":
        return f"{PATTERN_REGEX_PREFIX}{pattern}"
    if mtype == "keyword" and isinstance(pattern, str):
        return f"{PATTERN_CONTAINS_PREFIX}{pattern}"
    # state/threshold/json_path/exists → 序列化为 JSON，由 LLM judge 处理
    return f"{PATTERN_MATCHER_PREFIX}{json.dumps(matcher, ensure_ascii=False)}"


def _signal_to_step(s: dict[str, Any]) -> KBDStep | None:
    """从 signal dict 构建 KBDStep（仅 consumer/backend 信号可执行）。

    兼容两种契约：
      - 新规范：matcher dict 优先（_matcher_to_expected 序列化为 expected_pattern）
      - 旧遗留：signal dict 直接携带 expected_pattern（__REGEX__:/__CONTAINS__: 等）时回退使用
    """
    acquirer = s.get("acquirer", "")
    if not acquirer or s.get("signal_category") != "backend":
        return None
    matcher = s.get("matcher")
    expected = _matcher_to_expected(matcher) if matcher else s.get("expected_pattern", "")
    return KBDStep(
        tool_name=acquirer,
        tool_args_template=s.get("acquirer_args") or {},
        expected_pattern=expected,
        matcher=matcher,
    )


@dataclass
class KBDStep:
    """KBD 中单个诊断步骤的定义。

    tool_name 是归一化 key：同一分类下不同 KBD 若使用相同工具，
    其 tool_name 相同，便于 CDD 算法计算步骤覆盖频率。
    """

    tool_name: str  # 工具名称（对应 acquirer，如 qfk_log）
    tool_args_template: dict  # 参数模板（含 {{占位符}}，执行时由 env_context ∪ 变量池填充）
    expected_pattern: str  # 期望输出特征（__REGEX__:/ __CONTAINS__:/ __MATCHER__:/ 自然语言，兼容旧 KBD）
    matcher: dict | None = None  # 消费者信号的判定契约（Matcher dict），供 _judge_matches 类型化求值


@dataclass
class KBD:
    """知识库文档（Knowledge Base Document）。

    由 kb_client.search_cases_with_steps() 从知识库检索获得。
    KBD 差异诊断引擎对 candidates: list[KBD] 执行差异分析。

    字段与文档章节对应关系（8 大标准章节）：
      核心字段（4 个，诊断与呈现必需）：
        problem_description → 问题描述
        signals             → 关键信号集合（producer/consumer）
        root_cause          → 根因
        solution            → 解决方案
    """

    # ── 基础标识 ──────────────────────────────────────────────
    id: str  # KBD 唯一 ID
    name: str  # KBD 名称（简短描述）
    category_id: str  # 所属故障分类编码，如 "虚拟机-003"

    # ── 核心内容（4 大章节，必填）────────────────────────────
    problem_description: str
    signals: list[dict]  # 关键信号集合（raw dicts，含 signal_category/acquirer/acquirer_args/matcher）
    root_cause: str
    solution: str

    # ── 补充内容（4 个章节，可选）────────────────────────────
    alert_info: str = ""
    operational_impact: str = ""
    is_temporary: str = ""
    recommendations: str = ""

    # ── 检索元数据 ────────────────────────────────────────────
    similarity: float = 0.0

    @property
    def step_tool_names(self) -> set[str]:
        """返回本 KBD 所有 consumer（backend）信号的 acquirer 集合（用于频率统计）。"""
        return {
            s["acquirer"]
            for s in self.signals
            if s.get("acquirer") and s.get("signal_category") == "backend"
        }

    def get_step(self, tool_name: str) -> KBDStep | None:
        """按 acquirer 获取 consumer 信号的步骤定义。"""
        for s in self.signals:
            if s.get("acquirer") == tool_name and s.get("signal_category") == "backend":
                return _signal_to_step(s)
        return None

    def get_signal(self, tool_name: str) -> dict | None:
        """按 acquirer 获取原始信号 dict（供执行层做写操作门禁判定）。"""
        for s in self.signals:
            if s.get("acquirer") == tool_name:
                return s
        return None

    def get_expected_pattern(self, tool_name: str) -> str | None:
        """返回指定 acquirer 对应的期望输出模式（用于 judge）。"""
        step = self.get_step(tool_name)
        return step.expected_pattern if step else None

    def get_matcher(self, tool_name: str) -> dict | None:
        """返回指定 acquirer 对应的 Matcher dict（供 _judge_matches 类型化求值）。"""
        for s in self.signals:
            if s.get("acquirer") == tool_name and s.get("signal_category") == "backend":
                return s.get("matcher")
        return None


def kbd_from_dict(d: dict) -> KBD:
    """从 KB API 返回的 dict 构建 KBD 对象（工厂函数）。

    v2: 读取 "signals" key（producer/consumer 信号数组），废弃旧 "steps" key。
    """
    # ADR-1：仅读 signals_json，无回退、无兼容桥（旧 steps 字段已彻底移除）
    signals = d.get("signals", [])

    return KBD(
        id=d["id"],
        name=d.get("name", ""),
        category_id=d.get("category_id", ""),
        problem_description=d.get("problem_description", ""),
        signals=signals,
        root_cause=d.get("root_cause", ""),
        solution=d.get("solution", ""),
        alert_info=d.get("alert_info", ""),
        operational_impact=d.get("operational_impact", ""),
        is_temporary=d.get("is_temporary", ""),
        recommendations=d.get("recommendations", ""),
        similarity=float(d.get("similarity", 0.0)),
    )
