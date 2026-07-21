"""
QFK 后端信号处理器（Handlers）
将结构化信号转换成 actual acli 命令执行，并对其输出结果做关键字匹配判断
"""

from __future__ import annotations

import re
import shlex
from abc import ABC, abstractmethod
from typing import ClassVar

from app.tools.acli.executor import ExecResult
from app.tools.qfk.matcher import evaluate_matcher
from app.tools.qfk.signal import VALID_CONTAINERS, BackendSignal


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

        关键字组合模式（match_mode）：
          - or  ：任一关键字命中即判定为真（等价于旧 any）
          - and ：全部关键字命中才判定为真（等价于旧 all）
          - not ：所有关键字均不出现才判定为真（取代旧 expected=False 的取反语义）

        Returns:
            (matched, matched_keywords, evidence_text)
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

    # 处理 host 参数：cluster 或指定主机
    if signal.is_cluster_mode():
        parts.append("--cluster")
    elif signal.host:
        parts.extend(["--host", shlex.quote(signal.host)])

    # 处理 timeout 参数
    if signal.timeout and signal.timeout > 0:
        parts.extend(["--timeout", str(signal.timeout)])

    return parts


def _validate_keyword(signal: BackendSignal) -> list[str]:
    """验证关键字，返回非空关键字列表"""
    keywords = signal.keyword or []
    keywords = [kw for kw in keywords if kw]
    if not keywords:
        raise CommandBuildError("关键字 keyword 不能为空")
    return keywords


# ─────────────────────────────────────────────────────────────────────────────
# 具体处理器实现
# ─────────────────────────────────────────────────────────────────────────────

class LogHandler(FunctionHandler):
    """
    qfk_log 处理器
    命令格式: acli [--host HOST | --cluster] [--timeout N] log get -k "keyword" -f "file" [-t "end"]
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        keywords = _validate_keyword(signal)

        # 基础命令
        parts = _build_base_command(signal)
        parts.extend(["log", "get"])

        # 关键字参数
        mode = (signal.match_mode or "or").lower()
        if mode == "or":
            # or 模式：用 grep -E 合并关键字
            pattern = "|".join(re.escape(kw) for kw in keywords)
            parts.extend(["-E", "-k", shlex.quote(pattern)])
        else:
            # and/not 模式：先拉取全量，再客户端过滤
            parts.extend(["-k", shlex.quote("")])

        # file 参数（必填）
        if not signal.file:
            raise CommandBuildError("qfk_log 必须提供 file 参数")
        if "/" in signal.file or "\\" in signal.file:
            raise CommandBuildError(f"日志文件名不能包含路径: {signal.file}")
        parts.extend(["-f", shlex.quote(signal.file)])

        # end 参数（选填）
        if signal.end:
            parts.extend(["-t", shlex.quote(signal.end)])

        return [" ".join(parts)]


class SystemHandler(FunctionHandler):
    """
    qfk_system 处理器
    命令格式: acli [--container CONTAINER] [--host HOST | --cluster] [--timeout N] system <command>
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        _validate_keyword(signal)

        # 基础命令
        parts = ["acli"]

        # container 参数（仅 system 支持）
        container = signal.container or "asv-con"
        if container not in VALID_CONTAINERS:
            raise CommandBuildError(f"非法容器: {container}，允许值: {VALID_CONTAINERS}")
        parts.extend(["--container", container])

        # host/cluster 参数
        if signal.is_cluster_mode():
            parts.append("--cluster")
        elif signal.host:
            parts.extend(["--host", shlex.quote(signal.host)])

        # timeout 参数
        if signal.timeout and signal.timeout > 0:
            parts.extend(["--timeout", str(signal.timeout)])

        # system <command>
        if not signal.command:
            raise CommandBuildError("qfk_system 必须提供 command 参数")

        # 防注入校验
        forbidden_chars = re.compile(r"[|;&$`\\()\[\]{}<>!\n\r#]")
        if forbidden_chars.search(signal.command):
            raise CommandBuildError(f"command 中包含非法字符: {signal.command!r}")

        parts.extend(["system", signal.command.strip()])

        return [" ".join(parts)]


class ServiceHandler(FunctionHandler):
    """
    qfk_service 处理器
    命令格式: acli [--host HOST | --cluster] [--timeout N] service <container> <service> <action>
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        _validate_keyword(signal)

        # 基础命令
        parts = _build_base_command(signal)

        # service 参数（必填）
        if not signal.service:
            raise CommandBuildError("qfk_service 必须提供 service 参数")

        # 防注入校验
        if not re.match(r"^[a-zA-Z0-9_\-]+$", signal.service):
            raise CommandBuildError(f"非法服务名称: {signal.service}")

        # container 默认 asv-con
        container = signal.container or "asv-con"

        # action 默认 status
        action = signal.action or "status"

        parts.extend(["service", container, signal.service, action])

        return [" ".join(parts)]


class GenericSubCommandHandler(FunctionHandler):
    """
    通用子命名空间命令构建器（vm/network/storage/hardware/platform）
    命令格式: acli [--host HOST | --cluster] [--timeout N] <namespace> <command>
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        _validate_keyword(signal)

        # 基础命令
        parts = _build_base_command(signal)

        # namespace
        namespace = signal.namespace
        if not namespace:
            raise CommandBuildError("缺少 namespace 字段")

        # command 参数（必填）
        if not signal.command:
            raise CommandBuildError(f"{namespace} 必须提供 command 参数")

        # 防注入校验
        forbidden_chars = re.compile(r"[|;&$`\\()\[\]{}<>!\n\r#]")
        if forbidden_chars.search(signal.command):
            raise CommandBuildError(f"command 中包含非法字符: {signal.command!r}")

        parts.extend([namespace, signal.command.strip()])

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
