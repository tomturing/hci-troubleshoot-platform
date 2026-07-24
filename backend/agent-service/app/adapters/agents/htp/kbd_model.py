"""KBD 关键信号文档模型（v2 嵌套信号契约）。

signals_json 的唯一权威格式为 v2 嵌套：
  { schema_version: 2,
    signals: [ { id, acquire: {tool, args}, match, orchestrate: {requires, produces},
                provenance: {category, ...}, review: {require_human_confirm} } ] }

KBD 运行时直接消费 v2 嵌套信号，不存在 v1 扁平桥接、过渡版本或中间表示。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ─── Matcher 序列化辅助（v2 契约的一部分，非迁移逻辑）────────────────────
PATTERN_REGEX_PREFIX = "__REGEX__:"
PATTERN_CONTAINS_PREFIX = "__CONTAINS__:"
PATTERN_MATCHER_PREFIX = "__MATCHER__:"


def _matcher_to_expected(matcher: Any) -> str | None:
    """把 Matcher 契约转成历史兼容的 expected_pattern 三态字符串。

    返回 None 表示该信号无 expected_pattern（交由 LLM 判断）。
    """
    if matcher is None:
        return None
    if isinstance(matcher, dict):
        mtype = matcher.get("type")
        if mtype == "regex":
            return PATTERN_REGEX_PREFIX + (matcher.get("pattern") or "")
        if mtype == "contains":
            return PATTERN_CONTAINS_PREFIX + (matcher.get("keyword") or "")
        if mtype == "matcher":
            return PATTERN_MATCHER_PREFIX + json.dumps(matcher, ensure_ascii=False)
        return None
    if isinstance(matcher, str):
        # 旧 Matcher 序列化（__REGEX__:/__CONTAINS__:/__MATCHER__:）或自然语言
        return matcher
    return None


def _matcher_to_serialized(matcher: Any) -> str:
    """把 Matcher 契约序列化为字符串（用于序列化回溯）。"""
    if matcher is None:
        return ""
    if isinstance(matcher, dict):
        return json.dumps(matcher, ensure_ascii=False)
    return str(matcher)


# ─── v2 嵌套信号字段访问辅助 ──────────────────────────────────────────
def _acquire_tool(sig: dict[str, Any]) -> str:
    """v2 嵌套信号中的采集器工具名（acquire.tool）。"""
    return (sig.get("acquire") or {}).get("tool", "")


def _signal_category(sig: dict[str, Any]) -> str:
    """信号类别：优先取 provenance.category，否则由 acquire.tool 前缀推导。

    qfk_* → backend（消费者/后端可执行）；qkv_* → frontend（生产者）。
    """
    cat = (sig.get("provenance") or {}).get("category")
    if cat:
        return cat
    tool = _acquire_tool(sig)
    return "backend" if tool.startswith("qfk") else "frontend"


def _signal_to_step(s: dict[str, Any]) -> KBDStep | None:
    """从 v2 嵌套 signal 构建 KBDStep。

    调用方（get_step/get_matcher）已按 backend 类别过滤，此处仅要求 acquire.tool 非空。
    """
    acquire = s.get("acquire") or {}
    tool = acquire.get("tool", "")
    if not tool:
        return None
    args = acquire.get("args") or {}
    matcher = s.get("match")
    expected = _matcher_to_expected(matcher)
    return KBDStep(
        tool_name=tool,
        tool_args_template=args,
        expected_pattern=expected,
        matcher=matcher,
    )


class KBDStep(BaseModel):
    """单个可执行步骤（从 v2 嵌套信号按需构建）。

    tool_name = acquire.tool（如 qfk_log）；tool_args_template = acquire.args。
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: str  # 工具名称（对应 acquire.tool，如 qfk_log）
    tool_args_template: dict = Field(default_factory=dict)  # acquire.args 参数模板
    expected_pattern: str | None = None  # 兼容旧 matcher 序列化字符串（三态）
    matcher: Any = None  # 保留 matcher dict 供 _evaluate_matcher 使用


class KBD(BaseModel):
    """关键信号文档（KBD）。

    signals 为 v2 嵌套信号 dict 列表。消费者（backend）信号可执行诊断步骤，
    生产者（frontend）信号用于阶段 A 填充变量池。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str = ""
    root_cause: str = ""
    solution: str = ""
    similarity: float = 0.0
    type: str = ""
    signals: list[dict] = Field(default_factory=list)  # v2 嵌套信号集合

    @property
    def step_tool_names(self) -> set[str]:
        """返回本 KBD 所有 consumer（backend）信号的 acquire.tool 集合（用于频率统计）。"""
        return {
            _acquire_tool(s)
            for s in self.signals
            if _acquire_tool(s) and _signal_category(s) == "backend"
        }

    def get_step(self, tool_name: str) -> KBDStep | None:
        """按 acquire.tool 获取 consumer 信号的步骤定义。"""
        for s in self.signals:
            if _acquire_tool(s) == tool_name and _signal_category(s) == "backend":
                return _signal_to_step(s)
        return None

    def get_signal(self, tool_name: str) -> dict | None:
        """按 acquire.tool 获取原始信号 dict（v2 嵌套）。"""
        for s in self.signals:
            if _acquire_tool(s) == tool_name:
                return s
        return None

    def get_matcher(self, tool_name: str) -> Any:
        """按 acquire.tool 获取 consumer 信号的 match 契约。"""
        for s in self.signals:
            if _acquire_tool(s) == tool_name and _signal_category(s) == "backend":
                return s.get("match")
        return None

    def get_expected_pattern(self, tool_name: str) -> str | None:
        """按 acquire.tool 获取兼容旧 matcher 序列化的 expected_pattern（三态）。"""
        step = self.get_step(tool_name)
        return step.expected_pattern if step else None


def kbd_from_dict(d: dict[str, Any]) -> KBD:
    """从 KBD 文档 dict 构造 KBD。

    输入为 v2 数组级对象 {schema_version, signals}（或直接含 signals 的 KBD dict）。
    v1 扁平 list 与 to_legacy_signal 反向桥接已彻底移除，运行时仅存在 v2 嵌套单一版本。
    """
    signals = d.get("signals", [])
    if not isinstance(signals, list):
        signals = []
    return KBD(
        id=d.get("id", ""),
        name=d.get("name", ""),
        root_cause=d.get("root_cause", ""),
        solution=d.get("solution", ""),
        similarity=float(d.get("similarity", 0.0) or 0.0),
        type=d.get("type", ""),
        signals=signals,
    )
