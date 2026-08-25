"""QKV 输出后处理的共享确定性内核。

该模块只处理已经由 QKV 解析器投影出的记录，不执行命令、不访问外部变量池、
不调用模型。它被 Agent 运行时和契约测试共同使用，保证配置校验与实际求值口径一致。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


class QKVProcessingError(ValueError):
    """带稳定错误码的 QKV 后处理错误。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class QKVAssertion:
    """一次后处理断言结果。"""

    processing_id: str
    status: str
    observed: Any = None
    reason: str = ""


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
        if "ERROR" in statuses:
            return None
        if "FAIL" in statuses:
            return False
        if "UNKNOWN" in statuses:
            return None
        return True


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
    """返回后处理 input 中引用的变量名（统一为大写）。"""

    if not isinstance(specs, list):
        return set()
    variables: set[str] = set()
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        value = spec.get("input")
        if not isinstance(value, str):
            continue
        variables.update(match.group(1).split(".")[0].upper() for match in re.finditer(r"\{\{([A-Za-z][A-Za-z0-9_.]*)\}\}", value))
    return variables


def validate_output_processing(specs: Any, *, available_inputs: set[str] | None = None) -> None:
    """校验 QKV 后处理静态契约，拒绝未知操作和脚本化字段。"""

    if specs in (None, []):
        return
    if not isinstance(specs, list) or not specs:
        raise QKVProcessingError("QKV_PROCESSING_INVALID", "output_processing 必须是非空数组")
    seen: set[str] = set()
    allowed = {
        "id", "mode", "input", "operation", "target_variable", "value_type", "cardinality", "scope",
        "feature", "path", "separator", "operator", "right", "fallback",
    }
    available = {str(item).strip().upper() for item in (available_inputs or set()) if str(item).strip()}
    derived: set[str] = set()
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index}] 必须是对象")
        unknown = set(spec) - allowed
        if unknown:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index}] 含未注册字段: {sorted(unknown)}")
        processing_id = str(spec.get("id") or "").strip()
        if not processing_id or processing_id in seen:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"处理单元[{index}] id 必须唯一且非空")
        seen.add(processing_id)
        mode = str(spec.get("mode") or "")
        operation = str(spec.get("operation") or "")
        if mode not in {"derive", "assert"}:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.mode 仅支持 derive/assert")
        if operation not in {"json_path", "trim", "lower", "upper", "split", "compare", "feature_extract"}:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.operation 不受支持: {operation}")
        if not isinstance(spec.get("input"), str) or not str(spec["input"]).strip():
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.input 必须是非空字符串")
        referenced = processing_input_variables([spec])
        unknown = referenced - available - derived
        if available_inputs is not None and unknown:
            raise QKVProcessingError(
                "QKV_PROCESSING_UNKNOWN_INPUT",
                f"{processing_id}.input 引用了未声明变量: {', '.join(sorted(unknown))}",
            )
        if mode == "derive":
            target = str(spec.get("target_variable") or "")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", target):
                raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.target_variable 必须是大写变量名")
        elif spec.get("target_variable") is not None:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id} assert 不得配置 target_variable")
        cardinality = str(spec.get("cardinality") or "exactly_one")
        if cardinality not in {"exactly_one", "zero_or_more", "all"}:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.cardinality 不受支持")
        scope = str(spec.get("scope") or "per_record")
        if scope not in {"per_record", "single"}:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.scope 仅支持 per_record/single")
        value_type = str(spec.get("value_type") or "string")
        if value_type not in {"string", "integer", "number", "percentage"}:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.value_type 不受支持: {value_type}")
        if operation == "split" and not isinstance(spec.get("separator"), str):
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.split 必须配置 separator")
        if operation == "json_path" and (
            not isinstance(spec.get("path"), str) or not str(spec.get("path")).strip()
        ):
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.json_path 必须配置 path")
        if operation == "compare" and str(spec.get("operator") or "") not in {">", ">=", "<", "<=", "==", "=", "!="}:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.compare.operator 不受支持")
        if operation == "compare" and spec.get("right") is None:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.compare 必须配置 right")
        if operation == "feature_extract" and not str(spec.get("feature") or "").strip():
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.feature_extract 必须配置 feature")
        if operation == "feature_extract" and str(spec.get("feature")) not in _SUPPORTED_FEATURES:
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.feature 不受支持")
        if mode == "assert" and operation != "compare":
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id} assert 仅支持 compare")
        if mode == "derive" and operation == "compare":
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id} compare 仅支持 assert")
        if spec.get("fallback") not in (None, "none", "ai_extract"):
            raise QKVProcessingError("QKV_PROCESSING_INVALID", f"{processing_id}.fallback 仅支持 none/ai_extract")
        if mode == "derive":
            derived.add(str(spec["target_variable"]).upper())


def apply_output_processing(records: list[dict[str, Any]], specs: list[dict[str, Any]] | None) -> QKVProcessingResult:
    """对 QKV 投影记录执行后处理；任何派生失败都不返回部分派生结果。"""

    validate_output_processing(specs)
    if not specs:
        return QKVProcessingResult(records=[dict(record) for record in records])
    working = [dict(record) for record in records]
    assertions: list[QKVAssertion] = []
    for spec in specs:
        processing_id = str(spec["id"])
        if spec.get("scope", "per_record") == "single" and len(working) != 1:
            if spec.get("mode") == "assert":
                assertions.append(QKVAssertion(processing_id, "UNKNOWN", reason="QKV_CARDINALITY_MISMATCH"))
                continue
            raise QKVProcessingError("QKV_CARDINALITY_MISMATCH", f"{processing_id} 要求恰好一条 QKV 记录，实际 {len(working)} 条")
        if not working:
            if spec.get("mode") == "assert":
                assertions.append(QKVAssertion(processing_id, "UNKNOWN", reason="QKV_OUTPUT_EMPTY"))
                continue
            raise QKVProcessingError("QKV_OUTPUT_EMPTY", f"{processing_id} 没有可处理的 QKV 记录")
        staged: list[tuple[dict[str, Any], Any]] = []
        for record in working:
            value = _resolve_input(spec["input"], record)
            try:
                result = _operate(value, spec)
            except QKVProcessingError as exc:
                if spec.get("mode") == "assert":
                    assertions.append(QKVAssertion(processing_id, "UNKNOWN", observed=value, reason=exc.code))
                    continue
                raise
            values = result if isinstance(result, list) else [result]
            cardinality = spec.get("cardinality", "exactly_one")
            if cardinality == "exactly_one" and len(values) != 1:
                if spec.get("mode") == "assert":
                    assertions.append(QKVAssertion(processing_id, "UNKNOWN", observed=values, reason="QKV_CARDINALITY_MISMATCH"))
                    continue
                raise QKVProcessingError("QKV_CARDINALITY_MISMATCH", f"{processing_id} 期望一个值，实际 {len(values)} 个")
            if cardinality == "zero_or_more" and len(values) > 1:
                values = values
            if spec.get("mode") == "assert":
                observed = values[0] if len(values) == 1 else values
                passed = _assert_value(observed, spec)
                assertions.append(QKVAssertion(processing_id, "PASS" if passed else "FAIL", observed=observed))
            else:
                staged.append((record, values[0] if cardinality == "exactly_one" and values else values))
        if spec.get("mode") == "derive":
            for record, value in staged:
                record[str(spec["target_variable"]).lower()] = value
    return QKVProcessingResult(records=working, assertions=assertions)


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
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                return None
            current = current[index]
            continue
        return None
    return current


def _operate(value: Any, spec: dict[str, Any]) -> Any:
    operation = spec["operation"]
    if operation == "json_path":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise QKVProcessingError("QKV_JSON_PATH_INVALID", "json_path 输入不是 JSON") from exc
        result = _lookup(value, str(spec["path"]))
        if result is None:
            raise QKVProcessingError("QKV_JSON_PATH_MISSING", f"json_path 未找到 {spec['path']}")
        return result
    if operation == "trim":
        return str(value or "").strip()
    if operation == "lower":
        return str(value or "").lower()
    if operation == "upper":
        return str(value or "").upper()
    if operation == "split":
        return str(value or "").split(str(spec["separator"]))
    if operation == "compare":
        return _coerce(value, str(spec.get("value_type") or "string"))
    if operation == "feature_extract":
        return _feature_extract(str(value or ""), str(spec["feature"]))
    raise QKVProcessingError("QKV_PROCESSING_INVALID", f"未知 operation={operation}")


def _feature_extract(text: str, feature: str) -> list[Any]:
    if feature in {"percent.current", "percentage", "percent"}:
        return [float(match.group(1)) for match in _PERCENT_RE.finditer(text)]
    if feature == "number":
        return [float(match.group(0)) for match in _NUMBER_RE.finditer(text)]
    patterns = (
        _VM_NAME_PATTERNS
        if feature in {"vm_name", "vm"}
        else _HOST_PATTERNS
        if feature in {"host", "host_name"}
        else (_FEATURE_PATTERNS[feature],)
        if feature in _FEATURE_PATTERNS
        else ()
    )
    if patterns:
        values: list[str] = []
        for pattern in patterns:
            for match in pattern.finditer(text):
                values.append(match.group(0) if feature == "change_pair" else match.group(1))
        return values
    raise QKVProcessingError("QKV_FEATURE_UNSUPPORTED", f"不支持 feature={feature}")


def _coerce(value: Any, value_type: str) -> Any:
    if value_type == "percentage":
        if isinstance(value, str):
            match = _PERCENT_RE.search(value)
            if not match:
                raise QKVProcessingError("QKV_TYPE_ERROR", f"无法把 {value!r} 转为 percentage")
            return float(match.group(1))
        return float(value)
    if value_type in {"number", "integer"}:
        try:
            number = float(value)
            return int(number) if value_type == "integer" else number
        except (TypeError, ValueError) as exc:
            raise QKVProcessingError("QKV_TYPE_ERROR", f"无法把 {value!r} 转为 {value_type}") from exc
    return str(value)


def _assert_value(value: Any, spec: dict[str, Any]) -> bool:
    right = _coerce(spec.get("right"), str(spec.get("value_type") or "string"))
    left = _coerce(value, str(spec.get("value_type") or "string"))
    operator = spec["operator"]
    if operator in {"==", "="}:
        return left == right
    if operator == "!=":
        return left != right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    raise QKVProcessingError("QKV_PROCESSING_INVALID", f"不支持 operator={operator}")
