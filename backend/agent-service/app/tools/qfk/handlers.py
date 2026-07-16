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
from app.tools.qfk.signal import BackendSignal


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

        本方法为薄封装：拼装执行文本后委托 matcher.evaluate_matcher（单一真相源）
        完成关键字求值，再叠加命令执行证据链。最终 expected 翻转由引擎（engine.py）
        依据 signal.expected 完成，此处仅返回「原始 hit」（与历史行为一致）。

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
            # 记录执行过的实际命令和简短返回
            preview = r.stdout[:300].strip() if r.stdout.strip() else r.stderr[:300].strip()
            evidence_parts.append(f"命令: {r.command}\n退出码: {r.exit_code}\n输出片段: {preview}")

        combined_text = "\n".join(combined_outputs)
        mode = (match_mode or "or").lower()
        mode = {"any": "or", "all": "and"}.get(mode, mode)
        mode_str = mode.upper()

        # 委托单一真相源求值；or 模式下服务端已用 grep -E 过滤（-E -k "kw1|kw2"），
        # 故 server_pre_filtered=True，输出非空即代表命中。
        matcher_dict = {
            "type": "keyword",
            "pattern": keywords,
            "mode": mode,
            "expected": True,  # 原始 hit；expected 翻转交给 engine.py
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
        mode = (signal.match_mode or "or").lower()

        if mode == "or":
            # grep -E 等价：所有关键字以 | 连接为单一扩展正则，交由服务端过滤。
            # 彻底解决多关键字场景下"只透传首个关键字"导致的假阴性问题。
            # 关键字是「子串」语义（matcher 类型为 keyword，而非正则）：
            #   - 先判空 + 去重，避免拼接出 `-E -k ""` 或重复模式；
            #   - 再用 re.escape 逐字转义后连接，防止关键字含正则特殊字符
            #     （如 "vm.100"）被当成正则误匹配 "vmx100"（与命令注入同源：
            #      不可信数据进入正则解释器而未转义）。
            kw_set = sorted({kw for kw in (signal.keywords or []) if kw})
            if not kw_set:
                raise CommandBuildError(
                    "log/dialog 信号 or 模式至少需要一个非空关键字作为 acli log get -k 的检索词"
                )
            pattern = "|".join(re.escape(kw) for kw in kw_set)
            parts.extend(["-E", "-k", shlex.quote(pattern)])
        else:
            # and / not：服务端无法表达"全部/取反"语义，先拉取全量日志（-k ""），
            # 再交由 FunctionHandler.evaluate 在客户端按对应语义过滤（子串字面量，
            # 无正则风险，故此处无需 re.escape）。
            parts.extend(["-k", shlex.quote("")])

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
    通用子命名空间命令构建器（vm/network/storage/hardware/platform/system）
    命令格式: acli <namespace> <sub_command>
    """

    def build_commands(self, signal: BackendSignal) -> list[str]:
        namespace = signal.namespace
        if not namespace:
            raise CommandBuildError("BackendSignal 缺少 namespace 字段")

        sub_cmd = signal.sub_command
        if not sub_cmd:
            raise CommandBuildError(f"{namespace} 信号必须在 sub_command 属性中提供具体的子命令，例如: 'list' 或 'asan disk list'")

        # 简单防注入校验（过滤 shell 元字符 + 换行/注释符，纵深防御）
        # 换行符 \n\r 可绕过单条命令限制拼出第二条命令；# 在 shell 中开启注释，
        # 二者均被 CommandSanitizer 二次拦截，此处作为第一道防线提前拒绝。
        forbidden_chars = re.compile(r"[|;&$`\\()\[\]{}<>!\n\r#]")
        if forbidden_chars.search(sub_cmd):
            raise CommandBuildError(f"sub_command 中包含非法字符: {sub_cmd!r}")

        return [f"acli {namespace} {sub_cmd.strip()}"]


# ─────────────────────────────────────────────────────────────────────────────
# 处理器注册表
# ─────────────────────────────────────────────────────────────────────────────


class HandlerRegistry:
    """
    QFK 后端信号 Handler 注册表。

    设计约束（彻底动态注册）：
      - 类体内禁止硬编码任何 namespace → Handler 映射（旧版 _defaults 已彻底移除）。
      - 默认 Handler 必须经由 register() 在启动期显式注册，
        见模块底部的 register_default_qfk_handlers()。
      - 运行时新增 namespace 同样通过 register('custom_ns', CustomHandler()) 完成，
        不存在其它任何隐式/懒加载的注册路径。
    """

    _registry: ClassVar[dict[str, FunctionHandler]] = {}

    @classmethod
    def register(
        cls,
        namespace: str,
        handler: FunctionHandler,
        override: bool = False,
    ) -> None:
        """
        动态注册 handler

        Args:
            namespace: 命名空间标识（如 "log", "vm", "custom_ns"）
            handler: Handler 实例
            override: 是否覆盖已存在的 handler（默认 False，防止误覆盖）

        Raises:
            ValueError: 如果 namespace 已注册且 override=False
        """
        if namespace in cls._registry and not override:
            raise ValueError(
                f"Handler '{namespace}' 已注册，使用 register(..., override=True) 覆盖"
            )
        cls._registry[namespace] = handler

    @classmethod
    def unregister(cls, namespace: str) -> bool:
        """
        注销 handler

        Args:
            namespace: 要注销的命名空间

        Returns:
            bool: True 表示成功注销，False 表示不存在
        """
        if namespace in cls._registry:
            del cls._registry[namespace]
            return True
        return False

    @classmethod
    def get(cls, namespace: str) -> FunctionHandler:
        """
        获取指定 namespace 的处理器

        Args:
            namespace: 命名空间标识

        Returns:
            FunctionHandler: 对应的 Handler 实例

        Raises:
            ValueError: 未找到对应 handler（说明未通过 register() 注册）
        """
        handler = cls._registry.get(namespace)
        if not handler:
            available = ", ".join(cls.supported_namespaces())
            raise ValueError(
                f"未找到 namespace '{namespace}' 对应的 Handler。"
                f"已注册: [{available}]。"
                f"如需新增，请使用 HandlerRegistry.register('{namespace}', YourHandler())"
            )
        return handler

    @classmethod
    def supported_namespaces(cls) -> list[str]:
        """返回所有已注册的 namespace 列表"""
        return list(cls._registry.keys())

    @classmethod
    def reset(cls) -> None:
        """
        重置注册表（仅用于测试）。

        警告：生产环境禁止调用，会清除所有已注册的 handler。
        启动期注册的默认 Handler 不会自动恢复，测试结束后需重新调用
        register_default_qfk_handlers()。
        """
        cls._registry.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 默认 QFK Handler 启动期动态注册
# ─────────────────────────────────────────────────────────────────────────────


def register_default_qfk_handlers() -> None:
    """
    启动期动态注册 8 个 QFK 默认 namespace Handler。

    设计要点：
      - 此函数是「唯一」声明默认 Handler 的地方，旧版的 _defaults 类变量已删除。
      - 全部通过 register() 完成注册，运行时新增 namespace 也走同一条路径，
        杜绝在类体内硬编码 namespace。
      - 幂等：已注册的 namespace 不会重复注册（兼容测试 reset 后重跑）。
    """
    defaults: dict[str, type[FunctionHandler]] = {
        "log": LogKeywordHandler,
        "service": ServiceStatusHandler,
        "vm": GenericSubCommandHandler,
        "network": GenericSubCommandHandler,
        "storage": GenericSubCommandHandler,
        "hardware": GenericSubCommandHandler,
        "platform": GenericSubCommandHandler,
        "system": GenericSubCommandHandler,
    }
    for ns, handler_cls in defaults.items():
        if ns not in HandlerRegistry._registry:
            HandlerRegistry.register(ns, handler_cls())


# 模块导入即完成默认 Handler 注册（等效于启动期注册，保证 import 后即可用）
register_default_qfk_handlers()
