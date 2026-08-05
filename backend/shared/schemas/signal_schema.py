"""加载 §6.1 导出的 JSON Schema 契约，并用 jsonschema 校验 signals_json（保存时强制）。

来源：RFC《关键信号数据模型分层重构》§6.1。
契约文件由 `backend/scripts/gen-schemas.py` 导出到本目录 `signals/`。
依赖：jsonschema(>=4.21) + referencing（运行时依赖，见根 pyproject.toml）。
"""

from __future__ import annotations

import copy
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
from shared.schemas.signal_generation import current_tool_contract_revision

_SIGNALS_DIR = Path(__file__).resolve().parent / "signals"

_MATCHER_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "keyword": frozenset({"pattern"}),
    "regex": frozenset({"pattern"}),
    "state": frozenset({"pattern"}),
    "threshold": frozenset({"value", "operator"}),
    "delta": frozenset({"value", "operator"}),
    "trend": frozenset({"direction"}),
    "exists": frozenset(),
}
_MATCHER_OPTIONAL_FIELDS = frozenset(
    {
        "pattern",
        "mode",
        "value",
        "operator",
        "aggregation",
        "minimum_samples",
        "direction",
    }
)
_SIGNAL_FIELD_LABELS = {
    "acquire": "采集配置",
    "acquire.args": "采集参数",
    "match": "判定器",
    "match.pattern": "判定器 / 匹配内容",
    "match.extract": "判定器 / 取值配置",
    "match.value": "判定器 / 数值阈值",
    "match.operator": "判定器 / 比较方式",
    "orchestrate": "执行编排",
    "orchestrate.requires": "输入变量",
    "orchestrate.produces": "产出变量",
    "role": "证据作用",
}


def normalize_optional_matcher_nulls(raw: Any) -> Any:
    """移除 Matcher 中语义上可省略的显式 ``null``，不掩盖必填字段错误。

    JSON 的 ``null`` 与字段缺失是不同值；LLM/历史快照却经常为所有可能字段
    输出 ``null``。例如 ``exists`` 不读取 ``pattern``，所以 ``pattern=null`` 应
    归约为字段缺失。若 keyword/regex/state 的 pattern 为 null，则它仍是必填
    字段错误，必须保留给校验器和专家定位，不能静默修复。

    函数原地规范化并返回输入，便于抽取、保存和发布入口共用同一确定性边界。
    """

    if not isinstance(raw, dict):
        return raw
    signals = raw.get("signals") if isinstance(raw.get("signals"), list) else [raw]
    for signal in signals:
        if not isinstance(signal, dict) or not isinstance(signal.get("match"), dict):
            continue
        matcher = signal["match"]
        required = _MATCHER_REQUIRED_FIELDS.get(str(matcher.get("type") or ""), frozenset())
        for field in _MATCHER_OPTIONAL_FIELDS - required:
            if matcher.get(field) is None:
                matcher.pop(field, None)
    return raw


def humanize_signal_validation_error(error: ValidationError, signals: list[Any]) -> dict[str, Any]:
    """把 JSON Schema 错误转换成可定位、可执行的专家问题。

    ``ValidationError.message`` 只适合工程调试；专家至少需要知道稳定 Signal ID、
    页面字段和修复动作。机器字段保留给前端聚焦与日志检索，展示文本不泄漏
    ``anyOf``、``None`` 等 Schema 实现细节。
    """

    path = list(error.absolute_path)
    signal_index = path[1] if len(path) >= 2 and path[0] == "signals" and isinstance(path[1], int) else None
    signal = (
        signals[signal_index]
        if signal_index is not None and 0 <= signal_index < len(signals) and isinstance(signals[signal_index], dict)
        else None
    )
    signal_id = str(signal.get("id") or "").strip() if signal else ""
    relative_path = path[2:] if signal_index is not None else path
    field_path = ".".join(str(part) for part in relative_path)
    field_label = _SIGNAL_FIELD_LABELS.get(field_path)
    if field_label is None:
        field_label = next(
            (label for key, label in _SIGNAL_FIELD_LABELS.items() if field_path.startswith(f"{key}.")),
            "关键信号",
        )

    matcher = signal.get("match") if signal and isinstance(signal.get("match"), dict) else {}
    matcher_type = str(matcher.get("type") or "")
    if field_path == "match.pattern" and matcher_type == "exists":
        message = "存在性判定只检查输出是否为空，不使用“匹配内容”；请重新保存，平台会自动清理历史空值。"
        code = "MATCHER_UNUSED_FIELD"
    elif error.validator == "not" and field_path.startswith("orchestrate.produces"):
        message = "产出变量不能同时配置旧版 JSON 路径(path)和声明式取值(extract)；请保留当前取值配置后重新保存。"
        code = "PRODUCE_PATH_EXTRACT_CONFLICT"
    elif error.validator == "required":
        missing = str(error.message).split("'")[1] if "'" in error.message else "必填字段"
        message = f"{field_label}缺少必填项“{missing}”，请补充后保存。"
        code = "SIGNAL_FIELD_REQUIRED"
    elif error.validator == "additionalProperties":
        message = f"{field_label}包含当前版本不支持的字段，请重新选择对应类型后保存。"
        code = "SIGNAL_FIELD_UNSUPPORTED"
    elif error.validator in {"type", "anyOf", "oneOf"}:
        message = f"{field_label}的值类型不正确，请重新填写或重新选择对应类型。"
        code = "SIGNAL_FIELD_INVALID"
    else:
        message = f"{field_label}未满足当前关键信号规则，请检查该区域后保存。"
        code = "SIGNAL_FIELD_INVALID"

    issue: dict[str, Any] = {
        "level": "error",
        "code": code,
        "location": f"关键信号 · {signal_id} · {field_label}" if signal_id else field_label,
        "message": message,
    }
    if field_path:
        issue["field_path"] = field_path
    if signal_id:
        issue["signal_id"] = signal_id
        issue["action"] = {
            "type": "edit_signal",
            "signal_id": signal_id,
            "focus": field_path or None,
        }
    return issue


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
    _validate_verification_contract(raw, require_must=True)


def validate_draft_signals_json(raw: Any) -> None:
    """工作稿保存校验。

    工作稿必须始终是结构正确、工具参数可编译且 Contract 不含悬空引用的文档；但
    专家可以暂时移除最后一条必要证据，待补充后再发布。最终发布仍只能调用
    :func:`validate_publishable_signals_json`。
    """

    Draft7Validator(_SIGNAL_V2_SCHEMA, registry=_REGISTRY).validate(raw)
    _validate_qfk_match_or_produces(raw)
    _validate_runtime_acquire_args(raw)
    _validate_verification_contract(raw, require_must=False)


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
    _validate_variable_dependency_graph(raw)


def certify_publishable_signals_json(raw: Any) -> dict[str, Any]:
    """用当前代码契约校验并生成发布盖章，不覆盖 LLM 原始生成元数据。

    ``generation_metadata.tool_contract_revision`` 记录 Proposal 生成时使用的契约，
    属于不可变的生产追溯事实；专家发布时若直接覆盖它，后续就无法评估旧模型与新
    契约的差异。``publish_validation`` 单独记录本次 Expert 内容已经通过当前静态
    Schema、参数语义和发布门禁。发布是专家对当前整份 Signal 文档的确认，因此也会
    将逐条 Signal 的待复核标记归档为已人工复核。Agent 仍会在消费侧编译真实
    Handler 与变量 DAG。
    """

    certified = copy.deepcopy(raw)
    # 先验证专家提交的原始文档，避免盖章字段掩盖任何输入错误。
    validate_publishable_signals_json(certified)
    certified["publish_validation"] = {
        "schema_version": 1,
        "status": "passed",
        "tool_contract_revision": current_tool_contract_revision(),
        "validator": "expert_publish_gate",
    }
    for signal in certified.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        provenance = signal.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
            signal["provenance"] = provenance
        # needs_review 是当前版本的待办状态，不能在专家审核并发布后继续保留为 true。
        # 原始模型来源、证据和风险字段均不改变，仍可在 Revision 中完整追溯。
        provenance["needs_review"] = False
        review = signal.get("review")
        if not isinstance(review, dict):
            review = {}
            signal["review"] = review
        review["require_human_confirm"] = False
    # 再验证最终持久化形态，确保 Schema 与代码没有发生自相矛盾。
    validate_publishable_signals_json(certified)
    return certified


def _validate_variable_dependency_graph(raw: dict[str, Any]) -> None:
    """发布前验证 diagnostic 变量链可达，避免 Admin 成功而 Agent 编译失败。"""

    contract = raw.get("verification_contract") or {}
    declared_external = {
        str(name).strip().upper()
        for name in (contract.get("variables") or {})
        if str(name).strip()
    }
    nodes: list[tuple[str, set[str], set[str]]] = []
    for index, signal in enumerate(raw.get("signals") or [], start=1):
        if not isinstance(signal, dict):
            continue
        orchestrate = signal.get("orchestrate") or {}
        if str(orchestrate.get("phase") or "diagnostic") == "solution":
            continue
        signal_id = str(signal.get("id") or f"signal_{index:03d}")
        requires = {
            str(name).strip().upper()
            for name in (orchestrate.get("requires") or [])
            if str(name).strip()
        }
        produces = {
            str(item.get("name") if isinstance(item, dict) else item).strip().upper()
            for item in (orchestrate.get("produces") or [])
            if str(item.get("name") if isinstance(item, dict) else item).strip()
        }
        nodes.append((signal_id, requires, produces))

    all_produced = {name for _, _, produces in nodes for name in produces}
    if not contract:
        # 无 Verification Contract 的历史数据兼容运行时 env_context；首次专家保存
        # 会补全 Contract，之后即进入严格外部变量声明。
        declared_external.update(
            name for _, requires, _ in nodes for name in requires if name not in all_produced
        )
    undeclared = {
        name
        for _, requires, _ in nodes
        for name in requires
        if name not in all_produced and name not in declared_external
    }
    if undeclared:
        raise ValidationError(f"输入变量没有上游产出或外部声明: {', '.join(sorted(undeclared))}")

    available = set(declared_external)
    remaining = list(nodes)
    while remaining:
        ready = [node for node in remaining if node[1].issubset(available)]
        if not ready:
            blocked = ", ".join(f"{signal_id} 需要 {sorted(requires)}" for signal_id, requires, _ in remaining)
            raise ValidationError(f"关键信号变量依赖存在环或不可达: {blocked}")
        for node in ready:
            available.update(node[2])
            remaining.remove(node)


def _validate_verification_contract(raw: Any, *, require_must: bool) -> None:
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
    if require_must and signals and not (policy.get("must") or []):
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
        if isinstance(matcher, dict):
            if not isinstance(matcher.get("extract"), dict):
                raise ValidationError(f"signals[{index}].match 必须配置新版 extract")
            _validate_value_extract(
                matcher["extract"],
                location=f"signals[{index}].match.extract",
                consumer_type="matcher",
            )
        for produce_index, produce in enumerate(produces):
            if not isinstance(produce, dict) or not isinstance(produce.get("extract"), dict):
                raise ValidationError(
                    f"signals[{index}].orchestrate.produces[{produce_index}] 必须配置新版 extract"
                )
            _validate_value_extract(
                produce["extract"],
                location=f"signals[{index}].orchestrate.produces[{produce_index}].extract",
                consumer_type=str(produce.get("type") or "string"),
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
            elif not (
                args.get("request_id")
                or any(
                    _value_extract_has_bounded_include(item.get("extract"))
                    for item in produces
                    if isinstance(item, dict)
                )
            ):
                raise ValidationError(
                    f"signals[{index}] 的 qfk_log 产出变量采集必须在完整行/文本取值中配置非空包含关键字，"
                    "或提供 request_id；仅排除关键字不能限制输出范围"
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


def _validate_value_extract(
    extract: dict[str, Any],
    *,
    location: str,
    consumer_type: str,
) -> None:
    """补充 JSON Schema 不便表达的 Extract 跨字段约束。"""

    extract_type = str(extract.get("type") or "")
    if extract_type == "json":
        cardinality = str(extract.get("cardinality") or "exactly_one")
        if consumer_type == "object" and cardinality == "all":
            raise ValidationError(f"{location} 的 object 产出不能使用 cardinality=all")
        if consumer_type == "array<object>" and cardinality != "all":
            raise ValidationError(f"{location} 的 array<object> 产出必须使用 cardinality=all")
        return
    if extract_type != "text":
        raise ValidationError(f"{location}.type 仅支持 text/json")

    columns = extract.get("columns")
    rows = extract.get("rows")
    if not isinstance(rows, dict):
        raise ValidationError(f"{location} 的 text extract 必须配置 rows")
    if rows.get("mode") == "indices" and rows.get("basis") == "data" and not isinstance(extract.get("header"), dict):
        raise ValidationError(f"{location} 使用 basis=data 时必须配置 header")
    if rows.get("mode") == "keywords" and str(rows.get("scope") or "same_record") != "same_record":
        raise ValidationError(f"{location}.rows.scope 仅支持 same_record")
    if not isinstance(columns, list):
        if consumer_type in {"object", "array<object>"}:
            raise ValidationError(f"{location} 的复合变量必须配置结构化 columns")
        return

    keys = [str(column.get("key") or "") for column in columns if isinstance(column, dict)]
    if len(keys) != len(set(keys)):
        raise ValidationError(f"{location}.columns 的 key 必须唯一")
    value_key = str(extract.get("value_key") or "")
    if value_key and value_key not in keys:
        raise ValidationError(f"{location}.value_key={value_key} 不属于 columns key")
    if len(columns) > 1 and consumer_type not in {"object", "array<object>"} and not value_key:
        raise ValidationError(f"{location} 的多列标量/Matcher 消费必须显式配置 value_key")

    selectors = [
        column.get("selector")
        for column in columns
        if isinstance(column, dict) and isinstance(column.get("selector"), dict)
    ]
    if any(selector.get("by") == "header" for selector in selectors) and not isinstance(extract.get("header"), dict):
        raise ValidationError(f"{location} 按表头选列时必须配置 header")
    for row_range in rows.get("ranges") or []:
        if isinstance(row_range, dict) and int(row_range.get("start") or 0) > int(row_range.get("end") or 0):
            raise ValidationError(f"{location}.rows.ranges 的 start 不能大于 end")

    cardinality = str(extract.get("cardinality") or "exactly_one")
    if consumer_type == "object" and cardinality == "all":
        raise ValidationError(f"{location} 的 object 产出不能使用 cardinality=all")
    if consumer_type == "array<object>" and cardinality != "all":
        raise ValidationError(f"{location} 的 array<object> 产出必须使用 cardinality=all")


def _value_extract_has_bounded_include(extract: Any) -> bool:
    """qfk_log 有界性只认可逐记录的非空 include，exclude 单独存在不缩小上界。"""

    if not isinstance(extract, dict) or extract.get("type") != "text":
        return False
    rows = extract.get("rows")
    if not isinstance(rows, dict) or rows.get("mode") != "keywords":
        return False
    include = rows.get("include")
    return isinstance(include, list) and any(isinstance(item, str) and item.strip() for item in include)
