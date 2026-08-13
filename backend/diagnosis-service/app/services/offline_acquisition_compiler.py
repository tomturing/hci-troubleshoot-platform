"""把 KBD 结构化信号编译为离线诊断可审计的只读采集命令。"""

from __future__ import annotations

import shlex
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from shared.resolution.catalog import domain_command_supported_versions
from shared.resolution.log_selector import build_log_selector
from shared.resolution.models import ResolutionStatus, SignalIntent, build_resolution_audit_snapshot
from shared.resolution.runtime import get_resolution_runtime


class CompiledSignalAcquisition(BaseModel):
    """离线资源同步消费的确定性采集编译结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    command_template: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    query_type: str
    timeout_seconds: int
    catalog_version: str
    resolution_status: ResolutionStatus
    resolution_snapshot: dict[str, Any]
    supported_product_versions: list[str]


_RUNTIME_PLACEHOLDERS = {
    "{{END}}": "{window_end}",
    "{{ABSOLUTE_TIME}}": "{window_end}",
    "{{START}}": "{window_start}",
    "{{HOST}}": "{target_id}",
    "{{TARGET_ID}}": "{target_id}",
    # 在线模式由 QKV 产出的对象标识，离线模式由客户选择的受影响对象提供。
    # 两者都表示同一个诊断目标，不需要为 KBD 复制两套采集命令。
    "{{VM_ID}}": "{target_id}",
}


def _parameterize_argv(tool: str, argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    """把安全选项值冻结到画像参数，运行时变量映射为内置计划字段。"""

    option_fields = {
        "-k": "keyword",
        "-f": "file",
        "-p": "path",
        "-t": "time_window",
        "-i": "request_id",
        "-l": "limit",
        "-c": "context_lines",
        "--timeout": "timeout",
        "--formatter": "formatter",
        "--container": "container",
    }
    positional_fields: dict[int, str] = {}
    if tool == "qfk_service" and len(argv) >= 5:
        positional_fields[2] = "container"
        positional_fields[3] = "service"
    elif tool == "qfk_system" and "system" in argv:
        command_index = argv.index("system") + 1
        if command_index < len(argv):
            positional_fields[command_index] = "command"

    template = list(argv)
    parameters: dict[str, Any] = {}
    option_value_indexes = {
        index + 1: option_fields[token] for index, token in enumerate(argv[:-1]) if token in option_fields
    }
    for index, token in enumerate(argv):
        runtime_placeholder = _RUNTIME_PLACEHOLDERS.get(token)
        if runtime_placeholder:
            template[index] = runtime_placeholder
            continue
        if "{{" in token or "}}" in token:
            raise ValueError(f"离线采集命令包含无法映射的运行时变量: {token}")
        field = positional_fields.get(index) or option_value_indexes.get(index)
        if not field:
            continue
        template[index] = "{" + field + "}"
        parameters[field] = token
    return template, parameters


def _resolver_input(
    tool: str,
    args: dict[str, Any],
    matcher: dict[str, Any],
) -> tuple[str, dict[str, Any], str]:
    """将 KBD acquire 契约映射为 Shared Resolution Runtime 输入。"""

    normalized = dict(args)
    target = normalized.pop("target", None)
    if isinstance(target, dict):
        for source, destination in (
            ("resource", "file"),
            ("path", "path"),
            ("time_window", "time_window"),
        ):
            if normalized.get(destination) in (None, "") and target.get(source) not in (None, ""):
                normalized[destination] = target[source]

    if tool.startswith("qkv_"):
        query = tool.removeprefix("qkv_")
        if query not in {"alert", "task", "dialog"}:
            raise ValueError(f"不支持的 QKV 采集工具: {tool}")
        normalized["query"] = query
        if query == "dialog":
            paths = normalized.get("paths") or ["/sf/log/today", "/sf/log/today/vt"]
            normalized["path"] = normalized.get("path") or paths[0]
        return "qkv", normalized, "log" if query == "dialog" else "json"

    if not tool.startswith("qfk_"):
        raise ValueError(f"KBD 采集工具不在 QKV/QFK 契约内: {tool}")
    namespace = tool.removeprefix("qfk_")
    resolver_id = "domain" if namespace in {"vm", "network", "storage", "hardware", "platform"} else namespace
    if resolver_id not in {"log", "service", "system", "domain"}:
        raise ValueError(f"不支持的 QFK 采集工具: {tool}")

    if namespace == "log":
        pattern = matcher.get("pattern")
        keywords = (
            [str(item) for item in pattern if str(item)]
            if matcher.get("type") == "keyword" and isinstance(pattern, list)
            else [str(pattern)]
            if matcher.get("type") == "keyword" and pattern
            else []
        )
        matcher_rows = (matcher.get("extract") or {}).get("rows") or {}
        filter_keywords = [str(value) for value in matcher_rows.get("include") or [] if str(value)]
        filter_keywords.extend(
            str(value)
            for produce in normalized.pop("_produces", [])
            if isinstance(produce, dict)
            for value in (((produce.get("extract") or {}).get("rows") or {}).get("include") or [])
            if str(value)
        )
        selector, extended_regex, matcher_type = build_log_selector(
            matcher=matcher,
            keywords=keywords,
            filter_keywords=filter_keywords,
            resource_keyword=normalized.get("resource_keyword"),
            request_id=normalized.get("request_id"),
        )
        # REQUEST_ID 是在线 QKV 变量，不是离线会话的固有字段。只要 Matcher/取值规则
        # 已提供有界 selector，离线 Collector 就省略未解析的 -i，而不是生成无法执行的制品。
        if normalized.get("request_id") == "{{REQUEST_ID}}" and selector:
            normalized.pop("request_id", None)
        normalized.update(
            {
                "keyword": selector,
                "extended_regex": extended_regex,
                "matcher_type": matcher_type,
            }
        )
    elif namespace == "service":
        normalized["service"] = normalized.get("service") or normalized.get("resource_keyword")
        normalized["action"] = normalized.get("action") or normalized.get("command") or "status"
    elif namespace == "system":
        normalized["command"] = normalized.get("command") or normalized.get("sub_command")
    else:
        produces = normalized.pop("_produces", [])
        json_extraction = (matcher.get("extract") or {}).get("type") == "json" or any(
            isinstance(item, dict) and (item.get("extract") or {}).get("type") == "json"
            for item in produces
        )
        command_args = list(normalized.get("command_args") or [])
        legacy_formatter = (
            command_args[command_args.index("--formatter") + 1]
            if "--formatter" in command_args and command_args.index("--formatter") + 1 < len(command_args)
            else None
        )
        formatter = normalized.get("formatter") or legacy_formatter
        if json_extraction and formatter not in (None, "json"):
            raise ValueError("JSON 取值规则要求 qfk 领域命令使用 formatter=json")
        if json_extraction and formatter is None:
            normalized["formatter"] = "json"
        normalized["domain"] = namespace

    query_type = "log" if namespace == "log" else "json" if normalized.get("formatter") == "json" else "command_output"
    if query_type == "command_output" and "--formatter" in list(normalized.get("command_args") or []):
        arguments = list(normalized["command_args"])
        index = arguments.index("--formatter")
        if index + 1 < len(arguments) and arguments[index + 1] == "json":
            query_type = "json"
    return resolver_id, normalized, query_type


def compile_signal_acquisition(
    *,
    tool: str,
    args: dict[str, Any],
    matcher: dict[str, Any] | None = None,
    produces: list[dict[str, Any]] | None = None,
) -> CompiledSignalAcquisition:
    """使用统一 Resolver 编译 KBD 信号，不读取 Tool 的展示模板。"""

    compiler_args = dict(args)
    namespace = tool.removeprefix("qfk_")
    if tool == "qfk_log" or namespace in {"vm", "network", "storage", "hardware", "platform"}:
        compiler_args["_produces"] = list(produces or [])
    resolver_id, normalized_args, query_type = _resolver_input(tool, compiler_args, matcher or {})
    runtime = get_resolution_runtime()
    plan = runtime.compile(SignalIntent(resolver_id=resolver_id, tool=tool, args=normalized_args, source="kbd_sync"))
    acquisition = runtime.resolve(plan)
    if acquisition.status is ResolutionStatus.BLOCKED:
        message = "；".join(issue.message for issue in acquisition.issues) or "信号无法编译为只读采集命令"
        raise ValueError(message)
    if not acquisition.argv:
        raise ValueError(f"{tool} 未生成可执行 argv")
    template_argv, parameters = _parameterize_argv(tool, acquisition.argv)
    for field in {"limit", "context_lines", "timeout"}.intersection(parameters):
        parameters[field] = int(parameters[field])
    snapshot = build_resolution_audit_snapshot(plan, acquisition)
    supported_product_versions = ["6.*", "7.*", "8.*"]
    if resolver_id == "domain":
        supported_product_versions = domain_command_supported_versions(
            str(normalized_args["domain"]),
            shlex.split(str(normalized_args["command"])),
        ) or supported_product_versions
    timeout = max(1, min(int(normalized_args.get("timeout") or 60), 300))
    command_template = " ".join(
        token if token.startswith("{") and token.endswith("}") else shlex.quote(token) for token in template_argv
    )
    return CompiledSignalAcquisition(
        tool=tool,
        command_template=command_template,
        parameters=parameters,
        query_type=query_type,
        timeout_seconds=timeout,
        catalog_version=acquisition.catalog_version,
        resolution_status=acquisition.status,
        resolution_snapshot=snapshot.model_dump(mode="json"),
        supported_product_versions=supported_product_versions,
    )
