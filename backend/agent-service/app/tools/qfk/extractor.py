"""QFK 命令输出的确定性提取器。

该模块只处理已经执行完成的命令结果，不执行 shell、不解释 grep/awk，也不调用 LLM。
展示层可以继续使用截断输出；产出变量必须从完整物理流中提取，缓存缺失时 Fail Closed。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    raw_values: list[str]
    values: list[Any]
    value_type: str


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


def extract_output_values(output: str, spec: dict[str, Any], value_type: str = "string") -> ExtractionResult:
    """按受控“筛选行 + 列提取”规格从文本输出中确定性取值。

    ``value_type`` 为调用方的业务类型；若 ``spec.value_mode`` 有值则优先使用它。
    这样 Matcher 的数值比较与 Produces 的数值变量可以共享同一份文本语义。
    """

    if not isinstance(spec, dict) or spec.get("type", "text") != "text":
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "extract.type 必须为 text")

    include = _string_list(spec.get("include", []), "include")
    exclude = _string_list(spec.get("exclude", []), "exclude")
    include_mode = spec.get("include_mode", "all")
    if include_mode not in {"all", "any"}:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "include_mode 仅支持 all/any")
    case_sensitive = spec.get("case_sensitive", True)
    if not isinstance(case_sensitive, bool):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "case_sensitive 必须为布尔值")

    lines = output.splitlines()
    if not lines and not output:
        raise QFKExtractionError("QFK_OUTPUT_EMPTY", "命令标准输出为空")

    matched_lines = [
        line
        for line in lines
        if _line_matches(line, include, exclude, include_mode, case_sensitive)
    ]
    if not matched_lines:
        raise QFKExtractionError("QFK_NO_MATCH", "没有输出行满足筛选条件")

    cardinality = spec.get("cardinality", "exactly_one")
    if cardinality not in {"exactly_one", "first", "last", "all"}:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "cardinality 非法")
    if cardinality == "exactly_one" and len(matched_lines) != 1:
        raise QFKExtractionError(
            "QFK_MULTIPLE_MATCHES",
            f"期望唯一匹配行，实际匹配 {len(matched_lines)} 行",
        )
    selected = (
        matched_lines
        if cardinality == "all"
        else [matched_lines[-1] if cardinality == "last" else matched_lines[0]]
    )

    raw_values = [_extract_column(line, spec) for line in selected]
    effective_type = str(spec.get("value_mode") or value_type or "string")
    if effective_type == "array":
        converted: list[Any] = list(raw_values)
    else:
        converted = [_cast_scalar(value, effective_type) for value in raw_values]
    return ExtractionResult(
        matched_lines=matched_lines,
        selected_lines=selected,
        raw_values=raw_values,
        values=converted,
        value_type=effective_type,
    )


def extract_text_value(output: str, spec: dict[str, Any], value_type: str = "string") -> Any:
    """向后兼容的标量/数组接口；实现委托给唯一的公共提取核心。"""

    result = extract_output_values(output, spec, value_type)
    cardinality = spec.get("cardinality", "exactly_one")
    if cardinality == "all" or value_type == "array" or result.value_type == "array":
        return result.values
    return result.values[0]


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"{field} 必须为非空字符串数组")
    return value


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


def _extract_column(line: str, spec: dict[str, Any]) -> str:
    mode = spec.get("column_mode", "index" if spec.get("column") is not None else "whole")
    if mode == "whole":
        return line.strip()
    if mode not in {"index", "from_index"}:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "column_mode 仅支持 whole/index/from_index")
    column = spec.get("column")
    if not isinstance(column, int) or isinstance(column, bool) or column < 1:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "列号必须是从 1 开始的整数")

    delimiter = spec.get("delimiter", "whitespace")
    if delimiter == "whitespace":
        fields = re.split(r"\s+", line.strip()) if line.strip() else []
        joiner = " "
    elif isinstance(delimiter, str) and len(delimiter) == 1:
        fields = [field.strip() for field in line.split(delimiter)]
        joiner = delimiter
    else:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", "分隔符必须为 whitespace 或单个字符")

    index = column - 1
    if index >= len(fields):
        raise QFKExtractionError(
            "QFK_COLUMN_OUT_OF_RANGE",
            f"第 {column} 列不存在，该行只有 {len(fields)} 列",
        )
    return fields[index] if mode == "index" else joiner.join(fields[index:]).strip()


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
            # 允许 HCI 常见的带单位文本，例如 ``54%``、``21.5ms``。单位只是
            # 展示语义，数值 predicate 只比较确定性数值部分；不接受任意混合文本。
            text = value.strip()
            match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(?:\s*(?:%|[A-Za-z]+))?", text)
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
