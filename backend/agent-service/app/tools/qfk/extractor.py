"""QFK 命令输出的确定性提取器。

该模块只处理已经执行完成的命令结果，不执行 shell、不解释 grep/awk，也不调用 LLM。
展示层可以继续使用截断输出；产出变量必须从完整物理流中提取，缓存缺失时 Fail Closed。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.tools.acli.executor import ExecResult

DEFAULT_OUTPUT_MAX_BYTES = 1024 * 1024
HARD_OUTPUT_MAX_BYTES = 4 * 1024 * 1024


@dataclass
class QFKExtractionError(ValueError):
    """带稳定错误码的 QFK 输出提取错误。"""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class ExtractionResult:
    """公共文本提取的完整、可审计结果。

    这是 QFK ``produces[].extract`` 与 ``match.extract`` 的同一底层产物：两者
    使用相同的筛选、列提取、基数校验和类型转换，调用方仅决定把 values 写入变量池
    还是继续聚合并做 predicate。不会执行专家填写的 grep/awk/shell 文本。
    """

    matched_lines: list[str]
    selected_lines: list[str]
    raw_values: list[Any]
    values: list[Any]
    value_type: str
    raw_records: list[dict[str, Any]] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    value_key: str | None = None
    matched_line_numbers: list[int] = field(default_factory=list)
    selected_line_numbers: list[int] = field(default_factory=list)


async def get_complete_output(
    result: ExecResult,
    redis: Any,
    *,
    source: Literal["stdout", "stderr"] = "stdout",
    max_bytes: int = DEFAULT_OUTPUT_MAX_BYTES,
) -> str:
    """返回完整 stdout/stderr；被截断时从 Redis 缓存读取。

    ``cmd_cache:{exec_id}`` 是历史 stdout 缓存键；stderr 使用独立键，避免改变既有
    SOP JSONPath 提取的缓存格式。缓存过期、不可用或数据超限都明确报错，绝不退化到
    截断摘要继续提取。
    """

    if source not in {"stdout", "stderr"}:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"不支持的输出来源: {source}")
    if max_bytes < 1 or max_bytes > HARD_OUTPUT_MAX_BYTES:
        raise QFKExtractionError(
            "QFK_EXTRACT_INVALID_SPEC",
            f"输出大小上限必须在 1 到 {HARD_OUTPUT_MAX_BYTES} 字节之间",
        )

    truncated = result.truncated if source == "stdout" else result.stderr_truncated
    output = result.stdout if source == "stdout" else result.stderr
    if truncated:
        if not result.exec_id:
            raise QFKExtractionError(
                "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE",
                f"{source} 已截断但执行结果缺少 exec_id",
            )
        client = getattr(redis, "client", None)
        if client is None:
            raise QFKExtractionError(
                "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE",
                "完整输出缓存未连接",
            )
        cache_prefix = "cmd_cache" if source == "stdout" else "cmd_stderr_cache"
        try:
            cached = await client.get(f"{cache_prefix}:{result.exec_id}")
        except Exception as exc:
            raise QFKExtractionError(
                "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE",
                f"读取完整 {source} 缓存失败",
            ) from exc
        if cached is None:
            raise QFKExtractionError(
                "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE",
                f"{source} 已截断且完整输出缓存不存在或已过期",
            )
        output = cached.decode("utf-8") if isinstance(cached, bytes) else str(cached)

    if len(output.encode("utf-8")) > max_bytes:
        raise QFKExtractionError(
            "QFK_OUTPUT_TOO_LARGE",
            f"{source} 超过允许的 {max_bytes} 字节，需改用专用采集器缩小结果集",
        )
    return output


@dataclass(frozen=True)
class _LineRef:
    """保留物理行身份，避免筛选后把展示序号误当成稳定行号。"""

    physical: int
    text: str
    non_empty: int | None = None
    data: int | None = None


def extract_output_values(output: str, spec: dict[str, Any], value_type: str = "string") -> ExtractionResult:
    """ValueExtract 的唯一运行时入口。"""

    if not isinstance(spec, dict):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "extract 必须为对象")
    extract_type = str(spec.get("type") or "text")
    if extract_type == "json":
        return _extract_json_values(output, spec, value_type)
    if extract_type != "text":
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "extract.type 仅支持 text/json")
    return _extract_structured_text_values(output, spec, value_type)


def extract_value(output: str, spec: dict[str, Any], value_type: str = "string") -> Any:
    """按目标变量类型返回标量、数组或结构化记录。"""

    result = extract_output_values(output, spec, value_type)
    normalized_type = str(value_type or "string").lower()
    cardinality = spec.get("cardinality", "exactly_one")
    if normalized_type == "array<object>":
        return result.records or result.values
    if normalized_type == "object":
        records = result.records or result.values
        if len(records) != 1:
            raise QFKExtractionError("QFK_CARDINALITY_MISMATCH", f"object 期望唯一记录，实际 {len(records)} 条")
        return records[0]
    if cardinality == "all" or normalized_type == "array" or result.value_type in {"array", "array<object>"}:
        return result.values
    if not result.values:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "提取结果没有主值；请配置 value_key 或复合变量类型")
    return result.values[0]


def _extract_structured_text_values(output: str, spec: dict[str, Any], value_type: str) -> ExtractionResult:
    allowed = {"type", "rows", "parser", "header", "columns", "value_key", "delimiter", "cardinality", "source", "value_mode"}
    unsupported = sorted(set(spec) - allowed)
    if unsupported:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"不支持旧版或未知 text extract 字段: {', '.join(unsupported)}")
    columns = spec.get("columns")
    rows = spec.get("rows")
    if not isinstance(rows, dict):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "结构化文本取值必须配置 rows")
    if columns is not None and (
        spec.get("parser") not in {"whitespace_table", "delimited_table"}
        or not isinstance(columns, list)
        or not columns
    ):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "多列文本取值必须配置受控 table parser 和 columns")
    delimiter = spec.get("delimiter", "whitespace")
    if spec.get("parser") == "delimited_table" and (not isinstance(delimiter, str) or len(delimiter) != 1):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "delimited_table 必须配置单字符 delimiter")
    if columns is None and any(key in spec for key in ("parser", "header", "value_key")):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "整行结构化取值只能配置 rows")

    lines = output.splitlines()
    if not lines and not output:
        raise QFKExtractionError("QFK_OUTPUT_EMPTY", "命令标准输出为空")
    refs = _build_line_refs(lines)
    header_ref, header_fields = _resolve_header(refs, spec.get("header"), delimiter)
    refs = _assign_data_indices(refs, header_ref)
    matched_refs = _select_rows(refs, rows, header_ref)
    selected_refs = _select_cardinality(matched_refs, spec)
    if columns is None:
        raw_values = [ref.text.strip() for ref in selected_refs]
        effective_type = str(spec.get("value_mode") or value_type or "string")
        values = (
            list(raw_values)
            if effective_type == "array"
            else [_cast_scalar(value, effective_type) for value in raw_values]
        )
        return _result_from_lines(matched_refs, selected_refs, raw_values, values, effective_type)
    resolved_columns = _resolve_columns(columns, header_fields)

    raw_records: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for ref in selected_refs:
        fields = _split_fields(ref.text, delimiter)
        raw_record: dict[str, Any] = {}
        record: dict[str, Any] = {}
        for column, index, selected_by_header in resolved_columns:
            if index >= len(fields):
                code = "QFK_TABLE_SHAPE_MISMATCH" if selected_by_header else "QFK_COLUMN_OUT_OF_RANGE"
                raise QFKExtractionError(
                    code,
                    f"物理第 {ref.physical} 行缺少第 {index + 1} 列，实际只有 {len(fields)} 列",
                )
            key = str(column["key"])
            raw = fields[index]
            mode = str(column.get("value_mode") or "string")
            raw_record[key] = raw
            record[key] = _cast_scalar(raw, mode)
        raw_records.append(raw_record)
        records.append(record)

    value_key = str(spec.get("value_key") or "") or (str(columns[0]["key"]) if len(columns) == 1 else None)
    if value_key and value_key not in records[0]:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"value_key={value_key} 不属于 columns")
    raw_values = [record[value_key] for record in raw_records] if value_key else []
    values = [record[value_key] for record in records] if value_key else []
    value_type_out = (
        str(next(column.get("value_mode") or "string" for column in columns if column.get("key") == value_key))
        if value_key
        else str(value_type or "object")
    )
    return ExtractionResult(
        matched_lines=[ref.text for ref in matched_refs],
        selected_lines=[ref.text for ref in selected_refs],
        raw_values=raw_values,
        values=values,
        value_type=value_type_out,
        raw_records=raw_records,
        records=records,
        value_key=value_key,
        matched_line_numbers=[ref.physical for ref in matched_refs],
        selected_line_numbers=[ref.physical for ref in selected_refs],
    )


def _extract_json_values(output: str, spec: dict[str, Any], value_type: str) -> ExtractionResult:
    if not output.strip():
        raise QFKExtractionError("QFK_OUTPUT_EMPTY", "命令输出为空")
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, ValueError) as exc:
        raise QFKExtractionError("QFK_JSON_PARSE_FAILED", "声明 JSON 取值但命令输出不是合法 JSON") from exc
    found, node = _read_json_path(payload, str(spec.get("path") or ""))
    if not found:
        raise QFKExtractionError("QFK_PATH_NOT_FOUND", f"JSON path {spec.get('path')!r} 不存在")
    candidates = list(node) if isinstance(node, list) else [node]
    selected = _select_cardinality(candidates, spec)
    effective_type = str(spec.get("value_mode") or value_type or "string")
    values = [_cast_json_value(item, effective_type) for item in selected]
    records = [item for item in values if isinstance(item, dict)]
    return ExtractionResult(
        matched_lines=[],
        selected_lines=[],
        raw_values=selected,
        values=values,
        value_type=effective_type,
        raw_records=[item for item in selected if isinstance(item, dict)],
        records=records,
    )


def _result_from_lines(
    matched_refs: list[_LineRef],
    selected_refs: list[_LineRef],
    raw_values: list[Any],
    values: list[Any],
    value_type: str,
) -> ExtractionResult:
    return ExtractionResult(
        matched_lines=[ref.text for ref in matched_refs],
        selected_lines=[ref.text for ref in selected_refs],
        raw_values=raw_values,
        values=values,
        value_type=value_type,
        matched_line_numbers=[ref.physical for ref in matched_refs],
        selected_line_numbers=[ref.physical for ref in selected_refs],
    )


def _build_line_refs(lines: list[str]) -> list[_LineRef]:
    refs: list[_LineRef] = []
    non_empty = 0
    for physical, line in enumerate(lines, start=1):
        if line.strip():
            non_empty += 1
            refs.append(_LineRef(physical=physical, text=line, non_empty=non_empty))
        else:
            refs.append(_LineRef(physical=physical, text=line))
    return refs


def _resolve_header(
    refs: list[_LineRef],
    header: Any,
    delimiter: Any,
) -> tuple[_LineRef | None, list[str] | None]:
    if header is None:
        return None, None
    if not isinstance(header, dict) or header.get("mode") != "contains":
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "header.mode 仅支持 contains")
    required = _string_list(header.get("required"), "header.required")
    case_sensitive = _boolean(header.get("case_sensitive", False), "header.case_sensitive")
    required_normalized = [_normalize_header(item, case_sensitive) for item in required]
    candidates: list[tuple[_LineRef, list[str]]] = []
    for ref in refs:
        fields = _split_fields(ref.text, delimiter)
        normalized_fields = [_normalize_header(item, case_sensitive) for item in fields]
        if all(item in normalized_fields for item in required_normalized):
            candidates.append((ref, fields))
    if not candidates:
        raise QFKExtractionError("QFK_HEADER_NOT_FOUND", f"没有表头同时包含 {required}")
    if len(candidates) > 1:
        raise QFKExtractionError("QFK_AMBIGUOUS_HEADER", f"有 {len(candidates)} 行同时满足表头条件")
    return candidates[0]


def _assign_data_indices(refs: list[_LineRef], header_ref: _LineRef | None) -> list[_LineRef]:
    if header_ref is None:
        return refs
    assigned: list[_LineRef] = []
    data_index = 0
    for ref in refs:
        if ref.physical > header_ref.physical and ref.text.strip():
            data_index += 1
            assigned.append(_LineRef(ref.physical, ref.text, ref.non_empty, data_index))
        else:
            assigned.append(ref)
    return assigned


def _select_rows(refs: list[_LineRef], rows: dict[str, Any], header_ref: _LineRef | None) -> list[_LineRef]:
    mode = rows.get("mode")
    data_candidates = [ref for ref in refs if ref.data is not None] if header_ref else [ref for ref in refs if ref.text.strip()]
    if mode == "all":
        matched = data_candidates
    elif mode == "keywords":
        include = _string_list(rows.get("include", []), "rows.include")
        exclude = _string_list(rows.get("exclude", []), "rows.exclude")
        include_mode = _include_mode(rows.get("include_mode", "all"))
        case_sensitive = _boolean(rows.get("case_sensitive", True), "rows.case_sensitive")
        matched = [
            ref
            for ref in data_candidates
            if _line_matches(ref.text, include, exclude, include_mode, case_sensitive)
        ]
    elif mode == "indices":
        basis = rows.get("basis")
        if basis == "physical":
            candidates = refs
        elif basis == "non_empty":
            candidates = [ref for ref in refs if ref.non_empty is not None]
        elif basis == "data" and header_ref:
            candidates = [ref for ref in refs if ref.data is not None]
        elif basis == "data":
            raise QFKExtractionError("QFK_HEADER_NOT_FOUND", "rows.basis=data 需要成功识别表头")
        else:
            raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "rows.basis 非法")
        wanted = _row_indices(rows, len(candidates))
        matched = [ref for position, ref in enumerate(candidates, start=1) if position in wanted]
    else:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "rows.mode 仅支持 all/keywords/indices")
    if not matched:
        raise QFKExtractionError("QFK_NO_MATCH", "没有输出行满足行选择条件")
    return matched


def _row_indices(rows: dict[str, Any], available: int) -> set[int]:
    wanted: set[int] = set()
    indices = rows.get("indices") or []
    ranges = rows.get("ranges") or []
    if not indices and not ranges:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "行号选择必须配置 indices 或 ranges")
    for index in indices:
        if not isinstance(index, int) or isinstance(index, bool) or index < 1 or index > available:
            raise QFKExtractionError("QFK_ROW_OUT_OF_RANGE", f"行号 {index!r} 超出 1..{available}")
        wanted.add(index)
    for row_range in ranges:
        if not isinstance(row_range, dict):
            raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "rows.ranges 必须为对象数组")
        start, end = row_range.get("start"), row_range.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 1
            or end < start
            or end > available
        ):
            raise QFKExtractionError("QFK_ROW_OUT_OF_RANGE", f"行号范围 {start!r}..{end!r} 超出 1..{available}")
        wanted.update(range(start, end + 1))
    return wanted


def _resolve_columns(
    columns: list[Any],
    header_fields: list[str] | None,
) -> list[tuple[dict[str, Any], int, bool]]:
    keys: set[str] = set()
    resolved: list[tuple[dict[str, Any], int, bool]] = []
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get("selector"), dict):
            raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "columns 项必须包含 selector")
        key = str(column.get("key") or "")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in keys:
            raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"columns.key 非法或重复: {key!r}")
        keys.add(key)
        selector = column["selector"]
        if selector.get("by") == "index":
            index = selector.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 1:
                raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "列号必须是从 1 开始的整数")
            resolved.append((column, index - 1, False))
            continue
        if selector.get("by") != "header":
            raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "selector.by 仅支持 index/header")
        if header_fields is None:
            raise QFKExtractionError("QFK_HEADER_NOT_FOUND", "按表头选列但没有配置或识别表头")
        names = [selector.get("name"), *(selector.get("aliases") or [])]
        if any(not isinstance(name, str) or not name for name in names):
            raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "列名与 aliases 必须为非空字符串")
        normalized_names = {str(name).strip().casefold() for name in names}
        positions = [index for index, item in enumerate(header_fields) if item.strip().casefold() in normalized_names]
        if not positions:
            raise QFKExtractionError("QFK_COLUMN_NOT_FOUND", f"表头不存在列 {selector.get('name')!r}")
        if len(positions) > 1:
            raise QFKExtractionError("QFK_AMBIGUOUS_COLUMN", f"表头列 {selector.get('name')!r} 匹配到多个位置")
        resolved.append((column, positions[0], True))
    return resolved


def _select_cardinality(items: list[Any], spec: dict[str, Any]) -> list[Any]:
    if not items:
        raise QFKExtractionError("QFK_NO_MATCH", "提取结果为空")
    cardinality = spec.get("cardinality", "exactly_one")
    if cardinality not in {"exactly_one", "first", "last", "all"}:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "cardinality 非法")
    if cardinality == "exactly_one" and len(items) != 1:
        raise QFKExtractionError("QFK_CARDINALITY_MISMATCH", f"期望唯一结果，实际 {len(items)} 条")
    if cardinality == "all":
        return items
    return [items[-1] if cardinality == "last" else items[0]]


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"{field} 必须为非空字符串数组")
    return value


def _include_mode(value: Any) -> str:
    if value not in {"all", "any"}:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "include_mode 仅支持 all/any")
    return str(value)


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"{field_name} 必须为布尔值")
    return value


def _split_fields(line: str, delimiter: Any) -> list[str]:
    if delimiter == "whitespace":
        return re.split(r"\s+", line.strip()) if line.strip() else []
    if isinstance(delimiter, str) and len(delimiter) == 1:
        return [item.strip() for item in line.split(delimiter)]
    raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "分隔符必须为 whitespace 或单个字符")


def _normalize_header(value: str, case_sensitive: bool) -> str:
    normalized = value.strip()
    return normalized if case_sensitive else normalized.casefold()


def _line_matches(
    line: str,
    include: list[str],
    exclude: list[str],
    include_mode: str,
    case_sensitive: bool,
) -> bool:
    candidate = line if case_sensitive else line.casefold()
    includes = include if case_sensitive else [item.casefold() for item in include]
    excludes = exclude if case_sensitive else [item.casefold() for item in exclude]
    include_ok = not includes or (all(item in candidate for item in includes) if include_mode == "all" else any(item in candidate for item in includes))
    return include_ok and not any(item in candidate for item in excludes)


def _cast_scalar(value: str, value_type: str) -> Any:
    normalized_type = (value_type or "string").lower()
    try:
        if normalized_type == "string":
            return value.strip()
        if normalized_type == "integer":
            text = value.strip()
            if not re.fullmatch(r"[+-]?\d+", text):
                raise ValueError
            return int(text)
        if normalized_type == "number":
            # 百分号只是固定比例表示，可无损规范化；容量/时长等单位没有统一
            # 量纲契约，禁止把 2.6G 或 21.5ms 静默剥单位后参与比较。
            text = value.strip()
            match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(?:\s*%)?", text)
            if not match:
                raise ValueError
            return float(match.group(1))
        if normalized_type == "boolean":
            text = value.strip().casefold()
            if text in {"true", "1", "yes", "on"}:
                return True
            if text in {"false", "0", "no", "off"}:
                return False
            raise ValueError
        if normalized_type == "array":
            return [value.strip()]
    except (TypeError, ValueError) as exc:
        raise QFKExtractionError(
            "QFK_TYPE_CAST_FAILED",
            f"值 {value!r} 无法转换为 {value_type}",
        ) from exc
    raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"不支持的变量类型: {value_type}")


def _cast_json_value(value: Any, value_type: str) -> Any:
    normalized_type = str(value_type or "string").lower()
    if normalized_type == "object":
        if isinstance(value, dict):
            return value
        raise QFKExtractionError("QFK_TYPE_CAST_FAILED", f"JSON 值 {value!r} 不是 object")
    if normalized_type == "array<object>":
        if isinstance(value, dict):
            return value
        raise QFKExtractionError("QFK_TYPE_CAST_FAILED", f"JSON 数组元素 {value!r} 不是 object")
    if normalized_type == "array":
        return value
    if isinstance(value, (dict, list)):
        raise QFKExtractionError("QFK_TYPE_CAST_FAILED", f"JSON 复合值不能转换为 {value_type}")
    if value is None:
        raise QFKExtractionError("QFK_TYPE_CAST_FAILED", f"JSON null 不能转换为 {value_type}")
    if normalized_type == "boolean" and isinstance(value, bool):
        return value
    if normalized_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if normalized_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return _cast_scalar(str(value), normalized_type)


def _read_json_path(payload: Any, path: str) -> tuple[bool, Any]:
    if not path:
        return True, payload
    if any(token in path for token in ("$", "*", "?", "@", "(", ")")):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "JSON path 不接受 jq/过滤器/通配符")
    normalized = re.sub(r"\[(\d+)]", r".\1", path)
    if "[" in normalized or "]" in normalized or normalized.startswith(".") or ".." in normalized:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"JSON path 语法非法: {path!r}")
    node = payload
    for part in normalized.split("."):
        if not part:
            raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"JSON path 语法非法: {path!r}")
        if isinstance(node, dict):
            if part not in node:
                return False, None
            node = node[part]
        elif isinstance(node, list) and part.isdigit():
            index = int(part)
            if index >= len(node):
                return False, None
            node = node[index]
        else:
            return False, None
    return True, node
