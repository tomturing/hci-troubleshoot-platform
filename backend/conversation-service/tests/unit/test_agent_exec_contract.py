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
    )

    assert request.tool_name == "bash_exec"
    assert request.container == "asv-con"
    assert request.original_command == "df -h"
    assert request.built_command == request.command
