"""
容器执行命令构造器测试。
"""

import pytest
from app.tools.acli.container_exec import (
    ContainerCommandBuilder,
    ContainerExecBuildError,
    ContainerRuntime,
    NodeRuntimeContext,
    build_container_command,
)


def test_build_auto_runtime_wrapper_preserves_contract_fields():
    built = ContainerCommandBuilder.build("asv-con", "grep ERROR /sf/log/vtpdaemon.log | tail -50")

    assert built.container == "asv-con"
    assert built.original_command == "grep ERROR /sf/log/vtpdaemon.log | tail -50"
    assert built.runtime == ContainerRuntime.AUTO
    assert "HCI_RUNTIME=$(sh -lc" in built.built_command
    assert "export HCI_CONTAINER HCI_CTR_NS HCI_USER_COMMAND" in built.built_command
    assert "docker exec \"$HCI_CONTAINER\" sh -lc \"$HCI_USER_COMMAND\"" in built.built_command
    assert "crictl exec \"$HCI_CID\" sh -lc \"$HCI_USER_COMMAND\"" in built.built_command
    assert "ctr -n \"$HCI_CTR_NS\" tasks exec" in built.built_command
    assert "unsupported container runtime" in built.built_command


def test_build_with_explicit_docker_runtime_skips_auto_probe_branches():
    built = ContainerCommandBuilder.build(
        "vn-con",
        "ps aux",
        NodeRuntimeContext(runtime=ContainerRuntime.DOCKER),
    )

    assert built.runtime == ContainerRuntime.DOCKER
    assert "command -v docker" in built.probe_command
    assert "command -v crictl" not in built.probe_command


def test_build_rejects_unknown_runtime_from_context():
    with pytest.raises(ContainerExecBuildError, match="不支持的容器运行时"):
        ContainerCommandBuilder.build("asv-con", "ps aux", {"runtime": "podman"})


def test_build_rejects_invalid_container():
    with pytest.raises(ContainerExecBuildError, match="不支持的目标容器"):
        ContainerCommandBuilder.build("bad", "ps aux")


def test_compat_build_container_command_returns_built_command():
    command = build_container_command("vs-cp-manager", "df -h")

    assert command.startswith("HCI_CONTAINER=vs-cp-manager;")
    assert "df -h" in command
