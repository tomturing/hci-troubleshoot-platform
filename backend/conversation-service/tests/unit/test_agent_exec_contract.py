"""
Agent 执行命令事件契约测试。
"""

from app.routes.agent_exec import AgentExecRequest


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
