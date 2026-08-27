"""QKV 具体值的后处理适配器。

QKV 与 QFK 的差异只有输入边界：QKV 在进入本模块前已经通过
``orchestrate.produces[].path`` 从 JSON 投影出具体值。因此本模块不再复制
QFK 的 JSON 路径、字符串归一化、比较或 AI 逻辑，而是：

* 派生变量：仅实现 QKV 特有的 ``feature``/``split``，类型和基数复用 QFK
  ``extract_value``；
* 断言判断：把具体值适配成受控的 identity 文本，直接调用 QFK
  ``evaluate_matcher``；
* AI 兜底：直接调用 QFK ``extract_ai_value``，仍要求原文 evidence 逐字回查。

禁止 import ``app.*``，保证共享 Schema、在线 Agent、hci-sim 和离线回放使用
同一套确定性语义。配置中的处理 ID 不属于 QKV 业务契约；运行时仅用数组下标
定位处理单元，避免 QKV 独创一套与 QFK 不一致的身份模型。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from shared.signals.ai_processing import ai_processing_config, validate_ai_processing_config
from shared.signals.extractor import QFKExtractionError, extract_value
from shared.signals.matcher import evaluate_matcher


class QKVProcessingError(ValueError):
    """带稳定错误码的 QKV 后处理错误。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class QKVAssertion:
    """一次处理单元断言结果；unit_index 是数组位置，不是用户配置字段。"""

    unit_index: int
    status: str
    observed: Any = None
    reason: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class QKVProcessingResult:
    """后处理后的记录和断言汇总。"""

    records: list[dict[str, Any]]
    assertions: list[QKVAssertion] = field(default_factory=list)

    @property
    def matched(self) -> bool | None:
        if not self.assertions:
            return None
        statuses = {item.status for item in self.assertions}
        if "ERROR" in statuses or "UNKNOWN" in statuses:
            return None
        return "FAIL" not in statuses


_PLACEHOLDER_RE = re.compile(r"^\{\{([A-Za-z][A-Za-z0-9_.]*)\}\}$")
_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9])([0-9]+(?:\.[0-9]+)?)\s*%")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?[0-9]+(?:\.[0-9]+)?")
_VM_NAME_PATTERNS = (
    re.compile(r"(?:虚拟机|VM)\s*(?:名称|名|name)?\s*[（(\[【]?\s*[:：=]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.I),
    re.compile(r"(?:vm[_ -]?name)\s*[=:：]\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.I),
)
_HOST_PATTERNS = (
    re.compile(r"(?:主机|宿主机|host)\s*[（(\[【]?\s*[:：=]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.I),
)
_SUPPORTED_FEATURES = {
    "vm_name", "vm", "host", "host_name", "disk_name", "interface_name", "error_code",
    "source_host", "destination_host", "change_pair", "percent.current", "percentage", "percent", "number",
}
_FEATURE_PATTERNS = {
    "disk_name": re.compile(r"(?:磁盘|disk)\s*(?:名称|名|name)?\s*[:：=]\s*([A-Za-z0-9][A-Za-z0-9._/-]*)", re.I),
    "interface_name": re.compile(r"(?:网口|接口|interface)\s*(?:名称|名|name)?\s*[:：=]\s*([A-Za-z0-9][A-Za-z0-9._:-]*)", re.I),
    "error_code": re.compile(r"(?:错误码|错误代码|error[_ -]?code|code)\s*[:：=]\s*([A-Za-z0-9._-]+)", re.I),
    "source_host": re.compile(r"(?:源主机|source[_ -]?host)\s*[:：=]\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.I),
    "destination_host": re.compile(r"(?:目标主机|目的主机|destination[_ -]?host)\s*[:：=]\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.I),
    "change_pair": re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:->|→|至|到)\s*([A-Za-z0-9][A-Za-z0-9._-]*)"),
}


def processing_input_variables(specs: Any) -> set[str]:
    """返回后处理 input 和 Matcher 阈值中的变量引用。"""

    if not isinstance(specs, list):
        return set()
    variables: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, str):
            variables.update(
                match.group(1).split(".")[0].upper()
                for match in re.finditer(r"\{\{([A-Za-z][A-Za-z0-9_.]*)\}\}", value)
            )
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)

    for spec in specs:
        if isinstance(spec, dict):
            collect(spec.get("input"))
            if spec.get("mode") == "assert":
                collect(spec.get("match"))
    return variables


def _canonicalize(spec: dict[str, Any], index: int) -> dict[str, Any]:
    """把历史 QKV 形态归一到 QFK 词汇；新保存数据不得再生成历史字段。"""

    item = copy.deepcopy(spec)
    operation = str(item.get("operation") or "")
    if operation in {"json_path", "trim", "lower", "upper"}:
        if operation == "trim" and "id" in item:
            # 仅兼容已发布历史快照；新 Schema/UI 不再生成该形态。
            item["extract"] = {"type": "identity", "cardinality": "exactly_one", "_legacy": True}
            for key in ("operation", "target_variable", "value_type", "cardinality", "fallback"):
                item.pop(key, None)
            operation = ""
        else:
            raise QKVProcessingError(
                "QKV_PROCESSING_REDUNDANT_OPERATION",
                f"处理单元[{index + 1}] 的 {operation} 不适用于 QKV：produces.path 已经得到具体值",
            )
    if operation:
        # 只对已发布的 feature/split/compare 做无损兼容；其他旧 operation 拒绝。
        if operation == "feature_extract":
            item["extract"] = {
                "type": "feature",
                "feature": item.get("feature"),
                "cardinality": item.get("cardinality", "exactly_one"),
                "ai_processing": item.get("ai_processing") or (ai_processing_config({"ai_extract": item.get("ai_extract")}) if item.get("ai_extract") else None),
            }
            for key in ("operation", "feature", "cardinality", "fallback", "value_type", "target_variable"):
                if key != "target_variable":
                    item.pop(key, None)
        elif operation == "split":
            item["extract"] = {
                "type": "split",
                "separator": item.get("separator"),
                "cardinality": item.get("cardinality", "all"),
                "ai_processing": item.get("ai_processing") or (ai_processing_config({"ai_extract": item.get("ai_extract")}) if item.get("ai_extract") else None),
            }
            for key in ("operation", "separator", "cardinality", "fallback", "value_type", "target_variable"):
                item.pop(key, None)
        elif operation == "compare":
            right = item.get("right")
            if isinstance(right, str):
                percent = _PERCENT_RE.search(right)
                if percent:
                    right = float(percent.group(1))
            item["match"] = {
                "type": "threshold",
                "operator": item.get("operator"),
                "value": right,
                "expected": True,
            }
            for key in ("operation", "right", "operator", "value_type", "cardinality", "fallback", "target_variable"):
                item.pop(key, None)
        else:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] 不支持 operation={operation}")
    item.pop("id", None)
    if item.get("mode") == "derive":
        if "name" not in item and item.get("target_variable") is not None:
            item["name"] = item.pop("target_variable")
        if "type" not in item and item.get("value_type") is not None:
            item["type"] = item.pop("value_type")
    return item


def normalize_output_processing(specs: Any) -> list[dict[str, Any]]:
    if specs in (None, []):
        return []
    if not isinstance(specs, list) or not specs:
        raise QKVProcessingError("QKV_PROCESSING_INVALID", "output_processing 必须是非空数组")
    return [_canonicalize(item, index) if isinstance(item, dict) else item for index, item in enumerate(specs)]


def processing_derived_variables(specs: Any) -> set[str]:
    """返回后处理产生的变量名，供依赖图、CDD 和 hci-sim 共用。"""

    try:
        normalized = normalize_output_processing(specs)
    except QKVProcessingError:
        normalized = specs if isinstance(specs, list) else []
    return {
        str(item.get("name") or "").strip().upper()
        for item in normalized
        if isinstance(item, dict) and item.get("mode") == "derive" and str(item.get("name") or "").strip()
    }


def _identity_extract(value_type: str) -> dict[str, Any]:
    normalized = "number" if value_type == "percentage" else value_type
    if normalized not in {"string", "integer", "number", "boolean", "array"}:
        normalized = "string"
    return {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "value_mode": normalized,
    }


def _value_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(_value_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def _validate_match(match: Any, index: int) -> None:
    if not isinstance(match, dict):
        raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] assert 必须配置 QFK match")
    matcher_type = str(match.get("type") or "")
    if matcher_type not in {"keyword", "regex", "state", "threshold", "delta", "trend", "exists"}:
        raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] match.type 不受支持")
    if matcher_type in {"keyword", "regex", "state"} and not match.get("pattern"):
        raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] match.pattern 必填")
    if matcher_type in {"threshold", "delta"} and (match.get("value") is None or not match.get("operator")):
        raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] 数值 Matcher 必须配置 value/operator")
    if matcher_type == "trend" and not match.get("direction"):
        raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] trend 必须配置 direction")


def validate_output_processing(specs: Any, *, available_inputs: set[str] | None = None) -> None:
    """校验 QKV 新契约；输入值已经具体化，不再接受 JSON 路径/归一化操作。"""

    normalized = normalize_output_processing(specs)
    available = {str(item).strip().upper() for item in (available_inputs or set()) if str(item).strip()}
    derived: set[str] = set()
    for index, item in enumerate(normalized):
        if not isinstance(item, dict):
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] 必须是对象")
        allowed = {"mode", "input", "name", "type", "scope", "extract", "match", "_legacy"}
        unknown = set(item) - allowed
        if unknown:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] 含未注册字段: {sorted(unknown)}")
        if item.get("mode") not in {"derive", "assert"} or not isinstance(item.get("input"), str) or not item["input"].strip():
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] 必须配置 mode 和 input")
        referenced = processing_input_variables([item])
        unknown_inputs = referenced - available - derived
        if available_inputs is not None and unknown_inputs:
            raise QKVProcessingError(
                "QKV_PROCESSING_UNKNOWN_INPUT",
                f"处理单元[{index + 1}] input 引用了未声明变量: {', '.join(sorted(unknown_inputs))}",
            )
        scope = str(item.get("scope") or "per_record")
        if scope not in {"per_record", "single"}:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] scope 仅支持 per_record/single")
        if item["mode"] == "derive":
            name = str(item.get("name") or "")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] name 必须是大写变量名")
            value_type = str(item.get("type") or "string")
            if value_type not in {"string", "integer", "number", "percentage", "boolean", "array"}:
                raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] type 不受支持: {value_type}")
            extract = item.get("extract")
            if not isinstance(extract, dict) or extract.get("type") not in {"feature", "split", "identity"}:
                raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] extract.type 仅支持 feature/split")
            cardinality = str(extract.get("cardinality") or ("all" if extract.get("type") == "split" else "exactly_one"))
            if cardinality not in {"exactly_one", "first", "last", "all"}:
                raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] cardinality 必须复用 QFK 的 exactly_one/first/last/all")
            if extract.get("type") == "feature" and str(extract.get("feature") or "") not in _SUPPORTED_FEATURES:
                raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] feature 不受支持")
            if extract.get("type") == "split" and not isinstance(extract.get("separator"), str):
                raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] split 必须配置 separator")
            ai_extract = ai_processing_config(extract)
            if ai_extract is not None:
                try:
                    validate_ai_processing_config(ai_extract)
                except ValueError as exc:
                    raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] AI 处理无效: {exc}") from exc
                derived.add(name)
        else:
            if set(item) - {"mode", "input", "scope", "match"}:
                raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index + 1}] assert 只能包含 input/scope/match")
            _validate_match(item.get("match"), index)


def _feature_extract(text: str, feature: str) -> list[Any]:
    if feature in {"percent.current", "percentage", "percent"}:
        return [float(match.group(1)) for match in _PERCENT_RE.finditer(text)]
    if feature == "number":
        return [float(match.group(0)) for match in _NUMBER_RE.finditer(text)]
    patterns = (
        _VM_NAME_PATTERNS if feature in {"vm_name", "vm"}
        else _HOST_PATTERNS if feature in {"host", "host_name"}
        else (_FEATURE_PATTERNS[feature],) if feature in _FEATURE_PATTERNS else ()
    )
    if not patterns:
        raise QKVProcessingError("QKV_FEATURE_UNSUPPORTED", f"不支持 feature={feature}")
    values: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            values.append(match.group(0) if feature == "change_pair" else match.group(1))
    return values


def _derive_values(value: Any, item: dict[str, Any]) -> Any:
    extract = item["extract"]
    source_values = (
        _feature_extract(_value_text(value), str(extract["feature"]))
        if extract["type"] == "feature"
        else [_value_text(value)] if extract["type"] == "identity"
        else _value_text(value).split(str(extract["separator"]))
    )
    cardinality = str(extract.get("cardinality") or ("all" if extract["type"] == "split" else "exactly_one"))
    value_type = str(item.get("type") or "string")
    if cardinality == "exactly_one" and len(source_values) != 1:
        raise QKVProcessingError("QFK_CARDINALITY_MISMATCH", f"期望唯一结果，实际 {len(source_values)} 条")
    if not source_values:
        raise QKVProcessingError("QFK_NO_MATCH", "QKV 派生没有提取到值")
    selected = source_values if cardinality == "all" else [source_values[-1] if cardinality == "last" else source_values[0]]
    try:
        extraction_cardinality = "all" if cardinality == "all" else "first"
        return extract_value(
            "\n".join(_value_text(item_value) for item_value in selected),
            _identity_extract(value_type) | {"cardinality": extraction_cardinality},
            "number" if value_type in {"percentage", "number"} else value_type,
        )
    except QFKExtractionError as exc:
        raise QKVProcessingError(exc.code, exc.message) from exc


def _assert_concrete_value(value: Any, match: dict[str, Any]) -> tuple[bool | None, Any, str, str]:
    matcher = copy.deepcopy(match)
    matcher.pop("extract", None)
    matcher_type = str(matcher.get("type") or "")
    value_type = "number" if matcher_type in {"threshold", "delta", "trend"} else "string"
    text = _value_text(value)
    # 历史 compare 载荷直接把 DESCRIPTION 作为数值输入；仅为读取旧快照保留
    # 这个兼容适配，新契约要求先用 feature=percent.current 派生具体数值。
    if matcher_type in {"threshold", "delta", "trend"} and not re.fullmatch(r"\s*[+-]?\d+(?:\.\d+)?\s*%?\s*", text):
        numbers = [match.group(1) for match in _PERCENT_RE.finditer(text)] or [match.group(0) for match in _NUMBER_RE.finditer(text)]
        text = "\n".join(numbers)
    result = evaluate_matcher(matcher | {"extract": _identity_extract(value_type)}, text)
    observed = result.detail.get("value", result.detail.get("extract", {}).get("values", value))
    if result.matched is None:
        return None, observed, "QFK_MATCHER_INCONCLUSIVE", result.evidence
    return bool(result.matched), observed, "", result.evidence


def apply_output_processing(records: list[dict[str, Any]], specs: list[dict[str, Any]] | None) -> QKVProcessingResult:
    """执行无 AI 的 QKV 后处理；所有通用判断/类型/基数逻辑来自 QFK。"""

    normalized = normalize_output_processing(specs)
    validate_output_processing(normalized)
    if not normalized:
        return QKVProcessingResult(records=[dict(record) for record in records])
    working = [dict(record) for record in records]
    assertions: list[QKVAssertion] = []
    if not working:
        for index, item in enumerate(normalized):
            if item.get("mode") == "assert":
                assertions.append(QKVAssertion(index + 1, "UNKNOWN", reason="QFK_OUTPUT_EMPTY"))
            else:
                raise QKVProcessingError("QFK_OUTPUT_EMPTY", f"处理单元[{index + 1}] 没有可处理的 QKV 记录")
        return QKVProcessingResult(records=working, assertions=assertions)
    for index, item in enumerate(normalized):
        if item.get("scope", "per_record") == "single" and len(working) != 1:
            if item["mode"] == "assert":
                assertions.append(QKVAssertion(index + 1, "UNKNOWN", reason="QFK_CARDINALITY_MISMATCH"))
                continue
            raise QKVProcessingError("QFK_CARDINALITY_MISMATCH", f"处理单元[{index + 1}] 要求恰好一条记录，实际 {len(working)} 条")
        staged: list[tuple[dict[str, Any], Any]] = []
        for record in working:
            value = _resolve_input(item["input"], record)
            if item["mode"] == "derive":
                try:
                    derived = _derive_values(value, item)
                except QKVProcessingError:
                    raise
                staged.append((record, derived))
            else:
                matched, observed, reason, evidence = _assert_concrete_value(value, item["match"])
                assertions.append(QKVAssertion(index + 1, "UNKNOWN" if matched is None else ("PASS" if matched else "FAIL"), observed, reason, evidence))
        if item["mode"] == "derive":
            for record, derived in staged:
                record[str(item["name"]).lower()] = derived
    return QKVProcessingResult(records=working, assertions=assertions)


async def apply_output_processing_async(
    records: list[dict[str, Any]],
    specs: list[dict[str, Any]] | None,
    *,
    ai_client: Any | None = None,
    ai_extractor: Any | None = None,
    conversation_id: str = "",
    case_id: str = "",
    db_session_factory: Any | None = None,
) -> QKVProcessingResult:
    """QKV 后处理异步入口：确定性取值后再执行可选 AI 后处理。"""

    normalized = normalize_output_processing(specs)
    validate_output_processing(normalized)
    if not normalized or not any(item.get("mode") == "derive" and ai_processing_config(item.get("extract")) for item in normalized):
        return apply_output_processing(records, normalized)
    working = [dict(record) for record in records]
    assertions: list[QKVAssertion] = []
    if not working:
        for index, item in enumerate(normalized):
            if item.get("mode") == "assert":
                assertions.append(QKVAssertion(index + 1, "UNKNOWN", reason="QFK_OUTPUT_EMPTY"))
            else:
                raise QKVProcessingError("QFK_OUTPUT_EMPTY", f"处理单元[{index + 1}] 没有可处理的 QKV 记录")
        return QKVProcessingResult(records=working, assertions=assertions)
    for index, item in enumerate(normalized):
        if item.get("scope", "per_record") == "single" and len(working) != 1:
            raise QKVProcessingError("QFK_CARDINALITY_MISMATCH", f"处理单元[{index + 1}] 要求恰好一条记录，实际 {len(working)} 条")
        staged: list[tuple[dict[str, Any], Any]] = []
        for record in working:
            value = _resolve_input(item["input"], record)
            if item["mode"] == "assert":
                matched, observed, reason, evidence = _assert_concrete_value(value, item["match"])
                assertions.append(QKVAssertion(index + 1, "UNKNOWN" if matched is None else ("PASS" if matched else "FAIL"), observed, reason, evidence))
                continue
            try:
                # AI 的输入只能是确定性取值阶段已经产出的值，不能绕过前一步
                # 读取原始记录，也不能在确定性失败时充当兜底。
                derived = _derive_values(value, item)
            except QKVProcessingError:
                raise
            ai_config = ai_processing_config(item.get("extract"))
            if ai_config is not None:
                derived = await _derive_with_ai(
                    derived,
                    ai_config,
                    str(item.get("type") or "string"),
                    ai_client,
                    ai_extractor,
                    conversation_id,
                    case_id,
                    db_session_factory=db_session_factory,
                )
            staged.append((record, derived))
        if item["mode"] == "derive":
            for record, derived in staged:
                record[str(item["name"]).lower()] = derived
    return QKVProcessingResult(records=working, assertions=assertions)


async def _derive_with_ai(
    value: Any,
    ai_config: dict[str, Any] | None,
    value_type: str,
    ai_client: Any | None,
    ai_extractor: Any | None,
    conversation_id: str,
    case_id: str,
    db_session_factory: Any | None = None,
    deterministic_error: QKVProcessingError | None = None,
) -> Any:
    """执行统一 AI 后处理；原文取值与智能推导共用同一契约。"""

    if not ai_config:
        raise deterministic_error or QKVProcessingError("QKV_PROCESSING_INVALID", "QKV AI 处理缺少配置")
    if ai_client is None:
        raise QKVProcessingError("QFK_AI_EXTRACT_UNAVAILABLE", "QKV AI 处理客户端不可用") from deterministic_error
    if ai_extractor is None:
        raise QKVProcessingError("QFK_AI_EXTRACT_UNAVAILABLE", "QKV AI 提取器不可用") from deterministic_error
    ai_spec = _identity_extract(value_type) | {"ai_processing": ai_config}
    try:
        ai_result = await ai_extractor(
            _value_text(value),
            ai_spec,
            "array" if value_type == "array" else ("number" if value_type == "percentage" else value_type),
            ai_client,
            conversation_id=conversation_id,
            case_id=case_id,
            db_session_factory=db_session_factory,
        )
    except QFKExtractionError as exc:
        raise QKVProcessingError(exc.code, exc.message) from exc
    return ai_result.value


def _resolve_input(template: str, record: dict[str, Any]) -> Any:
    match = _PLACEHOLDER_RE.fullmatch(template.strip())
    if match:
        return _lookup(record, match.group(1))
    return template


def _lookup(record: Any, name: str) -> Any:
    current: Any = record
    for part in name.split("."):
        if isinstance(current, dict):
            key = next((candidate for candidate in current if str(candidate).casefold() == part.casefold()), None)
            if key is None:
                return None
            current = current[key]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current
