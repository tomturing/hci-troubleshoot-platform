"""KBD 关键信号文档模型（v2 嵌套信号契约）。

signals_json 的唯一权威格式为 v2 嵌套：
  { schema_version: 2,
    signals: [ { id, acquire: {tool, args}, match, orchestrate: {requires, produces},
                provenance: {category, ...}, review: {require_human_confirm} } ] }

KBD 运行时直接消费 v2 嵌套信号，不存在 v1 扁平桥接、过渡版本或中间表示。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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

    调用方按 signal_id 编排，此处仅要求 acquire.tool 非空。
    """
    acquire = s.get("acquire") or {}
    tool = acquire.get("tool", "")
    if not tool:
        return None
    args = acquire.get("args") or {}
    return KBDStep(
        tool_name=tool,
        tool_args_template=args,
        matcher=s.get("match"),
    )


class KBDStep(BaseModel):
    """单个可执行步骤（从 v2 嵌套信号按需构建）。

    tool_name = acquire.tool（如 qfk_log）；tool_args_template = acquire.args。
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: str  # 工具名称（对应 acquire.tool，如 qfk_log）
    tool_args_template: dict = Field(default_factory=dict)  # acquire.args 参数模板
    matcher: Any = None  # 保留 matcher dict 供 _evaluate_matcher 使用


class KBD(BaseModel):
    """关键信号文档（KBD）。

    signals 为 v2 嵌套信号 dict 列表。消费者（backend）信号可执行诊断步骤，
    生产者（frontend）信号用于阶段 A 填充变量池。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    support_id: str = ""
    name: str = ""
    category_id: str = ""
    root_cause: str = ""
    solution: str = ""
    similarity: float = 0.0
    type: str = ""
    signals: list[dict] = Field(default_factory=list)  # v2 嵌套信号集合
    resource_revision: dict = Field(default_factory=dict)
    verification_contract: dict = Field(default_factory=dict)
    generation_metadata: dict = Field(default_factory=dict)
    publish_validation: dict = Field(default_factory=dict)


def kbd_from_dict(d: dict[str, Any]) -> KBD:
    """从 KBD 文档 dict 构造 KBD。

    输入为 v2 数组级对象 {schema_version, signals}（或直接含 signals 的 KBD dict）。
    支持兼容 DB 原始列 dict 形态 {"schema_version": 2, "signals": [...]} 与标准 list 数组。
    """
    signals = d.get("signals", [])
    verification_contract = (
        d.get("verification_contract")
        or d.get("case_verification_contract")
        or {}
    )
    if isinstance(signals, dict):
        verification_contract = signals.get("verification_contract") or verification_contract
        generation_metadata = signals.get("generation_metadata") or d.get("generation_metadata") or {}
        publish_validation = signals.get("publish_validation") or d.get("publish_validation") or {}
        signals = signals.get("signals", [])
    elif not isinstance(signals, list):
        signals = []
        generation_metadata = d.get("generation_metadata") or {}
        publish_validation = d.get("publish_validation") or {}
    else:
        generation_metadata = d.get("generation_metadata") or {}
        publish_validation = d.get("publish_validation") or {}
    return KBD(
        id=d.get("id", ""),
        support_id=str(d.get("support_id", "") or ""),
        name=d.get("name", ""),
        category_id=d.get("category_id", ""),
        root_cause=d.get("root_cause", ""),
        solution=d.get("solution", ""),
        similarity=float(d.get("similarity", 0.0) or 0.0),
        type=d.get("type", ""),
        signals=signals,
        resource_revision=d.get("resource_revision") or {},
        verification_contract=verification_contract,
        generation_metadata=generation_metadata,
        publish_validation=publish_validation,
    )
