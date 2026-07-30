"""加载 §6.1 导出的 JSON Schema 契约，并用 jsonschema 校验 signals_json（保存时强制）。

来源：RFC《关键信号数据模型分层重构》§6.1。
契约文件由 `backend/scripts/gen-schemas.py` 导出到本目录 `signals/`。
依赖：jsonschema(>=4.21) + referencing（运行时依赖，见根 pyproject.toml）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7
from shared.schemas.acquirer_args import validate_acquire_args
from shared.schemas.log_source_catalog import (
    LOG_MATCHER_TYPES,
    REQUEST_ARTIFACT_ROOT,
    normalize_log_path,
    resolve_log_source,
)

_SIGNALS_DIR = Path(__file__).resolve().parent / "signals"


def _build_registry() -> Registry:
    """把所有 *.schema.json 装入 referencing Registry（以各自 $id 为键）。"""
    resources: list[tuple[str, Resource]] = []
    for p in sorted(_SIGNALS_DIR.rglob("*.schema.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        uri = data.get("$id")
        if not uri:
            # 兜底：以相对路径作为 $id（正常生成文件都带绝对 $id）
            uri = p.relative_to(_SIGNALS_DIR).as_posix()
        resources.append((uri, Resource.from_contents(data, default_specification=DRAFT7)))
    return Registry().with_resources(resources)


_REGISTRY = _build_registry()
_SIGNAL_V2_SCHEMA = json.loads((_SIGNALS_DIR / "signal.v2.schema.json").read_text(encoding="utf-8"))


def validate_signals_json(raw: Any) -> None:
    """校验整段 `signals_json`（v2 对象）符合 `signal.v2.schema.json`。

    同时经 if/then 逐条校验 `acquire.args`（按 `tool` 选 `acquirer_args/{tool}.schema.json`）。
    `additionalProperties:false` 会拒绝幽灵字段与顶层 `keyword` 等回归。
    失败时抛 `jsonschema.ValidationError`（调用方应转为 422）。
    """
    Draft7Validator(_SIGNAL_V2_SCHEMA, registry=_REGISTRY).validate(raw)
    _validate_qfk_match_or_produces(raw)
    _validate_runtime_acquire_args(raw)
    _validate_verification_contract(raw)


def _validate_runtime_acquire_args(raw: Any) -> None:
    """Close the save-time/runtime gap for semantic QKV/QFK arguments."""

    if not isinstance(raw, dict):
        return
    for index, signal in enumerate(raw.get("signals") or []):
        if not isinstance(signal, dict):
            continue
        acquire = signal.get("acquire") or {}
        tool = str(acquire.get("tool") or "")
        ok, error = validate_acquire_args(tool, acquire.get("args") or {})
        if not ok:
            raise ValidationError(f"signals[{index}] 运行时参数不可编译: {error}")


def validate_publishable_signals_json(raw: Any) -> None:
    """发布门禁：在结构契约之上强制稳定、非空且唯一的 signal id。"""
    validate_signals_json(raw)
    signals = raw.get("signals") if isinstance(raw, dict) else None
    seen: set[str] = set()
    for index, signal in enumerate(signals or []):
        signal_id = str(signal.get("id") or "").strip() if isinstance(signal, dict) else ""
        if not signal_id:
            raise ValidationError(f"signals[{index}] 缺少稳定 id，禁止发布")
        if signal_id in seen:
            raise ValidationError(f"signal id 重复，禁止发布: {signal_id}")
        seen.add(signal_id)


def _validate_verification_contract(raw: Any) -> None:
    if not isinstance(raw, dict) or not isinstance(raw.get("verification_contract"), dict):
        return
    signals = raw.get("signals") or []
    known_ids = {
        str(signal.get("id"))
        for signal in signals
        if isinstance(signal, dict) and signal.get("id")
    }
    policy = raw["verification_contract"].get("evidence_policy") or {}
    assigned: dict[str, str] = {}
    for role in ("must", "should", "exclude", "context"):
        for signal_id in policy.get(role) or []:
            if signal_id not in known_ids:
                raise ValidationError(f"verification_contract.{role} 引用了不存在的 signal_id: {signal_id}")
            if signal_id in assigned:
                raise ValidationError(
                    f"verification_contract 中 signal_id={signal_id} 同时属于 {assigned[signal_id]} 和 {role}"
                )
            assigned[signal_id] = role
    if signals and not (policy.get("must") or []):
        raise ValidationError("verification_contract.evidence_policy.must 至少需要 1 条必要信号")
    if int(policy.get("minimum_should", 0)) > len(policy.get("should") or []):
        raise ValidationError("verification_contract.minimum_should 超过 should 信号数量")


def _validate_qfk_match_or_produces(raw: Any) -> None:
    """校验生产者/QFK 的判定与产出契约。"""
    if not isinstance(raw, dict):
        return
    for index, signal in enumerate(raw.get("signals") or []):
        if not isinstance(signal, dict):
            continue
        tool = ((signal.get("acquire") or {}).get("tool") or "")
        produces = ((signal.get("orchestrate") or {}).get("produces") or [])
        matcher = signal.get("match")
        has_produces = any(
            isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()
            for item in produces
        )
        if isinstance(tool, str) and tool.startswith("qkv_"):
            if isinstance(matcher, dict) or not has_produces:
                raise ValidationError(
                    f"signals[{index}] 的 {tool} 是产出变量信号，必须配置 orchestrate.produces 且 match 必须为 null"
                )
            if any(isinstance(item, dict) and item.get("extract") is not None for item in produces):
                raise ValidationError(
                    f"signals[{index}] 的 {tool} 只支持 JSON path，不支持文本 extract"
                )
            continue
        if not isinstance(tool, str) or not tool.startswith("qfk_"):
            continue
        command = str(((signal.get("acquire") or {}).get("args") or {}).get("command") or "")
        if "|" in command:
            raise ValidationError(
                f"signals[{index}] 的 command 禁止保存 shell 管道；请先转换为结构化 extract"
            )
        has_match = isinstance(matcher, dict)
        if has_match == has_produces:
            raise ValidationError(
                f"signals[{index}] 的 {tool} 必须且只能配置“关键字判定(match)”或“产出变量(orchestrate.produces)”之一"
            )
        if tool == "qfk_log":
            args = ((signal.get("acquire") or {}).get("args") or {})
            normalized_path = normalize_log_path(str(args.get("path"))) if args.get("path") else None
            is_request_artifact = bool(
                normalized_path
                and (
                    normalized_path == REQUEST_ARTIFACT_ROOT
                    or normalized_path.startswith(f"{REQUEST_ARTIFACT_ROOT}/")
                )
            )
            if not is_request_artifact and not str(args.get("file") or "").strip():
                raise ValidationError(f"signals[{index}] 的 qfk_log 常规日志缺少必填字段 file")
            if has_match:
                matcher_type = str(matcher.get("type") or "")
                if matcher_type not in LOG_MATCHER_TYPES:
                    raise ValidationError(
                        f"signals[{index}] 的 qfk_log matcher.type={matcher_type} 不受支持；"
                        f"允许: {LOG_MATCHER_TYPES}"
                    )
                if is_request_artifact:
                    source = {
                        "source_id": "request_artifact_scope",
                        "parser": "plain_text",
                        "predicates": ["keyword", "regex", "state", "exists"],
                    }
                else:
                    try:
                        source = resolve_log_source(
                            str(args.get("file") or ""),
                            source_family=str(args.get("source_family") or "auto"),
                            path=normalized_path,
                            parser=str(args.get("parser")) if args.get("parser") else None,
                        )
                    except ValueError as exc:
                        raise ValidationError(f"signals[{index}] 的 qfk_log 日志源不可解析: {exc}") from exc
                if matcher_type not in source.get("predicates", []):
                    raise ValidationError(
                        f"signals[{index}] 的日志源 {source.get('source_id')} / parser={source.get('parser')} "
                        f"不支持 {matcher_type} predicate"
                    )
            elif not (args.get("resource_keyword") or args.get("request_id")):
                raise ValidationError(
                    f"signals[{index}] 的 qfk_log 产出变量采集必须提供 resource_keyword 或 request_id，"
                    "禁止无界整文件回传"
                )
        if isinstance(matcher, dict) and matcher.get("type") == "regex":
            pattern = matcher.get("pattern")
            if not isinstance(pattern, str):
                raise ValidationError(f"signals[{index}] 的 regex pattern 必须是字符串")
            try:
                re.compile(pattern)
            except (re.error, TypeError) as exc:
                raise ValidationError(
                    f"signals[{index}] 的 regex pattern 非法: {exc}"
                ) from exc
