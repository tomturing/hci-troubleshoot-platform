"""在线语义入口真实编排：纯语义分类、执行白名单与多轮上下文。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.investigation_agent import InvestigationAgent
from app.domain.agent_port import AgentEscalation, AgentStageUpdate, AgentTextChunk


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


def test_answered_semantic_question_advances_even_after_prior_retries():
    question = "当前报错是否为“缺少介质驱动程序”？"
    result = InvestigationAgent._semantic_guidance_messages(
        [
            {"role": "assistant", "content": question},
            {"role": "assistant", "content": question},
            {"role": "user", "content": "当前报错是“缺少介质驱动程序”"},
        ],
        {
            "next_action": {"question": question},
            "candidates": [{"manual_evidence_request": ["请提供安装失败截图"]}],
        },
    )

    assert result == ["当前只能请求补充证据：请提供安装失败截图"]


@pytest.mark.asyncio
async def test_matched_strong_history_still_exposes_guidance_after_inconclusive_cdd(monkeypatch):
    """无关历史任务命中不能吞掉安全的 guidance_only 人工补证据路径。"""

    class Diagnostic:
        def __init__(self, **kwargs):
            pass

        async def diagnose(self, *, candidates, **kwargs):
            yield AgentStageUpdate(stage="checked", metadata={})

        def get_result(self):
            return SimpleNamespace(
                is_definitive=False,
                steps_executed=[
                    SimpleNamespace(
                        tool_name="qkv_task",
                        kbd_id="strong",
                        signal_id="task_failure",
                        error=None,
                        outcome="SATISFIED",
                        match_kbd_ids={"strong"},
                    )
                ],
                diagnosis_report="原 CDD 仍无法确认。",
                conclusion_level="INCONCLUSIVE",
                candidate_states={"strong": "INCONCLUSIVE"},
            )

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", Diagnostic)
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(
        return_value={
            "sops": [],
            "snapshot_id": "snap",
            "kbds": [
                {
                    "id": "strong",
                    "support_id": "15425",
                    "name": "历史任务候选",
                    "executable": True,
                    "signals": [
                        {
                            "id": "task_failure",
                            "acquire": {"tool": "qkv_task", "args": {}},
                            "orchestrate": {"phase": "diagnostic"},
                        },
                        # 第一条必要信号已命中后，CDD 可按候选状态剪枝，不执行这个
                        # 后续告警；它不能让 strong_status 退化为 source_unavailable。
                        {
                            "id": "alert_failure",
                            "acquire": {"tool": "qkv_alert", "args": {}},
                            "orchestrate": {"phase": "diagnostic"},
                        },
                    ],
                },
                {
                    "id": "guidance",
                    "support_id": "15936",
                    "name": "安装介质驱动提示",
                    "executable": False,
                    "signals": [
                        {
                            "id": "case_context",
                            "acquire": {"tool": "qkv_case_context", "args": {}},
                        }
                    ],
                    "semantic_entry_profile": {"diagnosis_capability": "guidance_only"},
                    "resource_revision": {"revision": 1},
                },
            ],
        }
    )
    client.resolve_semantic_entry = AsyncMock(
        return_value={
            "decision": "inconclusive",
            "reason": "guidance_only",
            "candidates": [
                {
                    "kbd_id": "guidance",
                    "manual_evidence_request": ["请提供完整安装报错截图"],
                }
            ],
            "next_action": {"type": "manual_evidence_request", "question": "请确认完整报错文字"},
        }
    )
    registry = MagicMock()
    registry.get_client.return_value = MagicMock()
    agent = InvestigationAgent(ai_registry=registry, kb_client=client, tool_executor=MagicMock())

    events = [
        event
        async for event in agent.process(
            session_id="session",
            case_id="case",
            category_id="虚拟机-001",
            messages=[{"role": "user", "content": "安装 Windows Server 2016 提示缺少介质驱动程序"}],
        )
    ]

    request = client.resolve_semantic_entry.await_args.kwargs
    assert request["strong_producer_status"] == "matched_inconclusive"
    assert any(
        isinstance(event, AgentTextChunk) and "请提供完整安装报错截图" in event.content for event in events
    )
    assert not any(isinstance(event, AgentTextChunk) and "原 CDD 仍无法确认" in event.content for event in events)
    assert not any(isinstance(event, AgentEscalation) for event in events)


@pytest.mark.asyncio
async def test_pending_guidance_skips_cdd_and_advances_to_manual_evidence(monkeypatch):
    """客户已回答语义澄清后，只续接补证据，不重跑全量 CDD。"""

    class Diagnostic:
        def __init__(self, **kwargs):
            raise AssertionError("已有 guidance_only 补证据上下文时不应构造 KBDDiagnostic")

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", Diagnostic)
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(
        return_value={
            "sops": [],
            "snapshot_id": "snap",
            "kbds": [
                {
                    "id": "strong",
                    "support_id": "15425",
                    "name": "历史任务候选",
                    "executable": True,
                    "signals": [{"id": "task", "acquire": {"tool": "qkv_task", "args": {}}}],
                },
                {
                    "id": "guidance",
                    "support_id": "15936",
                    "name": "安装介质驱动提示",
                    "executable": False,
                    "signals": [{"id": "case_context", "acquire": {"tool": "qkv_case_context", "args": {}}}],
                    "semantic_entry_profile": {"diagnosis_capability": "guidance_only"},
                    "resource_revision": {"revision": 1},
                },
            ],
        }
    )
    client.resolve_semantic_entry = AsyncMock(
        return_value={
            "decision": "inconclusive",
            "reason": "guidance_only",
            "candidates": [
                {"kbd_id": "guidance", "manual_evidence_request": ["请提供安装失败截图"]}
            ],
            "next_action": {"question": "当前报错是否为“缺少介质驱动程序”？"},
        }
    )
    registry = MagicMock()
    registry.get_client.return_value = MagicMock()
    agent = InvestigationAgent(ai_registry=registry, kb_client=client, tool_executor=MagicMock())

    events = [
        event
        async for event in agent.process(
            session_id="session",
            case_id="case",
            category_id="虚拟机-001",
            messages=[
                {"role": "assistant", "content": "当前只能请求补充证据：请提供安装失败截图"},
                {"role": "user", "content": "当前报错是“缺少介质驱动程序”"},
            ],
        )
    ]

    assert client.resolve_semantic_entry.await_args.kwargs["strong_producer_status"] == "matched_inconclusive"
    assert any(isinstance(event, AgentTextChunk) and "请提供安装失败截图" in event.content for event in events)
    assert not any(isinstance(event, AgentEscalation) for event in events)
