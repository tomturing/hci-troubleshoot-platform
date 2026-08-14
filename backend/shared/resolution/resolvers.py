"""六个领域 Resolver 的确定性实现。"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable
from typing import Any, Protocol

from shared.resolution.catalog import (
    command_path_known,
    domain_command_requirements,
    log_aliases,
    normalize_qkv_keyword,
    resolution_catalog_version,
    resolve_qkv_action,
)
from shared.resolution.models import (
    ResolutionIssue,
    ResolutionPlan,
    ResolutionStatus,
    ResolvedAcquisition,
    SignalIntent,
)
from shared.schemas.acquirer_args import (
    DEFAULT_SIGNAL_TIMEOUT_SECONDS,
    VALID_SERVICE_CONTAINERS,
    VALID_SYSTEM_CONTAINERS,
    normalize_qfk_system_args,
)
from shared.schemas.log_source_catalog import (
    LOG_ROOT,
    LOG_SOURCE_CATALOG_VERSION,
    REQUEST_ARTIFACT_ROOT,
    normalize_absolute_log_time,
    normalize_log_path,
    resolve_log_source,
)


class Resolver(Protocol):
    resolver_id: str

    def compile(self, intent: SignalIntent) -> ResolutionPlan: ...

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition: ...


def _issue(code: str, message: str, *, field: str | None = None, level: str = "error") -> ResolutionIssue:
    return ResolutionIssue(code=code, message=message, field=field, level=level)


def _blocked(
    intent: SignalIntent, issues: list[ResolutionIssue], *, catalog_version: str = "unknown"
) -> ResolutionPlan:
    return ResolutionPlan(
        resolver_id=intent.resolver_id,
        tool=intent.tool,
        canonical_args=intent.args,
        catalog_version=catalog_version,
        status=ResolutionStatus.BLOCKED,
        issues=issues,
    )


def _command_parts(command: str, *, dotted: bool = True) -> list[str]:
    raw = str(command or "").strip()
    if not raw:
        raise ValueError("command 不能为空")
    if dotted and raw.startswith("acli.") and " " not in raw:
        return raw.split(".")
    parts = shlex.split(raw)
    if not parts:
        raise ValueError("command 无法分词")
    return parts


class LogResolver:
    resolver_id = "log"

    # 用户、KBD 和现场常见别名。精确别名优先于模糊纠错，避免把两个真实文件混淆。
    ALIASES = {
        "vtpdaemon": "sfvt_vtpdaemon.log",
        "vtpdeamon": "sfvt_vtpdaemon.log",
        "vtpdaeamon": "sfvt_vtpdaemon.log",
        "sfvt_vtpdeamon.log": "sfvt_vtpdaemon.log",
    }

    @classmethod
    def canonical_file(cls, value: str | None) -> tuple[str, list[str]]:
        raw = str(value or "").strip()
        if not raw:
            return "", []
        basename = posixpath.basename(raw)
        aliases = {**cls.ALIASES, **log_aliases()}
        if basename in aliases:
            return aliases[basename], [basename]
        # source_id/keyword 也可定位到 Catalog 的精确 basename。
        candidates = []
        for definition in _log_definitions():
            if definition.source_id == raw:
                candidates.append(definition.file_pattern)
        if candidates:
            pattern = candidates[0]
            if pattern.startswith("r"):
                pattern = pattern[1:]
            # 只对当前 Catalog 中无动态段的 source_id 展开。
            if re.fullmatch(r"[A-Za-z0-9_.-]+", pattern):
                return pattern, [raw]
        return basename, []

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        args = dict(intent.args)
        raw_path = args.get("path")
        raw_file = args.get("file") or args.get("source_id")
        if not raw_file and isinstance(raw_path, str) and "/" in raw_path:
            raw_file = posixpath.basename(raw_path)
        is_request_artifact = bool(
            isinstance(raw_path, str)
            and (raw_path == REQUEST_ARTIFACT_ROOT or raw_path.startswith(f"{REQUEST_ARTIFACT_ROOT}/"))
        )
        if is_request_artifact:
            if not args.get("request_id"):
                return _blocked(
                    intent,
                    [_issue("LOG_REQUEST_ID_REQUIRED", "/sf/data/local 仅允许携带 request_id 的辅助关联搜索")],
                    catalog_version=LOG_SOURCE_CATALOG_VERSION,
                )
            source = {
                "source_id": "request_artifact_scope",
                "family": "request_artifact",
                "parser": "plain_text",
                "predicates": ("keyword", "regex", "state", "exists"),
                "runtime_supported": True,
                "default_path": REQUEST_ARTIFACT_ROOT,
            }
            canonical_file, aliases = "", []
        else:
            canonical_file, aliases = self.canonical_file(raw_file)
            if not canonical_file:
                return _blocked(
                    intent,
                    [_issue("LOG_FILE_REQUIRED", "qfk_log 必须提供日志 file basename 或 source_id", field="file")],
                    catalog_version=LOG_SOURCE_CATALOG_VERSION,
                )
        args["file"] = canonical_file
        path_hint = None
        if not is_request_artifact and isinstance(raw_path, str) and raw_path and not raw_path.startswith("/"):
            # vt/sfvt_x.log 这类输入把 basename 与相对上层目录拆开，绝不把相对路径直接交给 aCLI。
            prefix = posixpath.dirname(raw_path)
            if posixpath.basename(raw_path) == canonical_file and prefix:
                path_hint = prefix.strip("/")
                args["path"] = None
            elif "/" in raw_path:
                return _blocked(
                    intent,
                    [_issue("LOG_RELATIVE_PATH_AMBIGUOUS", "相对日志路径无法安全拆分为目录和 basename", field="path")],
                    catalog_version=LOG_SOURCE_CATALOG_VERSION,
                )
        elif isinstance(raw_path, str) and raw_path.startswith("/") and posixpath.basename(raw_path) == canonical_file:
            # 完整绝对文件路径属于目标文件本身；内部契约保存其 dirname，最终输出再组合 basename。
            args["path"] = posixpath.dirname(raw_path)
        if not is_request_artifact:
            try:
                source = resolve_log_source(
                    canonical_file,
                    source_family=str(args.get("source_family") or "auto"),
                    path=args.get("path"),
                    parser=args.get("parser"),
                )
            except ValueError as exc:
                return _blocked(
                    intent,
                    [_issue("LOG_CATALOG_REJECTED", str(exc))],
                    catalog_version=LOG_SOURCE_CATALOG_VERSION,
                )
        if not source.get("runtime_supported", True):
            return _blocked(
                intent,
                [_issue("LOG_RUNTIME_UNSUPPORTED", f"日志源不能由 qfk_log 获取，应使用 {source.get('acquisition')}")],
                catalog_version=LOG_SOURCE_CATALOG_VERSION,
            )
        if args.get("include_archives") is True and args.get("archive_precheck") != "verified":
            return _blocked(
                intent,
                [_issue("LOG_ARCHIVE_PRECHECK_REQUIRED", "include_archives=true 必须先通过 archive_precheck=verified")],
                catalog_version=LOG_SOURCE_CATALOG_VERSION,
            )
        matcher_type = str(args.get("matcher_type") or "")
        # producer 表示“有界采集后按 orchestrate.produces 取值”，不是一个判定
        # 谓词。其输出范围已由 build_log_selector 的 include/resource/request_id
        # 约束，不应拿它与日志源的 Matcher predicate 列表比较。
        if matcher_type and matcher_type != "producer" and matcher_type not in source.get("predicates", ()):
            return _blocked(
                intent,
                [
                    _issue(
                        "LOG_MATCHER_UNSUPPORTED",
                        f"日志源 {source.get('source_id')} 的 parser={source.get('parser')} 不支持 {matcher_type} predicate",
                        field="matcher.type",
                    )
                ],
                catalog_version=LOG_SOURCE_CATALOG_VERSION,
            )
        args["path_hint"] = path_hint
        args["aliases_used"] = aliases
        args["source"] = source
        args["time_window"] = normalize_absolute_log_time(args.get("time_window"))
        candidates = [str(raw_path)] if is_request_artifact else self._candidate_paths(source, args)
        return ResolutionPlan(
            resolver_id=self.resolver_id,
            tool=intent.tool or "qfk_log",
            canonical_args=args,
            argv_template=["acli", "log", "get"],
            candidates=[{"path": item, "file": canonical_file} for item in candidates],
            catalog_version=LOG_SOURCE_CATALOG_VERSION,
        )

    @staticmethod
    def _candidate_paths(source: dict[str, Any], args: dict[str, Any]) -> list[str]:
        explicit = args.get("path")
        absolute_time = args.get("time_window")
        if explicit and not (absolute_time and str(explicit).rstrip("/") in {"/sf/log/today", "/sf/log/today/vt"}):
            return [normalize_log_path(str(explicit))]
        hint = str(args.get("path_hint") or "").strip("/")
        family = str(source.get("family") or "whitebox")
        if family == "blackbox":
            root = "/sf/log/blackbox"
            if absolute_time and not str(absolute_time).startswith("{{"):
                return [f"{root}/{str(absolute_time)[:10].replace('-', '')}"]
            return [f"{root}/today"]
        if family == "vn_blackbox":
            root = "/sf/log/vn-blackbox"
            if absolute_time and not str(absolute_time).startswith("{{"):
                return [f"{root}/{str(absolute_time)[:10].replace('-', '')}"]
            return [f"{root}/today"]
        if family == "pod":
            return ["/sf/log/pods"]
        if absolute_time and not str(absolute_time).startswith("{{"):
            day = int(str(absolute_time)[8:10])
            tokens = [str(day), f"{day:02d}"]
            subpath = str(source.get("date_subpath") or hint).strip("/")
            return [
                f"{LOG_ROOT}/{token}/{subpath}" if subpath else f"{LOG_ROOT}/{token}" for token in dict.fromkeys(tokens)
            ]
        if hint:
            return [f"{LOG_ROOT}/{hint}"]
        default = source.get("default_path") or LOG_ROOT
        return [str(default)]

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition:
        if plan.status is ResolutionStatus.BLOCKED:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=plan.issues,
            )
        context = context or {}
        variables = dict(context.get("variables") or {})
        search_paths = [str(item["path"]) for item in plan.candidates]
        file_name = str(plan.canonical_args.get("file") or "")
        candidates = [posixpath.join(path, file_name) if file_name else path for path in search_paths]
        probe_object = context.get("probe")
        probe: Callable[[str], bool] | None = context.get("path_exists")
        if not callable(probe) and callable(getattr(probe_object, "path_exists", None)):
            probe = probe_object.path_exists
        selected_file = next((path for path in candidates if probe(path)), None) if callable(probe) else None
        probe_confirmed = selected_file is not None
        if selected_file is None:
            # 无 probe 时使用第一候选；这是 Catalog 可验证的确定性路径，不声称目标文件已存在。
            selected_file = candidates[0] if candidates else LOG_ROOT
        selected = posixpath.dirname(selected_file) if file_name else selected_file
        args = plan.canonical_args
        argv = ["acli", "log", "get"]
        selector = args.get("keyword") or args.get("resource_keyword")
        if selector:
            if args.get("extended_regex"):
                argv.append("-E")
            argv.extend(["-k", str(selector)])
        if args.get("request_id"):
            argv.extend(["-i", str(args["request_id"])])
        if args.get("file"):
            argv.extend(["-f", str(args["file"])])
        if selected:
            argv.extend(["-p", selected])
        if args.get("time_window"):
            argv.extend(["-t", str(args["time_window"])])
        if args.get("context_lines"):
            argv.extend(["-c", str(args["context_lines"])])
        if args.get("include_archives"):
            argv.append("-g")
        verified = probe_confirmed
        issues = (
            []
            if verified
            else [
                _issue(
                    "LOG_PATH_UNPROBED",
                    "未提供现场路径探针；路径由 Catalog 规则确定但尚未证明文件存在",
                    level="warning",
                )
            ]
        )
        return ResolvedAcquisition(
            resolver_id=self.resolver_id,
            tool=plan.tool,
            status=ResolutionStatus.VERIFIED if verified else ResolutionStatus.NEEDS_PROBE,
            argv=argv,
            command=" ".join(shlex.quote(item) for item in argv),
            absolute_path=selected_file,
            candidates_tried=candidates,
            resolution_rule="catalog+END+path_hint+probe" if callable(probe) else "catalog+END+path_hint",
            catalog_version=plan.catalog_version,
            variables_used=variables,
            issues=issues,
            evidence={
                "file": args.get("file"),
                "search_path": selected,
                "aliases_used": args.get("aliases_used", []),
                "path_exists": verified,
            },
        )


def _log_definitions() -> tuple[Any, ...]:
    from shared.schemas.log_source_catalog import LOG_SOURCE_CATALOG

    return LOG_SOURCE_CATALOG


class SystemResolver:
    resolver_id = "system"

    # ``cat`` is intentionally not an aCLI catalog command: KBD uses it only for
    # deterministic read-only configuration-file inspection and the existing
    # qfk_system contract validates the target path separately.  Treating it as
    # an unknown catalog path here would make the Shared Runtime stricter than
    # the execution contract and reject otherwise valid signals.
    _SPECIAL_READONLY_COMMANDS = frozenset({"cat"})

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        args = dict(intent.args)
        try:
            normalized = normalize_qfk_system_args(args)
            command = str(normalized["command"])
            command_args = list(normalized.get("command_args") or [])
            timeout = int(normalized.get("timeout") or DEFAULT_SIGNAL_TIMEOUT_SECONDS)
            if not 1 <= timeout <= 300:
                raise ValueError("timeout 必须在 1-300 秒之间")
            container = normalized.get("container")
            if container == "host":
                container = None
            if container and container not in VALID_SYSTEM_CONTAINERS:
                raise ValueError(f"qfk_system.container 不在允许集合: {container}")
            formatter = normalized.get("formatter")
            if formatter and formatter not in {"xml", "csv", "keyvalue", "json"}:
                raise ValueError("qfk_system.formatter 必须是 xml/csv/keyvalue/json 之一")
            parts = ["acli"]
            if normalized.get("cluster") or normalized.get("host") == "cluster":
                parts.append("--cluster")
            parts.extend(["--timeout", str(timeout)])
            if formatter:
                parts.extend(["--formatter", str(formatter)])
            if container:
                parts.extend(["--container", str(container)])
            parts.extend(["system", command, *command_args])
        except (TypeError, ValueError) as exc:
            return _blocked(intent, [_issue("SYSTEM_COMMAND_INVALID", str(exc), field="command")])
        args = normalized
        args["argv"] = parts
        command_path = ["acli", "system", command]
        known = command_path_known(command_path) or command in self._SPECIAL_READONLY_COMMANDS
        if not known:
            return _blocked(
                intent,
                [
                    _issue(
                        "SYSTEM_COMMAND_UNKNOWN",
                        "命令路径不在当前 Catalog；先用只读 --help/command list 核对并更新 Catalog",
                        field="command",
                    )
                ],
                catalog_version=resolution_catalog_version(),
            )
        return ResolutionPlan(
            resolver_id=self.resolver_id,
            tool=intent.tool or "qfk_system",
            canonical_args=args,
            argv_template=args["argv"],
            catalog_version=resolution_catalog_version(),
            issues=[],
        )

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition:
        if plan.status is ResolutionStatus.BLOCKED:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=plan.issues,
            )
        argv = list(plan.argv_template)
        return ResolvedAcquisition(
            resolver_id=self.resolver_id,
            tool=plan.tool,
            status=ResolutionStatus.VERIFIED,
            argv=argv,
            command=" ".join(shlex.quote(item) for item in argv),
            resolution_rule="argv+catalog",
            catalog_version=plan.catalog_version,
            issues=plan.issues,
        )


class DomainResolver(SystemResolver):
    resolver_id = "domain"

    DOMAINS = frozenset({"vm", "network", "storage", "hardware", "platform"})

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        domain = str(intent.args.get("domain") or intent.tool or "").removeprefix("qfk_")
        if domain not in self.DOMAINS:
            return _blocked(intent, [_issue("DOMAIN_UNKNOWN", f"不支持的 aCLI 领域: {domain}", field="domain")])
        args = dict(intent.args)
        raw = str(args.get("command") or "")
        if raw.startswith("acli."):
            dotted_parts = raw.split(".")
            if len(dotted_parts) < 3 or dotted_parts[1] != domain:
                return _blocked(
                    intent,
                    [_issue("DOMAIN_COMMAND_MISMATCH", f"命令路径与 qfk_{domain} 不一致", field="command")],
                )
            raw = " ".join(dotted_parts[2:])
        args["command"] = raw
        args["domain"] = domain
        try:
            command_tokens = shlex.split(raw)
        except ValueError as exc:
            return _blocked(intent, [_issue("DOMAIN_COMMAND_INVALID", str(exc), field="command")])
        required_options = domain_command_requirements(domain, command_tokens)
        supplied_args = list(args.get("command_args") or [])
        formatter = args.get("formatter")
        # 兼容已经发布的历史 KBD：--formatter 是 aCLI 全局参数，必须移到 namespace 前。
        # Resolver 做确定性的结构归一化，绝不能原样拼到子命令末尾。
        while "--formatter" in supplied_args:
            index = supplied_args.index("--formatter")
            if index + 1 >= len(supplied_args):
                return _blocked(intent, [_issue("DOMAIN_FORMATTER_INVALID", "--formatter 缺少格式值", field="command_args")])
            legacy_formatter = supplied_args[index + 1]
            if formatter not in (None, legacy_formatter):
                return _blocked(
                    intent,
                    [_issue("DOMAIN_FORMATTER_CONFLICT", "formatter 与 command_args 中的值冲突", field="formatter")],
                )
            formatter = legacy_formatter
            del supplied_args[index : index + 2]
        if formatter and formatter not in {"xml", "csv", "keyvalue", "json"}:
            return _blocked(
                intent,
                [_issue("DOMAIN_FORMATTER_INVALID", "formatter 必须是 xml/csv/keyvalue/json 之一", field="formatter")],
            )
        args["command_args"] = supplied_args
        if formatter:
            args["formatter"] = formatter
        if required_options and not any(option in [*command_tokens, *supplied_args] for option in required_options):
            return _blocked(
                intent,
                [
                    _issue(
                        "DOMAIN_REQUIRED_ARGUMENT",
                        f"{domain} {' '.join(command_tokens)} 必须提供参数之一: {required_options}",
                        field="command_args",
                    )
                ],
            )
        argv = ["acli"]
        if formatter:
            argv.extend(["--formatter", str(formatter)])
        argv.extend([domain, *command_tokens, *supplied_args])
        if any("\x00" in item or any(char in item for char in "|;&`$<>\n\r") for item in argv):
            return _blocked(intent, [_issue("DOMAIN_COMMAND_INVALID", "命令包含 shell 控制字符")])
        if not command_path_known(["acli", domain, *command_tokens]):
            return _blocked(
                intent,
                [_issue("DOMAIN_COMMAND_UNKNOWN", "命令路径不在当前 Catalog", field="command")],
                catalog_version=resolution_catalog_version(),
            )
        args["argv"] = argv
        return ResolutionPlan(
            resolver_id=self.resolver_id,
            tool=intent.tool or f"qfk_{domain}",
            canonical_args=args,
            argv_template=argv,
            catalog_version=resolution_catalog_version(),
        )


class ServiceResolver:
    resolver_id = "service"

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        args = dict(intent.args)
        container = str(args.get("container") or "asv")
        service = str(args.get("service") or args.get("resource_keyword") or "").strip()
        action = str(args.get("action") or "status").strip().lower()
        issues: list[ResolutionIssue] = []
        if container not in VALID_SERVICE_CONTAINERS:
            issues.append(_issue("SERVICE_CONTAINER_INVALID", f"服务组不在 Catalog: {container}", field="container"))
        if not service:
            issues.append(_issue("SERVICE_REQUIRED", "必须提供服务名", field="service"))
        if action != "status":
            issues.append(_issue("SERVICE_READ_ONLY_REQUIRED", "qfk_service 诊断只允许 action=status", field="action"))
        if issues:
            return _blocked(intent, issues)
        args.update({"container": container, "service": service, "action": action})
        return ResolutionPlan(
            resolver_id=self.resolver_id,
            tool=intent.tool or "qfk_service",
            canonical_args=args,
            argv_template=["acli", "service", container, service, "status"],
            catalog_version="service-catalog",
        )

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition:
        if plan.status is ResolutionStatus.BLOCKED:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=plan.issues,
            )
        argv = list(plan.argv_template)
        return ResolvedAcquisition(
            resolver_id=self.resolver_id,
            tool=plan.tool,
            status=ResolutionStatus.VERIFIED,
            argv=argv,
            command=" ".join(shlex.quote(item) for item in argv),
            resolution_rule="service+readonly",
            catalog_version=plan.catalog_version,
        )


class QkvResolver:
    resolver_id = "qkv"

    QUERIES = frozenset({"alert", "task", "dialog"})

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        args = dict(intent.args)
        query = str(args.get("query") or args.get("type") or "").lower()
        keyword = str(args.get("keyword") or "").strip()
        if query not in self.QUERIES:
            return _blocked(intent, [_issue("QKV_QUERY_INVALID", f"不支持的 QKV 查询: {query}", field="query")])
        if not keyword:
            return _blocked(intent, [_issue("QKV_KEYWORD_REQUIRED", "QKV 查询必须提供 keyword", field="keyword")])
        action = resolve_qkv_action(keyword, query)
        args.update({"query": query, "keyword": keyword, "keyword_normalized": normalize_qkv_keyword(keyword)})
        if action:
            args.update(action)
        else:
            # Arbitrary product/event keywords remain valid exact queries. They do
            # not receive unreviewed fuzzy guesses and are marked as unclassified.
            args.update({"keyword_candidates": [keyword], "keyword_resolution": "unclassified"})
        return ResolutionPlan(
            resolver_id=self.resolver_id,
            tool=intent.tool or f"qkv_{query}",
            canonical_args=args,
            argv_template=["acli", query, "get"],
            catalog_version=resolution_catalog_version(),
        )

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition:
        if plan.status is ResolutionStatus.BLOCKED:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=plan.issues,
            )
        args = plan.canonical_args
        query = args["query"]
        keyword = str(args["keyword"])
        candidates = [str(item) for item in args.get("keyword_candidates", [keyword])]
        first = candidates[0] if candidates else keyword
        limit = str(max(1, min(int(args.get("limit", 100)), 200)))
        if query == "dialog":
            path = str(args.get("path") or (args.get("paths") or ["/sf/log/today"])[0])
            context_lines = str(max(0, min(int(args.get("context_lines", 2)), 10)))
            argv = ["acli", "log", "get", "-k", first, "-p", path, "-c", context_lines]
        else:
            argv = ["acli", "--formatter", "json", query, "get", "-k", first]
            if query == "task" and args.get("is_failed"):
                argv.extend(["-s", "failed"])
            argv.extend(["-l", limit])
        return ResolvedAcquisition(
            resolver_id=self.resolver_id,
            tool=plan.tool,
            status=ResolutionStatus.VERIFIED,
            argv=argv,
            command=" ".join(shlex.quote(item) for item in argv),
            resolution_rule="qkv-action-catalog+bounded-aliases" if args.get("action_id") else "qkv-exact-keyword",
            catalog_version=plan.catalog_version,
            evidence={
                "keyword_original": keyword,
                "keyword_normalized": args.get("keyword_normalized"),
                "action_id": args.get("action_id"),
                "canonical_keyword": args.get("canonical_keyword", keyword),
                "keyword_candidates": candidates,
                "matched_as": args.get("matched_as", "unclassified"),
                "keyword_resolution": "action_catalog" if args.get("action_id") else "unclassified",
            },
        )


class VariableResolver:
    resolver_id = "variable"
    _placeholder = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        return ResolutionPlan(
            resolver_id=self.resolver_id,
            tool=intent.tool,
            canonical_args=dict(intent.args),
            catalog_version="variable-catalog",
        )

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition:
        values = dict((context or {}).get("variables") or {})
        unresolved: set[str] = set()

        def replace(value: Any) -> Any:
            if isinstance(value, str):

                def repl(match: re.Match[str]) -> str:
                    name = match.group(1)
                    if name not in values or values[name] in (None, ""):
                        unresolved.add(name)
                        return match.group(0)
                    return str(values[name])

                return self._placeholder.sub(repl, value)
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        resolved = replace(plan.canonical_args)
        issues = [_issue("VARIABLE_UNRESOLVED", f"变量未解析: {name}", field=name) for name in sorted(unresolved)]
        status = ResolutionStatus.NEEDS_PROBE if unresolved else ResolutionStatus.VERIFIED
        return ResolvedAcquisition(
            resolver_id=self.resolver_id,
            tool=plan.tool,
            status=status,
            variables_used=values,
            issues=issues,
            evidence={"resolved_args": resolved},
        )
