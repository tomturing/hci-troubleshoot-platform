"""
容器执行命令构造器。

Agent 服务无法直连客户 HCI 节点，真正的命令仍通过 terminal_bridge 在远端 SSH 上执行。
本模块只生成一段受控 shell wrapper：先在远端只读探测可用执行入口，再进入目标容器执行用户命令。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from shared.observability.logger import get_logger

logger = get_logger("container-exec-adapter")

ALLOWED_BASH_CONTAINERS = {"host", "asv-con", "vn-con", "vn-agent", "vs-cp-manager"}


class ContainerRuntime(StrEnum):
    """远端容器运行时类型。"""

    HOST = "host"
    CONTAINER_EXEC = "container_exec"
    NERDCTL = "nerdctl"
    DOCKER = "docker"
    CRICTL = "crictl"
    CTR = "ctr"
    AUTO = "auto"


class ContainerExecBuildError(ValueError):
    """容器执行命令构造失败。"""


@dataclass(frozen=True)
class BuiltCommand:
    """容器执行命令构造结果。"""

    container: str
    original_command: str
    built_command: str
    runtime: ContainerRuntime
    probe_command: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeRuntimeContext:
    """节点运行时上下文。

    当前大多数调用还没有可靠的节点版本/运行时元数据，因此默认走远端自动探测。
    后续如果环境采集能提供 runtime，可直接传入 runtime，减少远端探测分支。
    """

    runtime: ContainerRuntime = ContainerRuntime.AUTO
    namespace: str = "k8s.io"

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> NodeRuntimeContext:
        if not value:
            return cls()
        runtime_raw = str(value.get("runtime") or value.get("container_runtime") or ContainerRuntime.AUTO).lower()
        try:
            runtime = ContainerRuntime(runtime_raw)
        except ValueError as exc:
            raise ContainerExecBuildError(f"不支持的容器运行时：{runtime_raw}") from exc
        namespace = str(value.get("namespace") or value.get("containerd_namespace") or "k8s.io")
        return cls(runtime=runtime, namespace=namespace)


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


class ContainerExecAdapter:
    """把结构化容器边界转换为远端可执行 shell wrapper。"""

    _RUNTIME_PROBE = (
        "if command -v container_exec >/dev/null 2>&1; then "
        "printf container_exec; "
        "elif command -v nerdctl >/dev/null 2>&1 && nerdctl -n \"$HCI_CTR_NS\" ps --format '{{.Names}}' 2>/dev/null | grep -Fxq \"$HCI_CONTAINER\"; then "
        "printf nerdctl; "
        "elif command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -Fxq \"$HCI_CONTAINER\"; then "
        "printf docker; "
        "elif command -v crictl >/dev/null 2>&1 && crictl ps --name \"$HCI_CONTAINER\" -q | head -n1 | grep -q .; then "
        "printf crictl; "
        "elif command -v ctr >/dev/null 2>&1 && ctr -n \"$HCI_CTR_NS\" tasks ls 2>/dev/null | awk '{print $1}' | grep -Fxq \"$HCI_CONTAINER\"; then "
        "printf ctr; "
        "else printf unsupported; fi"
    )

    @classmethod
    def build(
        cls,
        *,
        container: str,
        command: str,
        node_context: dict[str, Any] | NodeRuntimeContext | None = None,
    ) -> BuiltCommand:
        """构造远端执行命令。

        失败语义采用 fail-closed：运行时未知或容器不存在时 wrapper 直接 exit 127，不执行用户命令。
        """
        clean_container = str(container or "").strip()
        clean_command = str(command or "").strip()
        if clean_container not in ALLOWED_BASH_CONTAINERS:
            raise ContainerExecBuildError(f"不支持的目标容器：{clean_container}")
        if not clean_command:
            raise ContainerExecBuildError("执行命令不能为空")

        if clean_container == ContainerRuntime.HOST:
            logger.info(
                event="host_exec_command_built",
                command_preview=clean_command[:80],
            )
            return BuiltCommand(
                container=clean_container,
                original_command=clean_command,
                built_command=clean_command,
                runtime=ContainerRuntime.HOST,
                probe_command="",
                metadata={"execution_boundary": "host"},
            )

        if isinstance(node_context, NodeRuntimeContext):
            context = node_context
        else:
            context = NodeRuntimeContext.from_mapping(node_context)

        runtime_probe = cls._runtime_probe_for(context.runtime)
        built_command = cls._build_probe_wrapper(
            container=clean_container,
            command=clean_command,
            runtime_probe=runtime_probe,
            namespace=context.namespace,
        )
        logger.info(
            event="container_exec_command_built",
            container=clean_container,
            runtime=context.runtime.value,
            command_preview=clean_command[:80],
            built_command_preview=built_command[:120],
        )
        return BuiltCommand(
            container=clean_container,
            original_command=clean_command,
            built_command=built_command,
            runtime=context.runtime,
            probe_command=runtime_probe,
            metadata={"containerd_namespace": context.namespace},
        )

    @classmethod
    def _runtime_probe_for(cls, runtime: ContainerRuntime) -> str:
        if runtime == ContainerRuntime.AUTO:
            return cls._RUNTIME_PROBE
        if runtime == ContainerRuntime.CONTAINER_EXEC:
            return "if command -v container_exec >/dev/null 2>&1; then printf container_exec; else printf unsupported; fi"
        if runtime == ContainerRuntime.NERDCTL:
            return "if command -v nerdctl >/dev/null 2>&1; then printf nerdctl; else printf unsupported; fi"
        if runtime == ContainerRuntime.DOCKER:
            return "if command -v docker >/dev/null 2>&1; then printf docker; else printf unsupported; fi"
        if runtime == ContainerRuntime.CRICTL:
            return "if command -v crictl >/dev/null 2>&1; then printf crictl; else printf unsupported; fi"
        if runtime == ContainerRuntime.CTR:
            return "if command -v ctr >/dev/null 2>&1; then printf ctr; else printf unsupported; fi"
        raise ContainerExecBuildError(f"不支持的容器运行时：{runtime}")

    @staticmethod
    def _build_probe_wrapper(*, container: str, command: str, runtime_probe: str, namespace: str) -> str:
        quoted_container = _shell_quote(container)
        quoted_namespace = _shell_quote(namespace)
        quoted_user_command = _shell_quote(command)
        quoted_runtime_probe = _shell_quote(runtime_probe)

        return (
            f"HCI_CONTAINER={quoted_container}; "
            f"HCI_CTR_NS={quoted_namespace}; "
            f"HCI_USER_COMMAND={quoted_user_command}; "
            "export HCI_CONTAINER HCI_CTR_NS HCI_USER_COMMAND; "
            f"HCI_RUNTIME=$(sh -lc {quoted_runtime_probe}); "
            "case \"$HCI_RUNTIME\" in "
            "container_exec) exec container_exec -n \"$HCI_CONTAINER\" -c \"$HCI_USER_COMMAND\" -d ;; "
            "nerdctl) exec nerdctl -n \"$HCI_CTR_NS\" exec \"$HCI_CONTAINER\" sh -lc \"$HCI_USER_COMMAND\" ;; "
            "docker) exec docker exec \"$HCI_CONTAINER\" sh -lc \"$HCI_USER_COMMAND\" ;; "
            "crictl) HCI_CID=$(crictl ps --name \"$HCI_CONTAINER\" -q | head -n1); "
            "if [ -z \"$HCI_CID\" ]; then echo \"[container_exec] container not found: $HCI_CONTAINER\" >&2; exit 127; fi; "
            "exec crictl exec \"$HCI_CID\" sh -lc \"$HCI_USER_COMMAND\" ;; "
            "ctr) if ! ctr -n \"$HCI_CTR_NS\" tasks ls 2>/dev/null | awk '{print $1}' | grep -Fxq \"$HCI_CONTAINER\"; then "
            "echo \"[container_exec] container task not found: $HCI_CONTAINER\" >&2; exit 127; fi; "
            "exec ctr -n \"$HCI_CTR_NS\" tasks exec --exec-id \"hci-$RANDOM-$$\" \"$HCI_CONTAINER\" sh -lc \"$HCI_USER_COMMAND\" ;; "
            "*) echo \"[container_exec] unsupported container runtime or inaccessible container: $HCI_CONTAINER\" >&2; exit 127 ;; "
            "esac"
        )


class ContainerCommandBuilder:
    """对外命令构造入口。"""

    @staticmethod
    def build(
        container: str,
        command: str,
        node_context: dict[str, Any] | NodeRuntimeContext | None = None,
    ) -> BuiltCommand:
        return ContainerExecAdapter.build(container=container, command=command, node_context=node_context)


def build_container_command(container: str, command: str) -> str:
    """兼容旧调用点，返回拼装后的实际命令字符串。"""
    return ContainerCommandBuilder.build(container, command).built_command
