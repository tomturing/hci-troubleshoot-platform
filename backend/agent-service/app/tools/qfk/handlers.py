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
- container    → qfk_system 的 aCLI ``--container`` 全局参数（不是 terminal_bridge 容器）
- cluster/timeout/formatter → qfk_system 的 aCLI 全局参数
注：host 不参与命令行构建--目标主机路由在传输层（engine.py 经 node_ip/case_id 由 terminal_bridge 选择 SSH 会话），BackendSignal.host 仅作运行时记录。
"""

import re
import shlex

from shared.resolution.log_selector import build_log_selector
from shared.schemas.acquirer_args import SAFE_LOG_FILE_PATTERN, VALID_SERVICE_CONTAINERS
from shared.schemas.log_source_catalog import (
    LOG_MATCHER_TYPES,
    LOG_ROOT,
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
    """检测是否含命令注入类非法字符，保留已验证的 ``{{VAR}}`` 占位符。"""
    if not value:
        return False
    without_placeholders = re.sub(r"\{\{[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\}\}", "VALUE", value)
    return any(c in _ILLEGAL_CHARS for c in without_placeholders)


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
        try:
            return build_log_selector(
                matcher=signal.matcher,
                keywords=signal.keyword,
                filter_keywords=signal.filter_keywords,
                resource_keyword=signal.resource_keyword,
                request_id=signal.request_id,
            )
        except ValueError as exc:
            raise CommandBuildError(str(exc)) from exc

    @staticmethod
    def _inferred_whitebox_path(source: dict[str, object], absolute_time: str | None) -> str:
        """按白盒日志布局定位目录；无法从 END 得到日号时回退日志根。

        HCI whitebox 实机目录使用月内日号（``/sf/log/4``），不是 ISO 日期或
        ``YYYYMMDD``。``absolute_time`` 在正常执行前已由变量池将 ``{{END}}``
        替换为实际绝对时间；若仍为占位符，不能猜测日期或继续使用 ``today``。
        """
        if not absolute_time or absolute_time.startswith("{{"):
            return LOG_ROOT

        # time_window 已由 normalize_absolute_log_time 校验为 YYYY-MM-DD[ ...]。
        day_token = str(int(absolute_time[8:10]))
        date_subpath = str(source.get("date_subpath") or "").strip("/")
        return f"{LOG_ROOT}/{day_token}/{date_subpath}" if date_subpath else f"{LOG_ROOT}/{day_token}"

    @staticmethod
    def _is_legacy_whitebox_today_path(path: str | None, source: dict[str, object]) -> bool:
        """识别旧 KBD 写入的 today 路径，以免它覆盖 END 的精确目录定位。"""
        if path == f"{LOG_ROOT}/today":
            return True
        date_subpath = str(source.get("date_subpath") or "").strip("/")
        return bool(date_subpath and path == f"{LOG_ROOT}/today/{date_subpath}")

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

            # 白盒日志目录按 END 的月内日号组织。默认和旧 KBD 中的 today 路径都不能
            # 覆盖这个定位，否则历史故障会被错误限制在今天。END 未解析时回退 /sf/log。
            # blackbox 不走 whitebox 的日号目录规则，仍按固定 YYYYMMDD 目录确定性改写。
            legacy_whitebox_today_path = self._is_legacy_whitebox_today_path(path, source)
            if source["family"] == "whitebox" and (signal.path_inferred or legacy_whitebox_today_path):
                path = self._inferred_whitebox_path(source, absolute_time)
            elif absolute_time and signal.path_inferred:
                date_token = absolute_time[:10].replace("-", "")
                if source["family"] == "blackbox":
                    path = f"/sf/log/blackbox/{date_token}"
                elif source["family"] == "vn_blackbox":
                    path = f"/sf/log/vn-blackbox/{date_token}"

        selector, use_extended, matcher_type = self._matcher_selector(signal)
        if matcher_type in LOG_MATCHER_TYPES and matcher_type not in source["predicates"]:
            # 对齐 2026-08-07 QFK 取值执行契约：数值 Matcher（threshold/delta/trend）在配置了
            # ai_extract.instruction 时，允许走 "AI 类型化取值 → 确定性判断" 通道，日志源
            # catalog 无需直接支持该 predicate；未配置 AI 提取时仍按 catalog 单一事实源 fail closed。
            numeric_matcher = matcher_type in {"threshold", "delta", "trend"}
            extract = (signal.matcher or {}).get("extract") if isinstance(signal.matcher, dict) else None
            ai_instruction = ""
            if isinstance(extract, dict):
                ai_extract = extract.get("ai_extract")
                if isinstance(ai_extract, dict):
                    ai_instruction = str(ai_extract.get("instruction") or "")
            if not (numeric_matcher and ai_instruction):
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
        if action.lower() != "status":
            raise CommandBuildError("qfk_service 在 KBD 自动诊断中只允许只读 action=status")
        return [f"acli service {container} {shlex.quote(service)} status"]


class SystemHandler(BackendSignalHandler):
    def build_commands(self, signal: BackendSignal) -> list[str]:
        command = (signal.command or "").strip()
        if not command:
            raise CommandBuildError("qfk_system 必须在 command 中提供执行命令")
        if _has_illegal_chars(command):
            raise CommandBuildError(f"系统命令包含非法字符: {command}")
        parts = ["acli"]
        if signal.is_cluster_mode():
            parts.append("--cluster")
        # aCLI 的 timeout 是内层命令超时；Bridge 的外层 timeout 由 engine/executor
        # 另行控制，避免 SSH 转发在 aCLI 尚未返回前提前中断。
        parts.extend(["--timeout", str(signal.timeout)])
        if signal.formatter:
            parts.extend(["--formatter", shlex.quote(signal.formatter)])
        if signal.container:
            parts.extend(["--container", shlex.quote(signal.container)])
        parts.extend(["system", command])
        for arg in signal.command_args:
            if not isinstance(arg, str) or _has_illegal_chars(arg):
                raise CommandBuildError("系统命令参数包含非法字符")
            parts.append(shlex.quote(arg))
        return [" ".join(parts)]


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
        # KBD/LLM 常将 aCLI 路径写成 acli.vm.config.get。领域 QFK 只接受自己的
        # namespace；在这里结构化成 argv，避免点号字符串被原样交给 Shell。
        if command.startswith("acli.") and " " not in command:
            tokens = command.split(".")
            if len(tokens) < 3 or tokens[1] != namespace:
                raise CommandBuildError(f"{namespace} 命令路径与 qfk_{namespace} 不一致: {command}")
            command_parts = tokens[2:]
        else:
            try:
                command_parts = shlex.split(command)
            except ValueError as exc:
                raise CommandBuildError(f"{namespace} command 无法安全分词: {exc}") from exc
        if not command_parts or any(_has_illegal_chars(item) for item in command_parts):
            raise CommandBuildError(f"{namespace} command 包含非法 token")
        extra_args = signal.command_args or []
        if any(not isinstance(item, str) or not item or _has_illegal_chars(item) for item in extra_args):
            raise CommandBuildError(f"{namespace} command_args 包含非法参数")
        parts = [
            "acli",
            namespace,
            *[shlex.quote(item) for item in command_parts],
            *[shlex.quote(item) for item in extra_args],
        ]
        return [" ".join(parts)]


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
