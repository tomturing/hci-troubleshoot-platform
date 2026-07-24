"""
QFK 信号处理器

将 BackendSignal（v2 扁平运行模型）转换为 acli 命令行，并提供关键字评估（evaluate）。

路由：HandlerRegistry 按 namespace 字符串路由到对应处理器：
- log          → LogKeywordHandler（日志关键字，grep -E 风格）
- service      → ServiceHandler（acli service <container> <name> <action>）
- system       → GenericSubCommandHandler
- vm/network/storage/hardware/platform → GenericSubCommandHandler

字段映射（与 acquirer_args 契约一致）：
- command      → acli <namespace> <command>
- file/path    → qfk_log 的 -f / -p
- time_window  → qfk_log 的 -t
- service/action → qfk_service 的 <container> <name> <action>
- container    → qfk_system 的 --container
注：host 不参与命令行构建--目标主机路由在传输层（engine.py 经 node_ip/case_id 由 terminal_bridge 选择 SSH 会话），BackendSignal.host 仅作运行时记录。
"""

import re
import shlex

from app.tools.qfk.signal import (
    VALID_SERVICE_CONTAINERS,
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
    def build_commands(self, signal: BackendSignal) -> list[str]:
        keywords = signal.keyword or []

        # 去重、跳空、按字母序排序，再 re.escape 为字面量子串
        seen: set[str] = set()
        unique: list[str] = []
        for kw in keywords:
            if not kw or kw in seen:
                continue
            seen.add(kw)
            unique.append(kw)
        if not unique:
            if keywords:
                raise CommandBuildError("关键字全部为空：至少需要一个非空关键字")
            raise CommandBuildError("qfk_log 必须提供关键字（keyword）才能构建检索命令")

        unique.sort()
        pattern = "|".join(re.escape(kw) for kw in unique)

        parts = ["acli", "log", "get", "-E", "-k", shlex.quote(pattern)]

        # 日志路径（仅允许以 /sf/... 前缀开头，先于文件名校验触发以暴露越权）
        if signal.path:
            allowed_prefixes = ("/sf/log/", "/sf/logs/", "/sf/data/", "/sf/datanew/")
            if not signal.path.startswith(allowed_prefixes):
                raise CommandBuildError(
                    f"日志路径只允许以以下前缀开头: {allowed_prefixes}"
                )
            parts.extend(["-p", shlex.quote(signal.path)])

        # 日志文件名（file 字段）
        file = signal.file
        if not file:
            raise CommandBuildError(
                "qfk_log 必须提供日志文件名（通过 file 字段）"
            )
        if "/" in file:
            raise CommandBuildError(f"日志文件名非法：不能包含路径分隔符 (/)：{file}")
        if not file.endswith(".log"):
            raise CommandBuildError(f"日志文件名必须以 .log 结尾：{file}")
        parts.extend(["-f", shlex.quote(file)])

        # 时间窗
        if signal.time_window:
            parts.extend(["-t", shlex.quote(signal.time_window)])

        return [" ".join(parts)]


class ServiceHandler(BackendSignalHandler):
    def build_commands(self, signal: BackendSignal) -> list[str]:
        service = signal.service
        if not service:
            raise CommandBuildError(
                "服务名称必须通过 service 字段提供（qfk_service）"
            )
        if _has_illegal_chars(service):
            raise CommandBuildError(f"非法服务名称（含注入字符）: {service}")

        container = signal.container or "asv"
        if container not in VALID_SERVICE_CONTAINERS:
            raise CommandBuildError(
                f"非法服务容器: {container}（允许: {sorted(VALID_SERVICE_CONTAINERS)}）"
            )

        action = (signal.action or "status").strip()
        return [f"acli service {container} {shlex.quote(service)} {action}"]


class SystemHandler(BackendSignalHandler):
    def build_commands(self, signal: BackendSignal) -> list[str]:
        command = (signal.command or "").strip()
        if not command:
            raise CommandBuildError("qfk_system 必须在 command 中提供执行命令")
        if _has_illegal_chars(command):
            raise CommandBuildError(f"系统命令包含非法字符: {command}")
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
        "system": GenericSubCommandHandler,
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
