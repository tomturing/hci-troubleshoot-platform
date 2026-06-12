"""
工具执行前语义校验。

这里放在真实执行器之前，用于拦截 LLM 规划层面的非法工具调用。
校验失败不代表远端命令执行失败，调用方应把失败原因反馈给 ReAct 循环重新规划。
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.tools.acli.container_exec import ALLOWED_BASH_CONTAINERS, build_container_command

__all__ = [
    "ALLOWED_BASH_CONTAINERS",
    "ToolSemanticValidator",
    "ValidationIssue",
    "ValidationResult",
    "build_container_command",
    "get_acli_catalog_commands",
]

CATALOG_PATH = Path(__file__).with_name("catalog") / "acli_command_catalog.json"
_BASH_FORBIDDEN_PREFIX_RE = re.compile(r"(^|\s)(docker\s+exec|kubectl\s+exec|nsenter)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationIssue:
    """单条语义校验问题。"""

    code: str
    message: str
    field: str | None = None
    level: str = "error"
    suggested_fix: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    """语义校验结果。"""

    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    @classmethod
    def ok_result(cls) -> ValidationResult:
        return cls(ok=True, issues=[])

    @classmethod
    def error(
        cls,
        code: str,
        message: str,
        *,
        field: str | None = None,
        suggested_fix: str | None = None,
    ) -> ValidationResult:
        return cls(ok=False, issues=[ValidationIssue(code=code, message=message, field=field, suggested_fix=suggested_fix)])

    def to_feedback(self, tool_name: str) -> str:
        """生成给 LLM 的规划反馈。"""
        lines = [
            "【工具调用未通过执行前校验】",
            f"工具：{tool_name}",
        ]
        for issue in self.issues:
            location = f"字段：{issue.field}；" if issue.field else ""
            lines.append(f"错误：{location}{issue.message}（{issue.code}）")
            if issue.suggested_fix:
                lines.append(f"建议：{issue.suggested_fix}")
        lines.append("请重新思考并生成合法 tool_call；不要向用户报告为真实命令执行失败。")
        return "\n".join(lines)


def _normalize_spaces(value: str) -> str:
    return " ".join(value.strip().split())


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _load_catalog_commands() -> set[str]:
    with CATALOG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    commands = data.get("commands") or []
    result: set[str] = set()
    for item in commands:
        if isinstance(item, dict) and item.get("command"):
            result.add(_normalize_spaces(str(item["command"])))
        elif isinstance(item, str):
            result.add(_normalize_spaces(item))
    return result


@lru_cache(maxsize=1)
def get_acli_catalog_commands() -> frozenset[str]:
    """读取本地 aCLI catalog 快照。"""
    return frozenset(_load_catalog_commands())


def _strip_global_options(tokens: list[str]) -> list[str]:
    """去除 aCLI 全局参数，返回命令路径 token。"""
    result: list[str] = []
    i = 0
    value_options = {"--formatter", "--cluster", "--timeout", "--container"}
    bool_options = {"--debug", "--force", "--version", "--help", "-?", "-h"}
    while i < len(tokens):
        token = tokens[i]
        if token in value_options:
            i += 2
            continue
        if any(token.startswith(f"{option}=") for option in value_options):
            i += 1
            continue
        if token in bool_options:
            i += 1
            continue
        result.append(token)
        i += 1
    return result


def _catalog_matches(path_tokens: list[str], catalog_commands: frozenset[str]) -> bool:
    """判断命令 token 是否以 catalog 中的命令路径为前缀。"""
    for catalog_command in catalog_commands:
        catalog_tokens = catalog_command.split()
        if len(path_tokens) >= len(catalog_tokens) and path_tokens[: len(catalog_tokens)] == catalog_tokens:
            return True
    return False


class ToolSemanticValidator:
    """工具领域语义校验器。"""

    @classmethod
    def validate(
        cls,
        tool_name: str,
        args: dict[str, Any],
        tool_def: Any | None = None,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        if tool_name == "bash_exec":
            return cls._validate_bash_exec(args)
        if tool_name == "acli_exec":
            return cls._validate_acli_exec(args)
        return ValidationResult.ok_result()

    @staticmethod
    def _validate_bash_exec(args: dict[str, Any]) -> ValidationResult:
        container = args.get("container")
        command = str(args.get("command") or "").strip()

        if not container:
            return ValidationResult.error(
                "BASH_CONTAINER_REQUIRED",
                "bash_exec 必须指定目标容器",
                field="container",
                suggested_fix="选择 asv-con、vn-con、vn-agent、vs-cp-manager 之一",
            )
        if container not in ALLOWED_BASH_CONTAINERS:
            return ValidationResult.error(
                "BASH_CONTAINER_INVALID",
                f"bash_exec container 只能是 {sorted(ALLOWED_BASH_CONTAINERS)}",
                field="container",
            )
        if not command:
            return ValidationResult.error("BASH_COMMAND_EMPTY", "bash_exec.command 不能为空", field="command")
        if _BASH_FORBIDDEN_PREFIX_RE.search(command):
            return ValidationResult.error(
                "BASH_CONTAINER_PREFIX_FORBIDDEN",
                "bash_exec.command 不允许包含 docker exec、kubectl exec 或 nsenter，容器进入方式由服务端拼装",
                field="command",
            )
        tokens = _tokenize(command)
        if tokens and tokens[0] == "acli":
            return ValidationResult.error(
                "BASH_ACLI_FORBIDDEN",
                "aCLI 命令必须使用 acli_exec，不允许混入 bash_exec",
                field="command",
                suggested_fix="改用 acli_exec(command=...)",
            )
        return ValidationResult.ok_result()

    @staticmethod
    def _validate_acli_exec(args: dict[str, Any]) -> ValidationResult:
        command = str(args.get("command") or "").strip()
        if not command:
            return ValidationResult.error("ACLI_COMMAND_EMPTY", "acli_exec.command 不能为空", field="command")

        tokens = _tokenize(command)
        if not tokens:
            return ValidationResult.error("ACLI_COMMAND_PARSE_FAILED", "无法解析 aCLI 命令", field="command")
        if tokens[0] != "acli":
            return ValidationResult.error("ACLI_PREFIX_REQUIRED", "acli_exec.command 必须以 acli 开头", field="command")
        if "--help" in tokens or "-?" in tokens or "-h" in tokens:
            return ValidationResult.ok_result()

        path_tokens = _strip_global_options(tokens)
        normalized_path = _normalize_spaces(" ".join(path_tokens))
        if normalized_path == "acli acli command list":
            return ValidationResult.ok_result()

        catalog = get_acli_catalog_commands()
        if not _catalog_matches(path_tokens, catalog):
            return ValidationResult.error(
                "ACLI_COMMAND_NOT_IN_CATALOG",
                f"命令路径不在 aCLI catalog 中：{normalized_path}",
                field="command",
                suggested_fix="先执行 acli ... --help 或改用 catalog 中支持的命令",
            )
        return ValidationResult.ok_result()
