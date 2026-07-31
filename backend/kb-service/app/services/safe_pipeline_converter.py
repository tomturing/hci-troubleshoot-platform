"""把常见 grep/awk/cut 管道转换为受控的 QFK 文本提取规则。

转换器只做语法识别，不执行输入。无法无损表达的管道一律拒绝并交给人工复核。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any


@dataclass
class SafePipelineConversionError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class SafePipelineConversion:
    command: str
    extract: dict[str, Any]
    removed_segments: list[str] = field(default_factory=list)
    conversion_id: str = ""


def convert_safe_pipeline(raw_command: str) -> SafePipelineConversion:
    """把白名单管道转换为声明式行列 Extract，不执行输入。"""

    if not isinstance(raw_command, str) or not raw_command.strip():
        raise SafePipelineConversionError("QFK_PIPELINE_EMPTY", "命令不能为空")
    segments = _split_pipeline(raw_command)
    if len(segments) < 2:
        raise SafePipelineConversionError("QFK_PIPELINE_NOT_FOUND", "命令中没有可转换的管道")

    base_tokens = segments[0]
    if base_tokens[:1] == ["acli"]:
        base_tokens = _strip_acli_system_prefix(base_tokens)
    if not base_tokens:
        raise SafePipelineConversionError("QFK_PIPELINE_INVALID_BASE", "管道前缺少基础命令")
    _validate_base_tokens(base_tokens)

    include: list[str] = []
    exclude: list[str] = []
    case_sensitive = True
    columns: list[int] | None = None
    delimiter = "whitespace"
    row_indices: list[int] = []
    row_ranges: list[dict[str, int]] = []
    removed: list[str] = []

    for segment in segments[1:]:
        program = segment[0] if segment else ""
        if program == "grep":
            if row_indices or row_ranges:
                raise SafePipelineConversionError(
                    "QFK_PIPELINE_AMBIGUOUS_ROWS",
                    "第一版不组合关键字行筛选与行号筛选",
                )
            patterns, invert, ignore_case = _parse_grep(segment)
            if invert and patterns == ["grep"]:
                removed.append("grep -v grep（平台不执行 grep 进程，无需自排除）")
                continue
            (exclude if invert else include).extend(patterns)
            case_sensitive = case_sensitive and not ignore_case
        elif program == "awk":
            parsed_columns, awk_indices = _parse_awk(segment)
            if columns is not None:
                raise SafePipelineConversionError("QFK_PIPELINE_AMBIGUOUS_COLUMN", "只能配置一次列提取")
            if awk_indices and (include or exclude or row_indices or row_ranges):
                raise SafePipelineConversionError(
                    "QFK_PIPELINE_AMBIGUOUS_ROWS",
                    "第一版不组合关键字行筛选与行号筛选",
                )
            columns = parsed_columns
            row_indices.extend(awk_indices)
        elif program == "cut":
            parsed_delimiter, parsed_columns = _parse_cut(segment)
            if columns is not None:
                raise SafePipelineConversionError("QFK_PIPELINE_AMBIGUOUS_COLUMN", "只能配置一次列提取")
            delimiter, columns = parsed_delimiter, parsed_columns
        elif program == "sed":
            if include or exclude or row_indices or row_ranges:
                raise SafePipelineConversionError(
                    "QFK_PIPELINE_AMBIGUOUS_ROWS",
                    "第一版不组合关键字行筛选与行号筛选",
                )
            row_indices, row_ranges = _parse_sed(segment)
        else:
            raise SafePipelineConversionError(
                "QFK_PIPELINE_UNSUPPORTED_SEGMENT",
                f"不支持的管道段 {program or segment!r}；仅支持受控 grep/awk/cut/sed",
            )

    extract: dict[str, Any] = {"type": "text"}
    if row_indices or row_ranges:
        extract["rows"] = {
            "mode": "indices",
            "basis": "physical",
            **({"indices": row_indices} if row_indices else {}),
            **({"ranges": row_ranges} if row_ranges else {}),
        }
    elif include or exclude:
        extract["rows"] = {
            "mode": "keywords",
            "include": include,
            "exclude": exclude,
            "include_mode": "all",
            "case_sensitive": case_sensitive,
        }
    else:
        extract["rows"] = {"mode": "all"}
    if columns:
        extract["parser"] = "whitespace_table" if delimiter == "whitespace" else "delimited_table"
        if delimiter != "whitespace":
            extract["delimiter"] = delimiter
        extract["columns"] = [
            {
                "key": f"COLUMN_{column}",
                "selector": {"by": "index", "index": column},
                "value_mode": "string",
            }
            for column in columns
        ]
        if len(columns) == 1:
            extract["value_key"] = f"COLUMN_{columns[0]}"

    base_command = shlex.join(base_tokens)
    fingerprint = sha256(f"{raw_command.strip()}\n{base_command}\n{extract!r}".encode()).hexdigest()

    return SafePipelineConversion(
        command=base_command,
        extract=extract,
        removed_segments=removed,
        conversion_id=fingerprint,
    )


def apply_safe_pipeline_to_signal(signal: dict[str, Any]) -> bool:
    """若 QFK signal.command 含管道，则就地转换并返回 True。"""

    acquire = signal.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    args = acquire.get("args") or {}
    command = str(args.get("command") or "")
    if "|" not in command:
        return False
    if tool != "qfk_system":
        raise SafePipelineConversionError(
            "QFK_PIPELINE_UNSUPPORTED_TOOL",
            "当前仅 qfk_system 支持安全管道转换",
        )
    matcher = signal.get("match")
    produces = (signal.get("orchestrate") or {}).get("produces") or []
    valid_produces = [item for item in produces if isinstance(item, dict) and item.get("name")]
    if isinstance(matcher, dict) and valid_produces:
        raise SafePipelineConversionError("QFK_PIPELINE_AMBIGUOUS_TARGET", "match 与 produces 不能同时存在")
    if not isinstance(matcher, dict) and len(valid_produces) != 1:
        raise SafePipelineConversionError(
            "QFK_PIPELINE_OUTPUT_VARIABLE_REQUIRED",
            "安全管道转换要求恰好配置一个产出变量，变量名不能由平台猜测",
        )
    conversion = convert_safe_pipeline(command)
    args["command"] = conversion.command
    if isinstance(matcher, dict):
        if len(conversion.extract.get("columns") or []) > 1:
            raise SafePipelineConversionError(
                "QFK_PIPELINE_VALUE_KEY_REQUIRED",
                "多列 Matcher 转换必须在 Admin Preview 中显式选择主判定列",
            )
        matcher["extract"] = conversion.extract
        return True
    produce = valid_produces[0]
    if len(conversion.extract.get("columns") or []) > 1 and str(produce.get("type") or "string") not in {
        "object",
        "array<object>",
    }:
        raise SafePipelineConversionError(
            "QFK_PIPELINE_VALUE_KEY_REQUIRED",
            "多列标量变量转换必须在 Admin Preview 中显式选择主值列",
        )
    produce.pop("path", None)
    produce["extract"] = conversion.extract
    return True


def _split_pipeline(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise SafePipelineConversionError("QFK_PIPELINE_PARSE_FAILED", f"命令引号不完整: {exc}") from exc
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == "|":
            if not segments[-1]:
                raise SafePipelineConversionError("QFK_PIPELINE_PARSE_FAILED", "管道前后不能有空命令")
            segments.append([])
        elif "|" in token:
            raise SafePipelineConversionError("QFK_PIPELINE_PARSE_FAILED", f"不支持的管道运算符: {token}")
        else:
            segments[-1].append(token)
    if not segments[-1]:
        raise SafePipelineConversionError("QFK_PIPELINE_PARSE_FAILED", "管道末尾缺少命令")
    return segments


def _strip_acli_system_prefix(tokens: list[str]) -> list[str]:
    index = 1
    if tokens[index : index + 2] == ["--formatter", "json"]:
        index += 2
    if index >= len(tokens) or tokens[index] != "system":
        raise SafePipelineConversionError(
            "QFK_PIPELINE_INVALID_BASE",
            "仅支持 acli system 管道；管理端 command 字段也可直接填写 system 后的子命令",
        )
    return tokens[index + 1 :]


def _validate_base_tokens(tokens: list[str]) -> None:
    unsafe = re.compile(r"[;&`$<>#\n\r]")
    for token in tokens:
        if unsafe.search(token):
            raise SafePipelineConversionError(
                "QFK_PIPELINE_UNSAFE_BASE",
                f"基础命令包含禁止字符: {token!r}",
            )


def _parse_grep(tokens: list[str]) -> tuple[list[str], bool, bool]:
    patterns: list[str] = []
    invert = False
    ignore_case = False
    fixed = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            patterns.extend(tokens[index + 1 :])
            break
        if token in {"--invert-match", "--ignore-case", "--fixed-strings"}:
            invert = invert or token == "--invert-match"
            ignore_case = ignore_case or token == "--ignore-case"
            fixed = fixed or token == "--fixed-strings"
            index += 1
            continue
        if token in {"-e", "--regexp"}:
            index += 1
            if index >= len(tokens):
                raise SafePipelineConversionError("QFK_PIPELINE_INVALID_GREP", "grep -e 缺少模式")
            patterns.append(tokens[index])
            index += 1
            continue
        if token.startswith("-") and token != "-":
            flags = token[1:]
            if not flags or any(flag not in "ivF" for flag in flags):
                raise SafePipelineConversionError("QFK_PIPELINE_INVALID_GREP", f"不支持的 grep 参数: {token}")
            invert = invert or "v" in flags
            ignore_case = ignore_case or "i" in flags
            fixed = fixed or "F" in flags
            index += 1
            continue
        patterns.append(token)
        index += 1

    if len(patterns) != 1:
        raise SafePipelineConversionError(
            "QFK_PIPELINE_INVALID_GREP",
            "每个 grep 管道段必须且只能有一个模式；多段 grep 可表达 AND",
        )
    regex_probe = re.sub(r"\{\{[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\}\}", "", patterns[0])
    if not fixed and re.search(r"[.\^$*+?\[\]\\(){}|]", regex_probe):
        raise SafePipelineConversionError(
            "QFK_PIPELINE_REGEX_NEEDS_REVIEW",
            "grep 模式包含正则元字符，无法无损转换为字面量包含条件；请人工改写",
        )
    return patterns, invert, ignore_case


def _parse_awk(tokens: list[str]) -> tuple[list[int], list[int]]:
    if len(tokens) != 2:
        raise SafePipelineConversionError("QFK_PIPELINE_INVALID_AWK", "仅支持受控 awk 字段投影")
    program = tokens[1]
    match = re.fullmatch(
        r"\s*(?:NR\s*==\s*(\d+)\s*)?\{\s*print\s+(\$\d+(?:\s*,\s*\$\d+)*)\s*;?\s*}\s*",
        program,
    )
    if not match:
        raise SafePipelineConversionError(
            "QFK_PIPELINE_INVALID_AWK",
            "仅支持 awk '{print $N, $M}' 或 awk 'NR==N {print $C}'",
        )
    columns = [int(item.strip().removeprefix("$")) for item in match.group(2).split(",")]
    if any(column < 1 for column in columns) or len(columns) != len(set(columns)):
        raise SafePipelineConversionError("QFK_PIPELINE_INVALID_AWK", "awk 列号必须从 1 开始且不能重复")
    return columns, ([int(match.group(1))] if match.group(1) else [])


def _parse_cut(tokens: list[str]) -> tuple[str, list[int]]:
    delimiter: str | None = None
    fields: list[int] | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-d", "--delimiter"}:
            index += 1
            delimiter = tokens[index] if index < len(tokens) else None
        elif token.startswith("-d") and len(token) > 2:
            delimiter = token[2:]
        elif token in {"-f", "--fields"}:
            index += 1
            raw_field = tokens[index] if index < len(tokens) else ""
            fields = _parse_field_list(raw_field)
        elif token.startswith("-f") and len(token) > 2:
            raw_field = token[2:]
            fields = _parse_field_list(raw_field)
        else:
            raise SafePipelineConversionError("QFK_PIPELINE_INVALID_CUT", f"不支持的 cut 参数: {token}")
        index += 1
    if delimiter is None or len(delimiter) != 1 or not fields:
        raise SafePipelineConversionError("QFK_PIPELINE_INVALID_CUT", "仅支持 cut -d<单字符> -f<N,M,...>")
    return delimiter, fields


def _parse_field_list(raw: str) -> list[int] | None:
    if not re.fullmatch(r"\d+(?:,\d+)*", raw):
        return None
    fields = [int(item) for item in raw.split(",")]
    return fields if all(field > 0 for field in fields) and len(fields) == len(set(fields)) else None


def _parse_sed(tokens: list[str]) -> tuple[list[int], list[dict[str, int]]]:
    if len(tokens) != 3 or tokens[1] != "-n":
        raise SafePipelineConversionError("QFK_PIPELINE_INVALID_SED", "仅支持 sed -n 'Np' 或 sed -n 'N,Mp'")
    match = re.fullmatch(r"(\d+)(?:,(\d+))?p", tokens[2].strip())
    if not match:
        raise SafePipelineConversionError("QFK_PIPELINE_INVALID_SED", "仅支持 sed -n 'Np' 或 sed -n 'N,Mp'")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else None
    if start < 1 or (end is not None and end < start):
        raise SafePipelineConversionError("QFK_PIPELINE_INVALID_SED", "sed 行号必须从 1 开始且范围有序")
    if end is None:
        return [start], []
    return [], [{"start": start, "end": end}]
