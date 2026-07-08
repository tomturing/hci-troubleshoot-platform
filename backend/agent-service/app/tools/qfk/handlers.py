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
from app.tools.qfk.signal import BackendSignal, BackendSignalType


class CommandBuildError(ValueError):
    """QFK 命令构建异常"""


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

    def evaluate(self, results: list[ExecResult], keywords: list[str], match_mode: str) -> tuple[bool, str]:
        """
        根据执行结果（一个或多个结果）及关键字列表，进行布尔判定并提供证据

        Returns:
            (matched, evidence_text)
        """
        if not results:
            return False, "无执行结果"

        # 合并所有命令的 stdout 和 stderr 作为匹配池
        combined_outputs = []
        evidence_parts = []
        for r in results:
            text = f"{r.stdout}\n{r.stderr}"
            combined_outputs.append(text)
            # 记录执行过的实际命令和简短返回
            preview = r.stdout[:300].strip() if r.stdout.strip() else r.stderr[:300].strip()
            evidence_parts.append(f"命令: {r.command}\n退出码: {r.exit_code}\n输出片段: {preview}")

        combined_text = "\n".join(combined_outputs)
        text_lower = combined_text.lower()

        # 计算匹配的关键字
        matched_kws = [kw for kw in keywords if kw.lower() in text_lower]

        matched = len(matched_kws) == len(keywords) if match_mode.lower() == "all" else len(matched_kws) > 0

        # 构建匹配结果描述
        mode_str = "AND" if match_mode.lower() == "all" else "OR"
        evidence_prefix = f"【关键字对比评估 ({mode_str})】\n目标关键字: {keywords}\n命中的关键字: {matched_kws}\n命中判定: {matched}\n\n【执行证据链】\n"
        evidence = evidence_prefix + "\n\n".join(evidence_parts)

        return matched, evidence


# ─────────────────────────────────────────────────────────────────────────────
# 具体处理器实现
# ─────────────────────────────────────────────────────────────────────────────


class LogKeywordHandler(FunctionHandler):
    """
    处理 log_keyword 和 dialog_keyword
    使用 acli log get 搜索关键字
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        # 日志检索必须拥有 keywords 至少一个来作为 acli log get 的检索入口参数
        if not signal.keywords:
            raise CommandBuildError("log/dialog 信号类型必须提供关键字作为 acli log get -k 的检索词")

        parts = ["acli log get"]

        # 默认取第一个 keyword 作为 acli 的命令端过滤参数，减少数据中转开销
        first_kw = signal.keywords[0]
        parts.extend(["-k", shlex.quote(first_kw)])

        # 校验并提取文件和路径参数
        target = signal.target
        if target:
            if target.resource:
                if "/" in target.resource or "\\" in target.resource:
                    raise CommandBuildError(f"日志文件名称（target.resource）不能包含路径: {target.resource}")
                parts.extend(["-f", shlex.quote(target.resource)])

            if target.path:
                allowed_prefixes = ("/sf/log", "/sf/data")
                if not any(target.path.startswith(p) for p in allowed_prefixes):
                    raise CommandBuildError(f"日志检索路径只允许以 {allowed_prefixes} 开头，实际: {target.path}")
                parts.extend(["-p", shlex.quote(target.path)])

            if target.time_window:
                # 若时间窗口包含类似于可识别的时间，用 -t 限制以提高效率
                parts.extend(["-t", shlex.quote(target.time_window)])

        return [" ".join(parts)]




class ServiceStatusHandler(FunctionHandler):
    """
    处理 service_status
    使用 acli service <container> <service_name> status 检查状态
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        container = signal.container or "asv"
        valid_containers = {"asv", "anet", "host"}
        if container not in valid_containers:
            raise CommandBuildError(f"非法服务容器类型: {container}，允许值: {valid_containers}")

        service_name = None
        if signal.target and signal.target.resource:
            service_name = signal.target.resource
        elif signal.target and signal.target.scope:
            service_name = signal.target.scope

        if not service_name:
            raise CommandBuildError("service_status 必须通过 target.resource 或 target.scope 指定服务名称")

        # 防止服务名命令注入
        if not re.match(r"^[a-zA-Z0-9_\-]+$", service_name):
            raise CommandBuildError(f"非法服务名称: {service_name}")

        return [f"acli service {container} {service_name} status"]


class GenericSubCommandHandler(FunctionHandler):
    """
    通用子命名空间命令构建器（vm_state/network_check/storage_state/hardware_state/platform_state/system_metric）
    命令格式: acli <Q_namespace> <sub_command>
    """

    # 后端信号类型与 acli 子命名空间的映射
    NAMESPACE_MAP: ClassVar[dict[BackendSignalType, str]] = {
        BackendSignalType.VM_STATE: "vm",
        BackendSignalType.NETWORK_CHECK: "network",
        BackendSignalType.STORAGE_STATE: "storage",
        BackendSignalType.HARDWARE_STATE: "hardware",
        BackendSignalType.PLATFORM_STATE: "platform",
        BackendSignalType.SYSTEM_METRIC: "system",
    }

    def build_commands(self, signal: BackendSignal) -> list[str]:
        namespace = self.NAMESPACE_MAP.get(signal.signal_type)
        if not namespace:
            raise CommandBuildError(f"不支持的 Generic 子命令信号类型: {signal.signal_type}")

        sub_cmd = signal.sub_command
        if not sub_cmd:
            raise CommandBuildError(f"{signal.signal_type} 信号必须在 sub_command 属性中提供具体的子命令，例如: 'list' 或 'asan disk list'")

        # 简单防注入校验（过滤 shell 元字符）
        forbidden_chars = re.compile(r"[|;&$`\\()\[\]{}<>!]")
        if forbidden_chars.search(sub_cmd):
            raise CommandBuildError(f"sub_command 中包含非法字符: {sub_cmd!r}")

        return [f"acli {namespace} {sub_cmd.strip()}"]


# ─────────────────────────────────────────────────────────────────────────────
# 处理器注册表
# ─────────────────────────────────────────────────────────────────────────────


class HandlerRegistry:
    """
    QFK 后端信号 Handler 注册表
    """

    _registry: ClassVar[dict[BackendSignalType, FunctionHandler]] = {
        BackendSignalType.LOG_KEYWORD: LogKeywordHandler(),
        BackendSignalType.SERVICE_STATUS: ServiceStatusHandler(),
        BackendSignalType.VM_STATE: GenericSubCommandHandler(),
        BackendSignalType.NETWORK_CHECK: GenericSubCommandHandler(),
        BackendSignalType.STORAGE_STATE: GenericSubCommandHandler(),
        BackendSignalType.HARDWARE_STATE: GenericSubCommandHandler(),
        BackendSignalType.PLATFORM_STATE: GenericSubCommandHandler(),
        BackendSignalType.SYSTEM_METRIC: GenericSubCommandHandler(),
    }

    @classmethod
    def get(cls, signal_type: BackendSignalType) -> FunctionHandler:
        """获取指定后端信号类型的处理器"""
        handler = cls._registry.get(signal_type)
        if not handler:
            raise ValueError(f"未找到后端信号类型 {signal_type} 对应的 Handler 注册")
        return handler
