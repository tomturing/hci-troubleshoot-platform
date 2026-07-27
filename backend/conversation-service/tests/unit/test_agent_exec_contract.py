"""
Agent 执行命令事件契约测试。
"""

import pytest
from app.routes.agent_exec import AgentExecRequest
from pydantic import ValidationError


def test_agent_exec_request_accepts_container_audit_fields():
    request = AgentExecRequest(
        exec_id="exec-123",
        tool_name="bash_exec",
        command="HCI_CONTAINER=asv-con; ...",
        container="asv-con",
        original_command="df -h",
        built_command="HCI_CONTAINER=asv-con; ...",
        reason="检查磁盘",
        risk_level=1,
        node_ip="192.168.1.10",
        case_id="Q2026061114729",
        trace_id="caa7e3e825ba4a606df189740be1118c",
        traceparent="00-caa7e3e825ba4a606df189740be1118c-cbef2f8fb7e2d3a8-03",
    )

    assert request.tool_name == "bash_exec"
    assert request.container == "asv-con"
    assert request.original_command == "df -h"
    assert request.built_command == request.command
    assert request.trace_id == "caa7e3e825ba4a606df189740be1118c"
    assert request.traceparent == "00-caa7e3e825ba4a606df189740be1118c-cbef2f8fb7e2d3a8-03"


def test_agent_exec_request_accepts_safe_output_filter_and_timeout():
    request = AgentExecRequest(
        exec_id="exec-123",
        tool_name="acli_exec",
        command="acli system lsof",
        reason="检查镜像占用",
        risk_level=1,
        timeout=90,
        output_filters=[
            {
                "source": "stdout",
                "include": ["4359974862144"],
                "exclude": ["grep"],
                "include_mode": "all",
                "case_sensitive": True,
            }
        ],
    )
    assert request.timeout == 90
    assert request.output_filters[0].include == ["4359974862144"]


@pytest.mark.parametrize(
    "output_filter",
    [{}, {"source": "stdout", "include": [""]}, {"source": "combined", "include": ["VM"]}, {"source": "stdout", "include": ["x" * 513]}],
)
def test_agent_exec_request_rejects_unsafe_or_empty_output_filter(output_filter):
    with pytest.raises(ValidationError):
        AgentExecRequest(
            exec_id="exec-123",
            tool_name="acli_exec",
            command="acli system lsof",
            reason="检查镜像占用",
            risk_level=1,
            output_filters=[output_filter],
        )
