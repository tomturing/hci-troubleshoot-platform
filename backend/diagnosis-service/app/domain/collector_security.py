"""Collector 命令模板安全校验、参数验证和渲染。"""

import re
import shlex
import string
from typing import Any

from jsonschema import Draft202012Validator

from app.errors import DiagnosisError

BUILTIN_PARAMETERS = frozenset({"target_id", "target_type", "window_start", "window_end"})
FORBIDDEN_EXECUTABLES = frozenset({"sh", "bash", "dash", "zsh", "eval", "env", "xargs", "sudo"})
READ_ONLY_EXECUTABLES = frozenset(
    {
        "acli", "date", "df", "dmidecode", "echo", "ethtool", "free", "hostname", "ip",
        "hci-api-read", "journalctl", "kubectl", "lscpu", "lsblk", "manual-attachment", "nvme",
        "raidcli", "smartctl", "ss", "systemctl", "task", "true", "uname", "uptime",
    }
)
MUTATING_ARGUMENTS = frozenset(
    {
        "add", "apply", "attach-ns", "change", "clear", "connect", "create", "create-ns", "del",
        "delete", "delete-ns", "detach-ns", "disable", "disconnect", "enable", "exec", "flush",
        "format", "fw-commit", "fw-download", "install", "kill", "offline", "online", "patch",
        "poweroff", "reboot", "remove", "replace", "reset", "restart", "rm", "sanitize",
        "security-send", "set", "set-feature", "shutdown", "start", "stop", "subsystem-reset",
        "update", "upgrade", "write", "write-zeroes",
    }
)
READ_ONLY_SUBCOMMANDS = {
    "kubectl": frozenset({"describe", "get", "logs", "top", "version"}),
    "raidcli": frozenset({"show"}),
    "systemctl": frozenset({"is-active", "is-enabled", "list-units", "show", "status"}),
    "task": frozenset({"get", "list", "show"}),
}
FORBIDDEN_EXECUTABLE_OPTIONS = {
    "ethtool": frozenset({"-A", "-C", "-G", "-K", "-L", "-N", "-Q", "-X", "-s", "--change"}),
    "journalctl": frozenset({"--flush", "--relinquish-var", "--rotate", "--sync"}),
    "smartctl": frozenset({"-B", "-o", "-s", "-t", "--offlineauto", "--saveauto", "--smart", "--test"}),
}
FORBIDDEN_TEMPLATE_TOKENS = ("|", ";", ">", "<", "`", "$(", "\n", "\r", "&&", "||")
PLACEHOLDER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
HCI_API_REQUEST_PATTERN = re.compile(r"^GET (/[^ \x00-\x1f]*)$")


def validate_collector_contract(command_template: str, parameter_schema: dict[str, Any]) -> list[str]:
    """校验固定命令模板和参数 Schema，返回占位符列表。"""

    if any(token in command_template for token in FORBIDDEN_TEMPLATE_TOKENS):
        raise DiagnosisError(
            code="UNSAFE_COLLECTOR_COMMAND",
            message="Collector 命令模板禁止管道、重定向、命令替换和多命令拼接",
            http_status=422,
        )
    try:
        tokens = shlex.split(command_template, posix=True)
    except ValueError as exc:
        raise DiagnosisError(
            code="INVALID_COLLECTOR_COMMAND",
            message="Collector 命令模板无法解析",
            http_status=422,
        ) from exc
    if not tokens:
        raise DiagnosisError(code="INVALID_COLLECTOR_COMMAND", message="Collector 命令不能为空", http_status=422)
    executable = tokens[0].rsplit("/", 1)[-1]
    if executable in FORBIDDEN_EXECUTABLES:
        raise DiagnosisError(
            code="UNSAFE_COLLECTOR_EXECUTABLE",
            message="Collector 不允许通过通用 Shell 或命令编排器间接执行",
            http_status=422,
        )
    if parameter_schema.get("type") != "object":
        raise DiagnosisError(
            code="INVALID_PARAMETER_SCHEMA",
            message="parameter_schema.type 必须为 object",
            http_status=422,
        )
    properties = parameter_schema.get("properties", {})
    if not isinstance(properties, dict):
        raise DiagnosisError(
            code="INVALID_PARAMETER_SCHEMA",
            message="parameter_schema.properties 必须为对象",
            http_status=422,
        )
    if parameter_schema.get("additionalProperties") is not False:
        raise DiagnosisError(
            code="INVALID_PARAMETER_SCHEMA",
            message="parameter_schema 必须显式禁止 additionalProperties",
            http_status=422,
        )
    try:
        Draft202012Validator.check_schema(parameter_schema)
    except Exception as exc:
        raise DiagnosisError(
            code="INVALID_PARAMETER_SCHEMA",
            message="parameter_schema 不是合法 JSON Schema",
            http_status=422,
        ) from exc

    placeholders: list[str] = []
    for token in tokens:
        parsed = list(string.Formatter().parse(token))
        fields = [field for _, field, _, _ in parsed if field is not None]
        if fields and (len(fields) != 1 or token != "{" + fields[0] + "}"):
            raise DiagnosisError(
                code="UNSAFE_COLLECTOR_PLACEHOLDER",
                message="Collector 参数占位符必须独占一个命令参数",
                http_status=422,
            )
        for _literal, field, format_spec, conversion in parsed:
            if field is None:
                continue
            if not PLACEHOLDER_PATTERN.fullmatch(field) or format_spec or conversion:
                raise DiagnosisError(
                    code="INVALID_COLLECTOR_PLACEHOLDER",
                    message="Collector 参数占位符格式不合法",
                    http_status=422,
                )
            if field not in properties and field not in BUILTIN_PARAMETERS:
                raise DiagnosisError(
                    code="UNKNOWN_COLLECTOR_PLACEHOLDER",
                    message=f"Collector 参数占位符 {field} 未在 parameter_schema 中声明",
                    http_status=422,
                )
            placeholders.append(field)
    _validate_read_only_tokens(executable, tokens[1:])
    return placeholders


def _validate_read_only_tokens(executable: str, arguments: list[str]) -> None:
    """对已完成语法校验的 argv 模板执行正向只读策略。"""

    if executable not in READ_ONLY_EXECUTABLES:
        raise DiagnosisError(
            code="COLLECTOR_EXECUTABLE_NOT_ALLOWLISTED",
            message=f"Collector 可执行程序 {executable} 不在只读采集白名单中",
            http_status=422,
        )
    normalized_arguments = {
        token.casefold().lstrip("-").split("=", 1)[0]
        for token in arguments
    }
    denied = sorted(normalized_arguments.intersection(MUTATING_ARGUMENTS))
    forbidden_options = FORBIDDEN_EXECUTABLE_OPTIONS.get(executable, frozenset())
    denied_options = sorted(token for token in arguments if token.split("=", 1)[0] in forbidden_options)
    if executable == "journalctl":
        denied_options.extend(token for token in arguments if token.startswith("--vacuum-"))
    if denied or denied_options:
        raise DiagnosisError(
            code="MUTATING_COLLECTOR_COMMAND",
            message="Collector 命令包含变更系统状态的参数",
            http_status=422,
            details={"arguments": sorted(set(denied + denied_options))},
        )
    allowed_subcommands = READ_ONLY_SUBCOMMANDS.get(executable)
    if allowed_subcommands is not None:
        subcommand = next((token.casefold() for token in arguments if not token.startswith("-")), "")
        if subcommand not in allowed_subcommands:
            raise DiagnosisError(
                code="COLLECTOR_SUBCOMMAND_NOT_ALLOWLISTED",
                message=f"Collector 不允许执行 {executable} {subcommand}".strip(),
                http_status=422,
            )


def render_collector_command(
    command_template: str,
    parameter_schema: dict[str, Any],
    values: dict[str, Any],
) -> tuple[list[str], str]:
    """校验参数并渲染为 argv 和安全展示命令。"""

    placeholders = validate_collector_contract(command_template, parameter_schema)
    properties = parameter_schema.get("properties", {})
    schema_values = {key: values[key] for key in properties if key in values}
    errors = sorted(
        Draft202012Validator(parameter_schema).iter_errors(schema_values),
        key=lambda error: list(error.path),
    )
    if errors:
        raise DiagnosisError(
            code="COLLECTOR_PARAMETER_VALIDATION_FAILED",
            message="Collector 参数校验失败",
            http_status=422,
            details={"errors": [error.message for error in errors]},
        )

    raw_tokens = shlex.split(command_template, posix=True)
    argv: list[str] = []
    for token in raw_tokens:
        if token.startswith("{") and token.endswith("}") and token[1:-1] in placeholders:
            field = token[1:-1]
            if field not in values:
                raise DiagnosisError(
                    code="UNRESOLVED_COLLECTOR_PARAMETER",
                    message=f"Collector 参数 {field} 尚未解析",
                    http_status=409,
                )
            value = values[field]
            if isinstance(value, bool):
                argv.append("true" if value else "false")
            else:
                argv.append(str(value))
        else:
            argv.append(token)
    _validate_read_only_tokens(argv[0].rsplit("/", 1)[-1], argv[1:])
    return argv, shlex.join(argv)


def validate_hci_api_contract(request_template: str, parameter_schema: dict[str, Any]) -> None:
    """校验只读 HCI API 请求；P0 只允许无动态 URL 拼接的相对 GET 路径。"""

    match = HCI_API_REQUEST_PATTERN.fullmatch(request_template.strip())
    if match is None:
        raise DiagnosisError(
            code="UNSAFE_HCI_API_REQUEST",
            message="HCI API Collector 只允许固定相对路径的 GET 请求",
            http_status=422,
        )
    path = match.group(1)
    if ".." in path or "{" in path or "}" in path or "://" in path:
        raise DiagnosisError(
            code="UNSAFE_HCI_API_PATH",
            message="HCI API 路径禁止父级跳转、动态拼接和绝对 URL",
            http_status=422,
        )
    validate_collector_contract("hci-api-read", parameter_schema)


def validate_manual_guide(guide: str, parameter_schema: dict[str, Any]) -> None:
    """校验人工附件采集指引；指引是展示文本，不进入 Shell 执行。"""

    if "\x00" in guide or len(guide.strip()) < 2:
        raise DiagnosisError(
            code="INVALID_MANUAL_ATTACHMENT_GUIDE",
            message="人工附件采集指引不能为空或包含 NUL 字符",
            http_status=422,
        )
    validate_collector_contract("manual-attachment", parameter_schema)
