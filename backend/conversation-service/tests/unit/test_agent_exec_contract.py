"""
Agent 执行命令事件契约测试。
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.routes import agent_exec
from app.routes.agent_exec import AgentExecRequest, ExecResultRequest
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

    assert request.tool_name == "bash_exec"
    assert request.container == "asv-con"
    assert request.original_command == "df -h"
    assert request.built_command == request.command
    assert request.timeout == 90
    assert request.output_filters[0].include == ["4359974862144"]


@pytest.mark.parametrize(
    "output_filter",
    [
        {},
        {"source": "stdout", "include": [""]},
        {"source": "combined", "include": ["VM"]},
        {"source": "stdout", "include": ["x" * 513]},
    ],
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


def test_exec_result_request_rejects_oversized_compatibility_output():
    with pytest.raises(ValidationError):
        ExecResultRequest(
            exec_id="exec-large",
            output="x" * (256 * 1024 + 1),
            exit_code=0,
        )


def test_exec_result_request_rejects_oversized_combined_streams():
    with pytest.raises(ValidationError):
        ExecResultRequest(
            exec_id="exec-large-streams",
            output="",
            exit_code=0,
            stdout="x" * (128 * 1024 + 1),
            stderr="y" * (128 * 1024),
        )


@pytest.mark.asyncio
async def test_case_id_resolution_keeps_session_alive_until_query_finishes(monkeypatch):
    conversation_id = "00000000-0000-0000-0000-000000008529"
    result = MagicMock()
    result.scalar_one_or_none.return_value = "Q2026072785259"
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock()
    db.async_session_factory.return_value = context
    monkeypatch.setattr(agent_exec, "_db_manager", db)

    resolved = await agent_exec._resolve_conversation_case_id(uuid.UUID(conversation_id))

    assert resolved == "Q2026072785259"
    session.execute.assert_awaited_once()
    context.__aexit__.assert_awaited_once()
