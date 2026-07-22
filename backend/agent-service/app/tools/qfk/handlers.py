"""
QFK 后端信号处理器（Handlers）
将结构化信号转换成 actual acli 命令执行，并对其输出结果做关键字匹配判断

字段语义（向后兼容）：
- 顶层共有字段：keyword, timeout, expected, match_mode
- target 子结构：scope（主机）/ resource（资源名）/ path（路径）/ time_window（结束时间）
- 特有字段：
  - log：使用 target.{resource, path, time_window}
  - service：使用 target.resource（服务名）、container（asv/vn/...）、action
  - system/vm/network/storage/hardware/platform：sub_command（子命令）
"""

from __future__ import annotations

import re
import shlex
from abc import ABC, abstractmethod
from typing import ClassVar

from app.tools.acli.executor import ExecResult
from app.tools.qfk.matcher import evaluate_matcher
from app.tools.qfk.signal import (
    VALID_CONTAINERS,
    VALID_SERVICE_CONTAINERS,
    BackendSignal,
    BackendSignalTarget,
)


class CommandBuildError(ValueError):
    """QFK 命令构建异常"""
    pass


class FunctionHandler(ABC):
    """
    QFK 关键信号执行策略基类
    """

    @abstractmethod
    def build_commands(self, signal: BackendSignal) -> list[str]:
        """
        根据结构化后端信号构建 1 个或多个 acli 执行命令
        """
        pass

    def evaluate(
        self,
        results: list[ExecResult],
        keywords: list[str],
        match_mode: str,
    ) -> tuple[bool, list[str], str]:
        """
        根据执行结果（一个或多个结果）及关键字列表，进行布尔判定并提供证据。
        """
        if not results:
            return False, [], "无执行结果"

        # 合并所有命令的 stdout 和 stderr 作为匹配池
        combined_outputs = []
        evidence_parts = []
        for r in results:
            text = f"{r.stdout}\n{r.stderr}"
            combined_outputs.append(text)
            preview = r.stdout[:300].strip() if r.stdout.strip() else r.stderr[:300].strip()
            evidence_parts.append(f"命令: {r.command}\n退出码: {r.exit_code}\n输出片段: {preview}")

        combined_text = "\n".join(combined_outputs)
        mode = (match_mode or "or").lower()
        mode = {"any": "or", "all": "and"}.get(mode, mode)
        mode_str = mode.upper()

        matcher_dict = {
            "type": "keyword",
            "pattern": keywords,
            "mode": mode,
            "expected": True,
        }
        res = evaluate_matcher(matcher_dict, combined_text, server_pre_filtered=(mode == "or"))
        matched = bool(res.matched)
        matched_kws = res.detail.get("matched_keywords", [])

        evidence_prefix = (
            f"【关键字对比评估 ({mode_str})】\n"
            f"目标关键字: {keywords}\n"
            f"命中的关键字: {matched_kws}\n"
            f"命中判定: {matched}\n\n"
            f"【执行证据链】\n"
        )
        evidence = evidence_prefix + "\n\n".join(evidence_parts)

        return matched, matched_kws, evidence


# ─────────────────────────────────────────────────────────────────────────────
# 基础命令构建器
# ─────────────────────────────────────────────────────────────────────────────

def _build_base_command(signal: BackendSignal) -> list[str]:
    """构建基础命令参数：acli [--cluster | --host HOST] [--timeout N]"""
    parts = ["acli"]

    # host 参数：优先顶层 host，否则 target.scope，cluster 特殊值
    host_val = signal.host
    if host_val == "cluster" or (signal.target and signal.target.scope == "cluster"):
        parts.append("--cluster")
    elif host_val:
        parts.extend(["--host", shlex.quote(str(host_val))])

    # timeout 参数（仅在显式设置时添加，避免默认污染命令）
    if signal.timeout is not None and signal.timeout > 0:
        parts.extend(["--timeout", str(signal.timeout)])

    return parts


def _get_target(signal: BackendSignal) -> BackendSignalTarget | None:
    """获取 signal.target（兼容 dict/对象输入）。"""
    if signal.target is not None:
        return signal.target
    # 兼容：当 target 没设置时从顶层字段构造
    if any([signal.host, signal.file, signal.end]):
        return BackendSignalTarget(
            scope=signal.host,
            resource=signal.file,
            time_window=signal.end,
        )
    return None


def _resolve_keywords(signal: BackendSignal) -> list[str]:
    """解析关键字列表（兼容 keyword / keywords），返回去重+排序后的非空列表。"""
    raw: list[str] = []
    if signal.keyword:
        raw.extend(signal.keyword)
    if signal.keywords:
        raw.extend(signal.keywords)
    cleaned = [kw for kw in raw if kw]
    if not cleaned:
        raise CommandBuildError("必须提供关键字（keyword 或 keywords 字段，至少需要一个非空关键字）")
    # 去重并按字母序排序，保证 or 模式 pattern 稳定
    return sorted(set(cleaned))


# ─────────────────────────────────────────────────────────────────────────────
# 具体处理器实现
# ─────────────────────────────────────────────────────────────────────────────

class LogHandler(FunctionHandler):
    """
    qfk_log 处理器
    命令格式: acli [--host HOST | --cluster] log get [-E] -k "keyword" -f "file" [-p "path"] [-t "time_window"]
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        keywords = _resolve_keywords(signal)

        # 基础命令
        parts = _build_base_command(signal)
        parts.extend(["log", "get"])

        # 关键字参数
        mode = (signal.match_mode or "or").lower()
        if mode == "or":
            # or 模式：用 grep -E 合并关键字（re.escape 转义）
            pattern = "|".join(re.escape(kw) for kw in keywords)
            parts.extend(["-E", "-k", shlex.quote(pattern)])
        else:
            # and/not 模式：先拉取全量，再客户端过滤
            parts.extend(["-k", shlex.quote("")])

        # target 解析
        target = _get_target(signal)
        if target is None:
            raise CommandBuildError("qfk_log 必须通过 target.resource 提供日志文件名")

        # path 参数（选填）：target.path（先校验前缀，再处理 resource）
        path = target.path
        if path:
            # 路径前缀合法性校验（防止任意目录穿越）
            allowed_prefixes = ("/sf/log/", "/sf/log")
            if not any(path.startswith(p) for p in allowed_prefixes):
                raise CommandBuildError(f"日志路径只允许以 /sf/log/ 开头: {path}")
            parts.extend(["-p", path])

        # file 参数（必填）：target.resource
        resource = target.resource
        if not resource:
            raise CommandBuildError("qfk_log 必须通过 target.resource 提供日志文件名")
        if "/" in resource or "\\" in resource:
            raise CommandBuildError(f"日志文件名不能包含路径: {resource}")
        parts.extend(["-f", resource])

        # end 参数（选填）：target.time_window
        if target.time_window:
            parts.extend(["-t", target.time_window])

        return [" ".join(parts)]


# 旧版别名（向后兼容）
LogKeywordHandler = LogHandler


class SystemHandler(FunctionHandler):
    """
    qfk_system 处理器
    命令格式: acli [--container CONTAINER] [--host HOST | --cluster] system <sub_command>
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        # 基础命令（system 子命令空间）
        parts = ["acli"]

        # container 参数（仅显式设置时添加）
        container = signal.container
        if container and container in VALID_CONTAINERS:
            parts.extend(["--container", container])

        # host/cluster 参数
        host_val = signal.host
        if host_val == "cluster" or (signal.target and signal.target.scope == "cluster"):
            parts.append("--cluster")
        elif host_val:
            parts.extend(["--host", shlex.quote(str(host_val))])

        # timeout 参数（仅显式设置时添加）
        if signal.timeout is not None and signal.timeout > 0:
            parts.extend(["--timeout", str(signal.timeout)])

        # system <sub_command>
        sub_command = signal.sub_command or signal.command
        if not sub_command:
            raise CommandBuildError("qfk_system 必须在 sub_command 属性中提供执行命令")

        # 防注入校验
        forbidden_chars = re.compile(r"[|;&$`\\()\[\]{}<>!\n\r#]")
        if forbidden_chars.search(sub_command):
            raise CommandBuildError(f"sub_command 中包含非法字符: {sub_command!r}")

        parts.extend(["system", sub_command.strip()])
        return [" ".join(parts)]


class ServiceHandler(FunctionHandler):
    """
    qfk_service 处理器
    命令格式: acli service <container> <service> <action>
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        # 基础命令（service 不带 host/cluster 前缀）
        parts = ["acli"]

        # target.resource → service 名（必填）
        target = _get_target(signal)
        if not target or not target.resource:
            raise CommandBuildError("qfk_service 必须通过 target.resource 提供服务名称")
        service_name = target.resource

        # 防注入校验
        if not re.match(r"^[a-zA-Z0-9_\-]+$", service_name):
            raise CommandBuildError(f"非法服务名称: {service_name}")

        # container 校验（旧版：asv/vn/vn-agent/vs）
        container = signal.container or "asv"
        if container not in VALID_SERVICE_CONTAINERS:
            raise CommandBuildError(f"非法服务容器: {container}，允许值: {VALID_SERVICE_CONTAINERS}")

        # action 默认 status
        action = signal.action or "status"

        parts.extend(["service", container, service_name, action])
        return [" ".join(parts)]


class GenericSubCommandHandler(FunctionHandler):
    """
    通用子命名空间命令构建器（vm/network/storage/hardware/platform/system）
    命令格式: acli [--host HOST | --cluster] <namespace> <sub_command>
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        # 基础命令
        parts = _build_base_command(signal)

        # namespace
        namespace = signal.namespace
        if not namespace:
            raise CommandBuildError("缺少 namespace 字段")

        # sub_command 参数（必填）
        sub_command = signal.sub_command or signal.command
        if not sub_command:
            raise CommandBuildError(f"{namespace} 必须在 sub_command 属性中提供子命令")

        # 防注入校验
        forbidden_chars = re.compile(r"[|;&$`\\()\[\]{}<>!\n\r#]")
        if forbidden_chars.search(sub_command):
            raise CommandBuildError(f"sub_command 中包含非法字符: {sub_command!r}")

        # system 走 SystemHandler；其余直接拼 namespace + sub_command
        if namespace == "system":
            parts.extend(["system", sub_command.strip()])
        else:
            parts.extend([namespace, sub_command.strip()])
        return [" ".join(parts)]


# ─────────────────────────────────────────────────────────────────────────────
# 处理器注册表
# ─────────────────────────────────────────────────────────────────────────────

class HandlerRegistry:
    """
    QFK 后端信号 Handler 注册表
    """

    _registry: ClassVar[dict[str, FunctionHandler]] = {}

    @classmethod
    def register(
        cls,
        namespace: str,
        handler: FunctionHandler,
        override: bool = False,
    ) -> None:
        if namespace in cls._registry and not override:
            raise ValueError(
                f"Handler '{namespace}' 已注册，使用 register(..., override=True) 覆盖"
            )
        cls._registry[namespace] = handler

    @classmethod
    def unregister(cls, namespace: str) -> bool:
        if namespace in cls._registry:
            del cls._registry[namespace]
            return True
        return False

    @classmethod
    def get(cls, namespace: str) -> FunctionHandler:
        handler = cls._registry.get(namespace)
        if not handler:
            available = ", ".join(cls.supported_namespaces())
            raise ValueError(
                f"未找到 namespace '{namespace}' 对应的 Handler。"
                f"已注册: [{available}]"
            )
        return handler

    @classmethod
    def supported_namespaces(cls) -> list[str]:
        return list(cls._registry.keys())

    @classmethod
    def reset(cls) -> None:
        cls._registry.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 默认 QFK Handler 启动期动态注册
# ─────────────────────────────────────────────────────────────────────────────

def register_default_qfk_handlers() -> None:
    """启动期动态注册 QFK 默认 namespace Handler"""
    defaults: dict[str, type[FunctionHandler]] = {
        "log": LogHandler,
        "system": SystemHandler,
        "service": ServiceHandler,
        "vm": GenericSubCommandHandler,
        "network": GenericSubCommandHandler,
        "storage": GenericSubCommandHandler,
        "hardware": GenericSubCommandHandler,
        "platform": GenericSubCommandHandler,
    }
    for ns, handler_cls in defaults.items():
        if ns not in HandlerRegistry._registry:
            HandlerRegistry.register(ns, handler_cls())


# 模块导入即完成默认 Handler 注册
register_default_qfk_handlers()
