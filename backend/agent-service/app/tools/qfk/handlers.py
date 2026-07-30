"""
QFK 信号处理器

将 BackendSignal（v2 扁平运行模型）转换为 aCLI 命令行，并提供兼容关键字评估。

路由：HandlerRegistry 按 namespace 字符串路由到对应处理器：
- log          → LogKeywordHandler（统一日志源 Catalog + 结构化 matcher）
- service      → ServiceHandler（acli service <container> <name> <action>）
- system       → GenericSubCommandHandler
- vm/network/storage/hardware/platform → GenericSubCommandHandler

字段映射（与 acquirer_args 契约一致）：
- command      → acli <namespace> <command>
- file/path/source_family/parser → qfk_log 的 Catalog 定位与解析
- time_window/request_id/context_lines/include_archives → qfk_log 的 -t/-i/-c/-g
- service/action → qfk_service 的 <container> <name> <action>
- container    → qfk_system 的 terminal_bridge 执行位置（host 表示宿主机）
注：host 不参与命令行构建--目标主机路由在传输层（engine.py 经 node_ip/case_id 由 terminal_bridge 选择 SSH 会话），BackendSignal.host 仅作运行时记录。
"""

import re
import shlex

from shared.schemas.acquirer_args import SAFE_LOG_FILE_PATTERN, VALID_SERVICE_CONTAINERS
from shared.schemas.log_source_catalog import (
    LOG_MATCHER_TYPES,
    REQUEST_ARTIFACT_ROOT,
    normalize_absolute_log_time,
    normalize_log_path,
    resolve_log_source,
)

from app.tools.qfk.signal import (
    BackendSignal,
)

__all__ = [
    "CommandBuildError",
    "HandlerRegistry",
    "HandlerError",
    "UnsupportedNamespaceError",
    "BackendSignalHandler",
    "LogKeywordHandler",
    "ServiceHandler",
    "SystemHandler",
    "GenericSubCommandHandler",
    "build_acli_command",
]


# ─── 异常 ──────────────────────────────────────────────────────────────────────
class HandlerError(Exception):
    """处理器通用异常"""


class CommandBuildError(HandlerError):
    """命令构建失败（字段缺失 / 非法 / 注入风险）。"""


class UnsupportedNamespaceError(HandlerError):
    """不支持的命名空间"""


# ─── 注入纵深：非法字符集合（# 等 quote-blind 字符由 Handler 入口拦截）──────────
_ILLEGAL_CHARS = set("|#;&`$<>{}\n\r")


def _has_illegal_chars(value: str | None) -> bool:
    """检测是否含命令注入类非法字符。"""
    if not value:
        return False
    return any(c in _ILLEGAL_CHARS for c in value)


def _normalize_mode(mode: str | None) -> str:
    mode = (mode or "or").lower()
    return {"any": "or", "all": "and"}.get(mode, mode)


# ─── 处理器基类 ────────────────────────────────────────────────────────────────
class BackendSignalHandler:
    def build_commands(self, signal: BackendSignal) -> list[str]:
        raise NotImplementedError

    def evaluate(
        self,
        results: list,
        keywords: list[str],
        mode: str = "or",
    ) -> tuple[bool, list[str], str]:
        """对执行结果做关键字布尔评估（与 matcher._eval_keyword 同义）。

        Returns:
            (matched, matched_keywords, evidence)
        """
        texts: list[str] = []
        for r in results or []:
            out = getattr(r, "stdout", "") or ""
            err = getattr(r, "stderr", "") or ""
            texts.append(f"{out}\n{err}")
        combined = "\n".join(texts)
        out_l = combined.lower()

        kws = [k for k in (keywords or []) if k]
        matched_kws = [k for k in kws if k.lower() in out_l]

        mode_norm = _normalize_mode(mode)
        if mode_norm == "and":
            matched = bool(kws) and len(matched_kws) == len(kws)
        elif mode_norm == "not":
            # not 模式本身编码取反语义：均不出现才命中
            matched = len(matched_kws) == 0
        else:  # or（默认）
            matched = bool(matched_kws)

        evidence = self._format_evidence(mode_norm, kws, matched_kws, combined)
        return matched, matched_kws, evidence

    @staticmethod
    def _format_evidence(
        mode_norm: str,
        kws: list[str],
        matched_kws: list[str],
        combined: str,
    ) -> str:
        mode_str = mode_norm.upper()
        lines = [
            f"【关键字对比评估 ({mode_str})】",
            f"目标关键字: {kws}",
            f"命中的关键字: {matched_kws}",
        ]
        snippet = combined.strip()
        if snippet:
            lines.append("输出摘要:")
            lines.append(snippet[:800])
        return "\n".join(lines)


class LogKeywordHandler(BackendSignalHandler):
    """统一 qfk_log 命令构建器。

    类名为兼容既有导入保留；实现已不限于 keyword。blackbox/whitebox 的差异只体现在
    Catalog 的默认目录、parser 和允许 predicate，不再拆成独立工具。
    """

    @staticmethod
    def _matcher_selector(signal: BackendSignal) -> tuple[str | None, bool, str]:
        """返回 ``(selector, extended_regex, matcher_type)``。"""

        matcher = signal.matcher or {}
        matcher_type = str(matcher.get("type") or ("keyword" if signal.keyword else ""))
        if matcher_type and matcher_type not in LOG_MATCHER_TYPES:
            raise CommandBuildError(f"qfk_log 不支持 matcher.type={matcher_type}")

        pattern = matcher.get("pattern")
        if matcher_type == "keyword":
            raw_items = pattern if isinstance(pattern, list) else [pattern] if pattern else signal.keyword
            unique = sorted({str(item) for item in raw_items if str(item)})
            if unique:
                return "|".join(re.escape(item) for item in unique), True, matcher_type
        elif matcher_type == "regex":
            if not isinstance(pattern, str) or not pattern:
                raise CommandBuildError("qfk_log regex matcher 必须提供非空 pattern")
            if len(pattern) > 2048 or "\n" in pattern or "\r" in pattern:
                raise CommandBuildError("qfk_log regex pattern 过长或包含换行")
            return pattern, True, matcher_type
        elif matcher_type == "state":
            if not isinstance(pattern, str) or not pattern:
                raise CommandBuildError("qfk_log state matcher 必须提供非空 pattern")
            return re.escape(pattern), True, matcher_type
        elif matcher_type in {"threshold", "delta", "trend"}:
            metric = matcher.get("metric") or signal.resource_keyword
            if not isinstance(metric, str) or not metric:
                raise CommandBuildError(f"qfk_log {matcher_type} matcher 必须提供 metric")
            return re.escape(metric), True, matcher_type
        elif matcher_type == "exists":
            return ".", True, matcher_type

        # 产出变量信号没有 matcher 时，必须有 request_id 或受控行选择器，禁止整文件回传。
        if signal.resource_keyword:
            return re.escape(signal.resource_keyword), True, matcher_type or "producer"
        if signal.request_id:
            return None, False, matcher_type or "producer"
        if signal.keyword:
            raise CommandBuildError("关键字全部为空：至少需要一个非空关键字")
        raise CommandBuildError("qfk_log 必须提供关键字 matcher、resource_keyword 或 request_id 以限制日志输出")

    def build_commands(self, signal: BackendSignal) -> list[str]:
        file = signal.file
        try:
            path = normalize_log_path(signal.path)
            absolute_time = normalize_absolute_log_time(signal.time_window)
        except ValueError as exc:
            raise CommandBuildError(str(exc)) from exc

        is_request_artifact = bool(
            path and (path == REQUEST_ARTIFACT_ROOT or path.startswith(f"{REQUEST_ARTIFACT_ROOT}/"))
        )
        if is_request_artifact:
            if not signal.request_id:
                raise CommandBuildError("/sf/data/local 不是日志目录；仅允许携带 request_id 的辅助关联搜索")
            if signal.source_family != "auto":
                raise CommandBuildError("/sf/data/local 辅助搜索不得声明日志 source_family")
            source = {
                "source_id": "request_artifact_scope",
                "parser": "plain_text",
                "predicates": ("keyword", "regex", "state", "exists"),
            }
        else:
            if not file:
                raise CommandBuildError("常规 qfk_log 必须提供 /sf/log 下的日志文件名（file basename）")
            if "/" in file or "\\" in file:
                raise CommandBuildError(f"日志文件名非法：不能包含路径分隔符：{file}")
            if file in {".", ".."} or not re.fullmatch(SAFE_LOG_FILE_PATTERN, file):
                raise CommandBuildError(f"日志文件名必须是无目录、无控制字符的安全 basename：{file}")
            try:
                source = resolve_log_source(
                    file,
                    source_family=signal.source_family,
                    path=path,
                    parser=signal.parser,
                )
                path = normalize_log_path(source.get("path"))
            except ValueError as exc:
                raise CommandBuildError(str(exc)) from exc

            # aCLI -t 自己负责 whitebox 日期目录定位与历史日志解压。若 path 是 Catalog
            # 的 today 默认值，继续传 -p today 会把历史 END 错锁到今天，因此省略 -p；
            # blackbox 不走 whitebox 解压逻辑，按固定 YYYYMMDD 目录确定性改写。
            if absolute_time and signal.path_inferred:
                date_token = absolute_time[:10].replace("-", "")
                if source["family"] == "whitebox":
                    path = None
                elif source["family"] == "blackbox":
                    path = f"/sf/log/blackbox/{date_token}"
                elif source["family"] == "vn_blackbox":
                    path = f"/sf/log/vn-blackbox/{date_token}"

        selector, use_extended, matcher_type = self._matcher_selector(signal)
        if matcher_type in LOG_MATCHER_TYPES and matcher_type not in source["predicates"]:
            raise CommandBuildError(
                f"日志源 {source['source_id']} 的 parser={source['parser']} 不支持 {matcher_type} predicate"
            )

        parts = ["acli", "log", "get"]
        if selector is not None:
            if use_extended:
                parts.append("-E")
            parts.extend(["-k", shlex.quote(selector)])
        if signal.request_id:
            parts.extend(["-i", shlex.quote(signal.request_id)])
        if file:
            parts.extend(["-f", shlex.quote(file)])
        if path:
            parts.extend(["-p", shlex.quote(path)])
        if absolute_time:
            parts.extend(["-t", shlex.quote(absolute_time)])
        if signal.context_lines:
            parts.extend(["-c", str(signal.context_lines)])
        if signal.include_archives:
            if signal.archive_precheck != "verified":
                raise CommandBuildError("搜索 .gz 归档前必须完成磁盘/日期/路径前置检查")
            parts.append("-g")

        return [" ".join(parts)]


class ServiceHandler(BackendSignalHandler):
    def build_commands(self, signal: BackendSignal) -> list[str]:
        service = signal.service
        if not service:
            raise CommandBuildError("服务名称必须通过 service 字段提供（qfk_service）")
        if _has_illegal_chars(service):
            raise CommandBuildError(f"非法服务名称（含注入字符）: {service}")

        container = signal.container or "asv"
        if container not in VALID_SERVICE_CONTAINERS:
            raise CommandBuildError(f"非法服务容器: {container}（允许: {sorted(VALID_SERVICE_CONTAINERS)}）")

        action = (signal.action or "status").strip()
        return [f"acli service {container} {shlex.quote(service)} {action}"]


class SystemHandler(BackendSignalHandler):
    def build_commands(self, signal: BackendSignal) -> list[str]:
        command = (signal.command or "").strip()
        if not command:
            raise CommandBuildError("qfk_system 必须在 command 中提供执行命令")
        if _has_illegal_chars(command):
            raise CommandBuildError(f"系统命令包含非法字符: {command}")
        resource = (signal.resource_keyword or "").strip()
        if resource:
            if _has_illegal_chars(resource):
                raise CommandBuildError(f"系统命令资源参数包含非法字符: {resource}")
            command = f"{command} {shlex.quote(resource)}"
        return [f"acli system {command}"]


class GenericSubCommandHandler(BackendSignalHandler):
    def build_commands(self, signal: BackendSignal) -> list[str]:
        namespace = signal.namespace
        if not namespace:
            raise UnsupportedNamespaceError("signal.namespace 不能为空（vm/network/...）")

        command = (signal.command or "").strip()
        if not command:
            raise CommandBuildError(f"{namespace} 必须在 command 中提供子命令")
        if _has_illegal_chars(command):
            raise CommandBuildError(f"命令包含非法字符（{namespace}）: {command}")

        return [f"acli {namespace} {command}"]


# ─── 命名空间路由 ──────────────────────────────────────────────────────────────
class HandlerRegistry:
    """按 namespace 字符串路由到对应处理器。"""

    _REGISTRY: dict[str, type[BackendSignalHandler]] = {
        "log": LogKeywordHandler,
        "service": ServiceHandler,
        "system": SystemHandler,
        "vm": GenericSubCommandHandler,
        "network": GenericSubCommandHandler,
        "storage": GenericSubCommandHandler,
        "hardware": GenericSubCommandHandler,
        "platform": GenericSubCommandHandler,
    }

    @classmethod
    def get(cls, namespace: str) -> BackendSignalHandler:
        handler_cls = cls._REGISTRY.get(namespace)
        if handler_cls is None:
            raise ValueError(f"未找到 namespace 对应的处理器: {namespace}")
        return handler_cls()

    @classmethod
    def supported_namespaces(cls) -> list[str]:
        return list(cls._REGISTRY.keys())


def build_acli_command(signal: BackendSignal) -> str:
    """根据 BackendSignal 构建 acli 命令字符串（便捷封装）。"""
    handler = HandlerRegistry.get(signal.namespace)
    commands = handler.build_commands(signal)
    return commands[0] if commands else ""
