"""QFK/QKV 统一 AI 后处理契约。

AI 是确定性取值后的可选再加工阶段。它必须返回结构化输出和可回查证据，平台
负责验证、类型适配以及后续 Matcher/变量处理；本模块不包含任何案例专用算法。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

AI_PROCESSING_MODES = {"extract", "derive"}
AI_OUTPUT_TYPES = {"boolean", "number", "string", "array"}
AI_ITEM_TYPES = {"boolean", "number", "string"}


@dataclass(frozen=True)
class AIEvidence:
    """一条可回查的候选输入证据。"""

    ref: str
    quote: str


@dataclass(frozen=True)
class AIProcessingResult:
    """统一 AI 后处理结果。"""

    output: Any
    evidence: list[AIEvidence]
    reason: str
    status: str = "success"


def ai_processing_config(spec: Any) -> dict[str, Any] | None:
    """读取新配置；历史 ai_extract 只作为迁移期兼容别名。"""

    if not isinstance(spec, dict):
        return None
    config = spec.get("ai_processing")
    if isinstance(config, dict):
        return config
    legacy = spec.get("ai_extract")
    if not isinstance(legacy, dict):
        return None
    # 仅用于读取历史草稿：旧 derive 的案例专用 normalizer 不再执行，
    # 迁移为通用智能推导数组契约，要求用户在发布前确认处理说明。
    migrated = {"contract_version": 1, "mode": str(legacy.get("mode") or "extract"), "instruction": str(legacy.get("instruction") or "")}
    if migrated["mode"] == "derive":
        migrated.update({"output_type": "array", "item_type": "number"})
    else:
        migrated["output_type"] = "string"
    return migrated


def ai_processing_mode(config: Any) -> str:
    if not isinstance(config, dict):
        return "extract"
    return str(config.get("mode") or "extract")


def ai_output_type(config: Any, fallback: str = "string") -> str:
    if isinstance(config, dict) and config.get("output_type"):
        return str(config["output_type"])
    normalized = str(fallback or "string").lower()
    return "array" if normalized.startswith("array") else ("number" if normalized in {"number", "integer"} else normalized)


def ai_item_type(config: Any, fallback: str = "string") -> str:
    if isinstance(config, dict) and config.get("item_type"):
        return str(config["item_type"])
    normalized = str(fallback or "string").lower()
    return "number" if normalized in {"array<number>", "number", "integer"} else normalized


def validate_ai_processing_config(config: Any) -> str:
    if not isinstance(config, dict):
        raise ValueError("AI 处理必须配置对象")
    instruction = config.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("AI 处理必须填写非空处理说明")
    if len(instruction) > 1000:
        raise ValueError("AI 处理说明不能超过 1000 个字符")
    mode = ai_processing_mode(config)
    if mode not in AI_PROCESSING_MODES:
        raise ValueError(f"AI 处理模式不受支持: {mode}")
    output_type = ai_output_type(config)
    if output_type not in AI_OUTPUT_TYPES:
        raise ValueError(f"AI 输出类型不受支持: {output_type}")
    item_type = ai_item_type(config)
    if output_type == "array" and item_type not in AI_ITEM_TYPES:
        raise ValueError(f"AI 数组元素类型不受支持: {item_type}")
    if output_type != "array" and "item_type" in config:
        raise ValueError("非数组 AI 输出不能配置 item_type")
    unknown = set(config) - {"contract_version", "mode", "instruction", "output_type", "item_type"}
    if unknown:
        raise ValueError(f"AI 处理包含未注册字段: {sorted(unknown)}")
    return mode


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_ai_output(value: Any, config: dict[str, Any], expected_type: str) -> Any:
    """校验 AI output_type 与下游消费者的兼容性，并返回标准化值。"""

    output_type = ai_output_type(config, expected_type)
    expected = str(expected_type or "string").lower()
    if output_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("AI 输出类型为布尔值时必须返回 JSON true/false")
        if expected not in {"boolean", "string"}:
            raise ValueError(f"AI 布尔输出不能交给 {expected} 消费")
        return value
    if output_type == "number":
        if not _finite_number(value):
            raise ValueError("AI 数值输出必须是有限 JSON 数字")
        if expected not in {"number", "integer", "string"}:
            raise ValueError(f"AI 数值输出不能交给 {expected} 消费")
        if expected == "integer" and int(value) != value:
            raise ValueError("AI 数值输出不能转换为整数")
        return int(value) if expected == "integer" else float(value) if expected == "number" else value
    if output_type == "string":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("AI 文本输出必须是非空字符串")
        if expected not in {"string", "boolean", "number", "integer"}:
            raise ValueError(f"AI 文本输出不能交给 {expected} 消费")
        return value.strip()
    if output_type == "array":
        if not isinstance(value, list) or not value:
            raise ValueError("AI 数组输出必须是非空数组")
        item_type = ai_item_type(config)
        if item_type == "number" and any(not _finite_number(item) for item in value):
            raise ValueError("AI 数组输出的元素必须全部是有限数字")
        if item_type == "string" and any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("AI 数组输出的元素必须全部是非空文本")
        if item_type == "boolean" and any(not isinstance(item, bool) for item in value):
            raise ValueError("AI 数组输出的元素必须全部是布尔值")
        if expected not in {"array", "array<number>", "string"}:
            raise ValueError(f"AI 数组输出不能交给 {expected} 消费")
        if expected == "array<number>" and item_type != "number":
            raise ValueError("数值数组消费者要求 item_type=number")
        return value
    raise ValueError(f"AI 输出类型不受支持: {output_type}")


def validate_ai_evidence(evidence: Any, candidates: dict[str, str]) -> list[AIEvidence]:
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("AI evidence 必须是非空数组")
    validated: list[AIEvidence] = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or set(item) != {"ref", "quote"}:
            raise ValueError(f"AI evidence[{index}] 必须只包含 ref 和 quote")
        ref, quote = item.get("ref"), item.get("quote")
        if not isinstance(ref, str) or ref not in candidates:
            raise ValueError(f"AI evidence[{index}] 引用了不存在的候选 {ref!r}")
        if not isinstance(quote, str) or not quote or quote not in candidates[ref]:
            raise ValueError(f"AI evidence[{index}] quote 不在候选 {ref!r} 中")
        validated.append(AIEvidence(ref=ref, quote=quote))
    return validated


def validate_ai_response(payload: Any, config: dict[str, Any], candidates: dict[str, str], expected_type: str) -> AIProcessingResult:
    """校验统一响应；任何结构错误都应由调用方 Fail Closed。"""

    if not isinstance(payload, dict) or set(payload) != {"status", "output", "evidence", "reason"}:
        raise ValueError("AI 响应必须只包含 status/output/evidence/reason")
    if payload.get("status") != "success":
        raise ValueError("AI 处理未成功完成")
    evidence = validate_ai_evidence(payload.get("evidence"), candidates)
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("AI reason 必须是非空文本")
    output = validate_ai_output(payload.get("output"), config, expected_type)
    return AIProcessingResult(output=output, evidence=evidence, reason=reason.strip())
