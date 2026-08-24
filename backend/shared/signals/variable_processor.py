"""qfk_var 的确定性变量处理内核。

description 提取严格按三层确定性流水线执行：候选值提取、变量命名和归一化、
类型与基数校验。第四层 AI 兜底由 agent-service 调用现有受控 AI 提取器完成，
本模块不依赖网络、LLM 或变量池实现。
"""

from __future__ import annotations

import json
import operator
import re
from dataclasses import dataclass, field
from typing import Any

MAX_DESCRIPTION_CHARS = 1024 * 1024

VALUE_TYPES = frozenset({"string", "integer", "number", "percentage", "boolean", "quantity", "object", "array<string>"})
CARDINALITIES = frozenset({"exactly_one", "zero_or_more"})


@dataclass(frozen=True)
class VariableCandidate:
    """第一层输出的原文字面量和证据范围。"""

    raw_value: str
    start: int
    end: int
    feature: str
    context: str = ""
    template_id: str | None = None


@dataclass(frozen=True)
class TargetDefinition:
    """第二、三层共享的目标变量契约。"""

    value_type: str
    cardinality: str = "exactly_one"
    labels: tuple[str, ...] = ()
    template_id: str = ""


@dataclass(frozen=True)
class VariableProcessResult:
    """qfk_var 的可审计结果。"""

    status: str
    value: Any = None
    matched: bool | None = None
    raw_values: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None
    error: str | None = None
    fallback_reason: str | None = None


TARGET_DEFINITIONS: dict[str, TargetDefinition] = {
    "vm_name": TargetDefinition("string", labels=("虚拟机",), template_id="vm_name.zh.v1"),
    "host": TargetDefinition("string", labels=("主机",), template_id="host.zh.v1"),
    "storage_name": TargetDefinition("string", labels=("存储",), template_id="storage_name.zh.v1"),
    "disk_name": TargetDefinition("string", labels=("硬盘名称", "硬盘", "磁盘"), template_id="disk_name.zh.v1"),
    "interface_name": TargetDefinition("string", labels=("网口",), template_id="interface_name.zh.v1"),
    "percent.current": TargetDefinition(
        "percentage", labels=("当前使用", "使用率"), template_id="percent_current.zh.v1"
    ),
    "percent.threshold": TargetDefinition(
        "percentage", labels=("超过阈值", "超出阈值", "阈值"), template_id="percent_threshold.zh.v1"
    ),
    "memory.used": TargetDefinition("quantity", labels=("已使用", "使用量"), template_id="memory_used.zh.v1"),
    "memory.threshold": TargetDefinition(
        "quantity", labels=("超过阈值", "超出阈值", "阈值"), template_id="memory_threshold.zh.v1"
    ),
    "memory.remaining": TargetDefinition("quantity", labels=("剩余",), template_id="memory_remaining.zh.v1"),
    "error_code": TargetDefinition("string", labels=("错误码",), template_id="error_code.zh.v1"),
    "source_host": TargetDefinition("string", labels=("源主机",), template_id="source_host.zh.v1"),
    "destination_host": TargetDefinition(
        "string", labels=("目的主机", "目标主机"), template_id="destination_host.zh.v1"
    ),
    "change_pair": TargetDefinition("object", labels=("从", "变更为"), template_id="change_pair.zh.v1"),
}

_PAIR_PATTERNS = (
    ("pair_zh_parentheses", re.compile(r"（([^（）\r\n]+)）")),
    ("pair_angle", re.compile(r"<([^<>\r\n]+)>")),
    ("pair_zh_brackets", re.compile(r"【([^【】\r\n]+)】")),
    ("pair_parentheses", re.compile(r"\(([^()\r\n]+)\)")),
    ("pair_brackets", re.compile(r"\[([^\[\]\r\n]+)\]")),
)
_LABEL_VALUE_RE = re.compile(
    r"(?P<label>[\u4e00-\u9fffA-Za-z_][\u4e00-\u9fffA-Za-z0-9_.-]{0,31})\s*[:：]\s*"
    r"(?P<value>[+-]?\d+(?:\.\d+)?\s*%|[+-]?\d+(?:\.\d+)?\s*[A-Za-z]+|"
    r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}|[\u4e00-\u9fff][^，。；;\r\n]{0,255})"
)
_LABEL_NUMBER_RE = re.compile(
    r"(?P<label>[\u4e00-\u9fffA-Za-z_][\u4e00-\u9fffA-Za-z0-9_.-]{0,31})\s*[:：]\s*"
    r"(?P<value>[+-]?\d+(?:\.\d+)?)(?![A-Za-z%])"
)
_PERCENT_RE = re.compile(r"(?<![\w.])[+-]?\d+(?:\.\d+)?\s*%")
_QUANTITY_RE = re.compile(r"(?<![\w.])[+-]?\d+(?:\.\d+)?\s*(?:[KMGTPE]?i?[Bb]|ms|s|min|h)(?!\w)")
_IDENTIFIER_RE = re.compile(
    r"(?<![\w.-])(?=[A-Za-z0-9_.-]*[A-Za-z])(?=[A-Za-z0-9_.-]*\d)[A-Za-z0-9][A-Za-z0-9_.-]{1,255}"
)
_QUANTITY_FULL_RE = re.compile(r"(?P<number>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]+)")
_PERCENTAGE_FULL_RE = re.compile(r"[+-]?\d+(?:\.\d+)?\s*%")
_INTEGER_FULL_RE = re.compile(r"[+-]?\d+")
_NUMBER_FULL_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_PATH_PART_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)]")


class VariableProcessingError(ValueError):
    """携带稳定错误码的变量处理异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _context_before(text: str, start: int, *, size: int = 24) -> str:
    return text[max(0, start - size) : start].strip()


def _candidate_key(candidate: VariableCandidate) -> tuple[int, int, str]:
    return candidate.start, candidate.end, candidate.raw_value


def extract_deterministic_candidates(text: str) -> list[VariableCandidate]:
    """第一层：只发现候选值和原文边界，不决定变量名。"""

    if not isinstance(text, str):
        raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", "feature_extract.input 必须是字符串")
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise VariableProcessingError("QFK_VAR_INPUT_TOO_LARGE", "description 超过 1 MiB 字符预算")

    candidates: dict[tuple[int, int, str], VariableCandidate] = {}
    for feature, pattern in _PAIR_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(1)
            value = match.group(1).strip()
            if value:
                item = VariableCandidate(value, start, end, feature, _context_before(text, match.start()))
                candidates.setdefault(_candidate_key(item), item)

    for match in _LABEL_VALUE_RE.finditer(text):
        start, end = match.span("value")
        value = match.group("value").strip()
        item = VariableCandidate(value, start, end, "label_colon", match.group("label").strip())
        candidates[_candidate_key(item)] = item

    # 冒号后的裸数字也是稳定特征；只在字段标签后采集，避免把日期、IP、版本号
    # 拆成多个伪变量。百分数和带单位数字由专门规则负责。
    for match in _LABEL_NUMBER_RE.finditer(text):
        start, end = match.span("value")
        value = match.group("value").strip()
        item = VariableCandidate(value, start, end, "label_number", match.group("label").strip())
        candidates.setdefault(_candidate_key(item), item)

    for feature, pattern in (("number_with_unit", _QUANTITY_RE), ("number_percentage", _PERCENT_RE)):
        for match in pattern.finditer(text):
            value = match.group(0).strip()
            item = VariableCandidate(value, match.start(), match.end(), feature, _context_before(text, match.start()))
            candidates.setdefault(_candidate_key(item), item)

    for match in _IDENTIFIER_RE.finditer(text):
        value = match.group(0)
        # IP 地址和纯小数由标签、括号或数值规则处理，避免拆成多个伪名称。
        if re.fullmatch(r"\d+(?:\.\d+){1,}", value) or (
            re.fullmatch(r"\d+(?:\.\d+)?", value) and (value.count(".") == 1 or len(value) < 2)
        ):
            continue
        item = VariableCandidate(
            value, match.start(), match.end(), "alpha_numeric", _context_before(text, match.start())
        )
        candidates.setdefault(_candidate_key(item), item)

    ordered = sorted(candidates.values(), key=lambda item: (item.start, -(item.end - item.start), item.feature))
    # 同一字段值被字母数字扫描拆成片段时，保留外层完整候选（例如 IP/版本号），
    # 防止后续基数校验把一个值误判成多个值。
    filtered: list[VariableCandidate] = []
    for candidate in ordered:
        if any(
            other.start <= candidate.start
            and other.end >= candidate.end
            and (other.start, other.end) != (candidate.start, candidate.end)
            for other in ordered
        ):
            continue
        filtered.append(candidate)
    return sorted(filtered, key=lambda item: (item.start, item.end, item.feature))


def _candidate_label(candidate: VariableCandidate) -> str:
    if candidate.feature == "label_colon":
        return candidate.context
    context = candidate.context.rstrip("的 \t")
    label_match = re.search(r"([\u4e00-\u9fffA-Za-z_][\u4e00-\u9fffA-Za-z0-9_.-]{0,15})$", context)
    return label_match.group(1) if label_match else context


def _label_matches(candidate: VariableCandidate, labels: tuple[str, ...]) -> bool:
    label = _candidate_label(candidate)
    return any(label == item or label.endswith(item) for item in labels)


def _map_change_pair(text: str, definition: TargetDefinition) -> list[VariableCandidate]:
    patterns = (
        re.compile(
            r"从\s*[（(<【]\s*(?P<before>[^）)>】\r\n]+)\s*[）)>】]\s*变更为\s*[（(<【]\s*(?P<after>[^）)>】\r\n]+)\s*[）)>】]"
        ),
        re.compile(r"从\s*(?P<before>[^，。；;\r\n]+?)\s*变更为\s*(?P<after>[^，。；;\r\n]+)"),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        before_start, before_end = match.span("before")
        after_start, after_end = match.span("after")
        return [
            VariableCandidate(
                match.group("before").strip(), before_start, before_end, "relation_from", "从", definition.template_id
            ),
            VariableCandidate(
                match.group("after").strip(), after_start, after_end, "relation_to", "变更为", definition.template_id
            ),
        ]
    return []


def map_target_candidates(
    text: str,
    candidates: list[VariableCandidate],
    target_variable: str,
) -> list[VariableCandidate]:
    """第二层：依据标签和审核模板将候选映射为目标变量。"""

    definition = TARGET_DEFINITIONS.get(target_variable)
    if definition is None:
        raise VariableProcessingError("QFK_VAR_TARGET_UNSUPPORTED", f"未注册目标变量: {target_variable}")
    if target_variable == "change_pair":
        return _map_change_pair(text, definition)

    mapped: list[VariableCandidate] = []
    for candidate in candidates:
        if not _label_matches(candidate, definition.labels):
            continue
        if definition.value_type == "percentage" and not _PERCENTAGE_FULL_RE.fullmatch(candidate.raw_value):
            continue
        if definition.value_type == "quantity" and not _QUANTITY_FULL_RE.fullmatch(candidate.raw_value):
            continue
        mapped.append(
            VariableCandidate(
                candidate.raw_value,
                candidate.start,
                candidate.end,
                candidate.feature,
                candidate.context,
                definition.template_id,
            )
        )
    # 同一原文 span 可能同时由括号扫描和数值扫描发现，只保留一个证据候选。
    deduplicated: dict[tuple[int, int, str], VariableCandidate] = {}
    for candidate in mapped:
        deduplicated.setdefault(_candidate_key(candidate), candidate)
    return sorted(deduplicated.values(), key=lambda item: (item.start, item.end))


def normalize_value(raw_value: Any, value_type: str) -> Any:
    """第二层归一化 + 第三层类型校验。"""

    normalized_type = str(value_type or "string").lower()
    if normalized_type not in VALUE_TYPES:
        raise VariableProcessingError("QFK_VAR_TYPE_UNSUPPORTED", f"不支持变量类型: {value_type}")
    if normalized_type == "object":
        if not isinstance(raw_value, dict):
            raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", "目标值不是 object")
        return raw_value
    if normalized_type == "array<string>":
        if not isinstance(raw_value, list) or any(not isinstance(item, str) for item in raw_value):
            raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", "目标值不是 string 数组")
        return [item.strip() for item in raw_value]
    if normalized_type == "boolean":
        if isinstance(raw_value, bool):
            return raw_value
        lowered = str(raw_value).strip().casefold()
        if lowered in {"true", "1", "yes", "on", "是"}:
            return True
        if lowered in {"false", "0", "no", "off", "否"}:
            return False
        raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", f"无法解析 boolean: {raw_value!r}")

    raw = str(raw_value).strip()
    if normalized_type == "string":
        if not raw:
            raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", "string 结果不能为空")
        return raw
    if normalized_type == "integer":
        if not _INTEGER_FULL_RE.fullmatch(raw):
            raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", f"无法解析 integer: {raw!r}")
        return int(raw)
    if normalized_type == "number":
        if not _NUMBER_FULL_RE.fullmatch(raw):
            raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", f"无法解析 number: {raw!r}")
        return float(raw)
    if normalized_type == "percentage":
        if not _PERCENTAGE_FULL_RE.fullmatch(raw):
            raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", f"无法解析 percentage: {raw!r}")
        value = float(raw.rstrip("%").strip())
        if not 0 <= value <= 100:
            raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", f"percentage 超出 0-100: {raw!r}")
        return value
    quantity_match = _QUANTITY_FULL_RE.fullmatch(raw)
    if not quantity_match:
        raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", f"无法解析 quantity: {raw!r}")
    return {
        "value": float(quantity_match.group("number")),
        "unit": quantity_match.group("unit"),
        "raw": raw,
    }


def _evidence(candidate: VariableCandidate) -> dict[str, Any]:
    return {
        "raw_value": candidate.raw_value,
        "span": {"start": candidate.start, "end": candidate.end},
        "context": candidate.context,
        "feature": candidate.feature,
        "template_id": candidate.template_id,
    }


def feature_extract(
    text: str,
    target_variable: str,
    *,
    value_type: str | None = None,
    cardinality: str | None = None,
) -> VariableProcessResult:
    """执行前三层确定性 description 处理。"""

    try:
        definition = TARGET_DEFINITIONS.get(target_variable)
        if definition is None:
            raise VariableProcessingError("QFK_VAR_TARGET_UNSUPPORTED", f"未注册目标变量: {target_variable}")
        expected_type = value_type or definition.value_type
        expected_cardinality = cardinality or definition.cardinality
        if expected_type != definition.value_type:
            raise VariableProcessingError(
                "QFK_VAR_TARGET_CONTRACT_MISMATCH",
                f"{target_variable} 固定类型为 {definition.value_type}，不能改成 {expected_type}",
            )
        if expected_cardinality not in CARDINALITIES:
            raise VariableProcessingError("QFK_VAR_CARDINALITY_UNSUPPORTED", f"不支持基数: {expected_cardinality}")
        candidates = extract_deterministic_candidates(text)
        mapped = map_target_candidates(text, candidates, target_variable)
        if target_variable == "change_pair" and len(mapped) == 2:
            value = {"from": mapped[0].raw_value, "to": mapped[1].raw_value}
            return VariableProcessResult(
                status="succeeded",
                value=normalize_value(value, expected_type),
                raw_values=[item.raw_value for item in mapped],
                evidence=[_evidence(item) for item in mapped],
            )
        if expected_cardinality == "exactly_one":
            if not mapped:
                return VariableProcessResult(
                    status="unknown",
                    error_code="QFK_VAR_NO_STABLE_BOUNDARY",
                    error=f"没有找到 {target_variable} 的稳定候选边界",
                    fallback_reason="no_stable_boundary",
                )
            if len(mapped) > 1:
                return VariableProcessResult(
                    status="ambiguous",
                    raw_values=[item.raw_value for item in mapped],
                    evidence=[_evidence(item) for item in mapped],
                    error_code="QFK_VAR_CARDINALITY_MISMATCH",
                    error=f"{target_variable} 期望一个候选，实际 {len(mapped)} 个",
                )
            value = normalize_value(mapped[0].raw_value, expected_type)
        else:
            value = [normalize_value(item.raw_value, expected_type) for item in mapped]
        return VariableProcessResult(
            status="succeeded",
            value=value,
            raw_values=[item.raw_value for item in mapped],
            evidence=[_evidence(item) for item in mapped],
        )
    except VariableProcessingError as exc:
        return VariableProcessResult(status="error", error_code=exc.code, error=exc.message)


def _path_tokens(path: str) -> list[str | int]:
    normalized = str(path or "").strip()
    if normalized.startswith("$"):
        normalized = normalized[1:]
    normalized = normalized.lstrip(".")
    if not normalized:
        return []
    tokens: list[str | int] = []
    consumed = 0
    for match in _PATH_PART_RE.finditer(normalized):
        if match.start() != consumed and normalized[consumed : match.start()] != ".":
            raise VariableProcessingError("QFK_VAR_PATH_INVALID", f"不支持的路径: {path}")
        tokens.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
        consumed = match.end()
    if consumed != len(normalized):
        raise VariableProcessingError("QFK_VAR_PATH_INVALID", f"不支持的路径: {path}")
    return tokens


def get_path_value(value: Any, path: str) -> Any:
    """读取受限对象字段/数组下标，不支持过滤器和递归扫描。"""

    current = value
    if isinstance(current, str):
        try:
            current = json.loads(current)
        except json.JSONDecodeError as exc:
            raise VariableProcessingError("QFK_VAR_JSON_INVALID", f"输入不是合法 JSON: {exc}") from exc
    for token in _path_tokens(path):
        try:
            if (
                isinstance(token, int)
                and isinstance(current, list)
                or isinstance(token, str)
                and isinstance(current, dict)
            ):
                current = current[token]
            else:
                raise KeyError(token)
        except (KeyError, IndexError) as exc:
            raise VariableProcessingError("QFK_VAR_PATH_NOT_FOUND", f"路径不存在: {path}") from exc
    return current


def _compare(left: Any, right: Any, comparison: str) -> bool:
    functions = {
        ">": operator.gt,
        ">=": operator.ge,
        "<": operator.lt,
        "<=": operator.le,
        "==": operator.eq,
        "=": operator.eq,
        "!=": operator.ne,
    }
    function = functions.get(comparison)
    if function is None:
        raise VariableProcessingError("QFK_VAR_OPERATOR_UNSUPPORTED", f"不支持比较符: {comparison}")
    return bool(function(left, right))


def _comparable_value(value: Any, value_type: str) -> Any:
    """将带单位数量转换为可比较的标量；不同单位拒绝静默换算。"""

    if value_type == "quantity":
        if not isinstance(value, dict) or not value.get("unit"):
            raise VariableProcessingError("QFK_VAR_TYPE_MISMATCH", "quantity 缺少单位")
        return (str(value["unit"]).casefold(), float(value["value"]))
    return value


def execute_operation(args: dict[str, Any]) -> VariableProcessResult:
    """执行无需 AI 的 qfk_var 操作；feature_extract 只运行前三层。"""

    try:
        operation = str(args.get("operation") or "")
        value_type = str(args.get("value_type") or "string")
        if operation == "feature_extract":
            return feature_extract(
                args.get("input"),
                str(args.get("target_variable") or ""),
                value_type=value_type,
                cardinality=str(args.get("cardinality") or "exactly_one"),
            )
        if operation in {"field", "json_path"}:
            value = get_path_value(args.get("input"), str(args.get("path") or ""))
            return VariableProcessResult(status="succeeded", value=normalize_value(value, value_type))
        if operation == "cast":
            return VariableProcessResult(status="succeeded", value=normalize_value(args.get("input"), value_type))
        if operation == "string":
            value = normalize_value(args.get("input"), "string")
            function = str(args.get("function") or "")
            if function == "trim":
                value = value.strip()
            elif function == "lower":
                value = value.lower()
            elif function == "upper":
                value = value.upper()
            elif function == "split":
                separator = args.get("separator")
                if not isinstance(separator, str) or not separator:
                    raise VariableProcessingError("QFK_VAR_INVALID_ARGS", "split.separator 必须是非空字符串")
                value = [item.strip() for item in value.split(separator) if item.strip()]
                return VariableProcessResult(status="succeeded", value=normalize_value(value, "array<string>"))
            elif function == "replace":
                value = value.replace(str(args.get("old") or ""), str(args.get("new") or ""))
            else:
                raise VariableProcessingError("QFK_VAR_STRING_FUNCTION_UNSUPPORTED", f"不支持字符串函数: {function}")
            return VariableProcessResult(status="succeeded", value=value)
        if operation == "compose":
            parts = args.get("parts")
            if not isinstance(parts, list) or not parts:
                raise VariableProcessingError("QFK_VAR_INVALID_ARGS", "compose.parts 必须是非空数组")
            return VariableProcessResult(
                status="succeeded", value=str(args.get("separator") or "").join(str(item) for item in parts)
            )
        if operation == "compare":
            left = normalize_value(args.get("left"), value_type)
            right = normalize_value(args.get("right"), value_type)
            if value_type == "quantity" and left["unit"].casefold() != right["unit"].casefold():
                raise VariableProcessingError(
                    "QFK_VAR_UNIT_MISMATCH",
                    f"quantity 单位不一致: {left['unit']} vs {right['unit']}",
                )
            left = _comparable_value(left, value_type)
            right = _comparable_value(right, value_type)
            matched = _compare(left, right, str(args.get("operator") or ""))
            return VariableProcessResult(status="succeeded", value=matched, matched=matched)
        if operation == "exists":
            value = args.get("input")
            if args.get("path"):
                try:
                    value = get_path_value(value, str(args["path"]))
                except VariableProcessingError as exc:
                    if exc.code == "QFK_VAR_PATH_NOT_FOUND":
                        return VariableProcessResult(status="succeeded", value=False, matched=False)
                    raise
            exists = value is not None
            return VariableProcessResult(status="succeeded", value=exists, matched=exists)
        raise VariableProcessingError("QFK_VAR_OPERATION_UNSUPPORTED", f"不支持操作: {operation}")
    except VariableProcessingError as exc:
        return VariableProcessResult(status="error", error_code=exc.code, error=exc.message)
    except (TypeError, ValueError) as exc:
        return VariableProcessResult(status="error", error_code="QFK_VAR_INVALID_ARGS", error=str(exc))
