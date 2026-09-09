"""在线语义入口真实编排：纯语义分类、执行白名单与多轮上下文。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.investigation_agent import InvestigationAgent
from app.domain.agent_port import AgentStageUpdate, AgentTextChunk


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["safe-only", "sim-ssh"])
async def test_semantic_only_category_reaches_cdd_with_authoritative_revision(monkeypatch, mode):
    seen = []

    class Diagnostic:
        def __init__(self, **kwargs):
            pass

        async def diagnose(self, *, candidates, **kwargs):
            seen.append([candidate.id for candidate in candidates])
            yield AgentStageUpdate(stage="checked", metadata={})

        def get_result(self):
            return None

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", Diagnostic)
    cases = [
        {
            "id": "1",
            "name": "格式检查",
            "support_id": "kb-1",
            "signals": [],
            "executable": True,
            "semantic_entry_profile": {"diagnosis_capability": "executable"},
            "resource_revision": {"revision": 8},
        }
    ]
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(return_value={"sops": [], "kbds": cases, "snapshot_id": "snap"})
    client.resolve_semantic_entry = AsyncMock(return_value={"decision": "executable", "candidates": [{"kbd_id": "1"}]})
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    events = [
        event
        async for event in agent.process(
            session_id="session",
            category_id="vm",
            execution_mode=mode,
            messages=[
                {"role": "user", "content": "创建虚拟机失败"},
                {"role": "assistant", "content": "请补充错误"},
                {"role": "user", "content": "镜像格式不支持"},
                {"role": "user", "content": "继续"},
            ],
        )
    ]
    assert seen == [["1"]]
    request = client.resolve_semantic_entry.await_args.kwargs
    assert request["strong_producer_status"] == "not_applicable"
    assert request["expected_revisions"] == {"1": 8}
    assert request["case_context"]["description"] == "创建虚拟机失败\n镜像格式不支持"
    assert any(isinstance(event, AgentStageUpdate) and event.stage == "semantic_entry_fallback" for event in events)


@pytest.mark.asyncio
async def test_guidance_is_shown_without_constructing_diagnostic(monkeypatch):
    diagnostic = MagicMock()
    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", diagnostic)
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(
        return_value={
            "sops": [],
            "kbds": [
                {
                    "id": "1",
                    "signals": [],
                    "executable": False,
                    "semantic_entry_profile": {"diagnosis_capability": "guidance_only"},
                }
            ],
        }
    )
    client.resolve_semantic_entry = AsyncMock(
        return_value={"decision": "inconclusive", "next_action": {"question": "请提供蓝屏错误文字"}}
    )
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    events = [
        event
        async for event in agent.process(
            session_id="s", category_id="vm", messages=[{"role": "user", "content": "蓝屏"}]
        )
    ]
    diagnostic.assert_not_called()
    assert any(isinstance(event, AgentTextChunk) and "请提供蓝屏错误文字" in event.content for event in events)


def test_repeated_semantic_question_is_bounded_without_counting_user_answers():
    question = "请提供蓝屏错误文字"
    assert InvestigationAgent._semantic_question_exhausted([{"role": "assistant", "content": question}] * 2, question)
    assert not InvestigationAgent._semantic_question_exhausted([{"role": "user", "content": question}] * 3, question)
