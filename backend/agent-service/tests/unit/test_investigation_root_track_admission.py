"""根因链路编排准入测试：关键链路无候选时根因轨必须独立进入 CDD。

背景（真 live E2E probe2 实锤）：#10(40061) 关键链路 stale、#2762(44871) 关键信号 0 条，
但两篇均有 root_executable=True 的根因信号；编排层旧准入只看关键链路 ``executable``，
关键候选为空即走语义兜底/升级人工收口，``diagnose_with_tracks`` 永远拿不到 root_cases，
违反两轨「完整独立、互不影响、可以分别单独使用」硬约束。本文件钉住修复后的准入语义：

1. dual（默认）下关键候选为空 + 存在可执行根因信号 → 跳过语义兜底，直接进
   ``diagnose_with_tracks(candidates=[], root_cases=[...])``；
2. ``process_only`` 下行为恒等（根因轨不在赛马模式内，回退旧降级收口）；
3. 仅 text 锚点（root_executable=False）不构成准入，旧降级路径不变。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.investigation_agent import InvestigationAgent
from app.domain.agent_port import AgentEscalation, AgentStageUpdate, AgentTextChunk

FALLBACK_TEXTS = ("没有可安全执行的语义候选", "均未通过自动执行契约")


def _root_only_case() -> dict:
    """模拟 kb-service 下发的 44871 形态：关键链路 0 信号 + 可执行根因信号。"""
    return {
        "id": "2762",
        "support_id": "44871",
        "title": "升级前检测嵌套虚拟化",
        "executable": False,
        "execution_issues": ["仅案例推荐：无现场验证信号，不进入自动执行 CDD"],
        "diagnosis_capability": "reference_only",
        "signals": [],
        "semantic_entry_profile": {},
        "resource_revision": {"revision": 1},
        "root_executable": True,
        "root_signals": [
            {
                "id": "root_bbd360f698c6",
                "acquire": {"tool": "qfk_system", "args": {"command": "cat", "command_args": ["/sf/modules/loadmod.conf"]}},
                "match": {"type": "keyword", "pattern": "", "mode": "or", "expected": True},
            }
        ],
        "root_display_anchors": [],
    }


def _agent_with_case(case: dict, monkeypatch) -> tuple[InvestigationAgent, AsyncMock]:
    """构造最小可跑的 InvestigationAgent：诊断器与 diagnose_with_tracks 均为替身。"""
    diagnostic = MagicMock()
    diagnostic.return_value.get_result.return_value = None
    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", diagnostic)

    tracked: list[dict] = []

    async def fake_diagnose(engine, *, candidates, root_cases, **kwargs):
        tracked.append({"candidates": candidates, "root_cases": root_cases})
        yield AgentStageUpdate(stage="root_diag_complete", metadata={})

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.diagnose_with_tracks", fake_diagnose)

    client = MagicMock()
    client.get_category_playbooks = AsyncMock(return_value={"sops": [], "kbds": [case], "snapshot_id": "snap"})
    # 语义兜底一旦被调用即记账，用于断言准入路径绕过了它。
    client.resolve_semantic_entry = AsyncMock(return_value=None)
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    return agent, client, tracked  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_root_executable_admits_cdd_when_process_track_empty(monkeypatch):
    """关键候选为空但根因轨可执行（dual 默认）：必须直入 diagnose_with_tracks，不得语义兜底。"""
    monkeypatch.delenv("KB_SIGNAL_TRACK_MODE", raising=False)
    agent, client, tracked = _agent_with_case(_root_only_case(), monkeypatch)

    events = [
        event
        async for event in agent.process(
            session_id="s",
            category_id="平台-013",
            messages=[{"role": "user", "content": "升级前检测报错嵌套虚拟化"}],
        )
    ]

    assert len(tracked) == 1, "根因轨可执行时必须进入双链路编排"
    assert tracked[0]["candidates"] == [], "关键链路候选保持为空，不混入根因载荷"
    assert [str(k["id"]) for k in tracked[0]["root_cases"]] == ["2762"]
    client.resolve_semantic_entry.assert_not_awaited()
    text = "".join(event.content for event in events if isinstance(event, AgentTextChunk))
    assert not any(marker in text for marker in FALLBACK_TEXTS)
    assert not any(isinstance(event, AgentEscalation) for event in events)


@pytest.mark.asyncio
async def test_process_only_mode_identity_root_track_not_admitted(monkeypatch):
    """process_only：根因轨不在赛马模式内，准入标记不成立，旧语义兜底降级路径恒等。"""
    monkeypatch.setenv("KB_SIGNAL_TRACK_MODE", "process_only")
    agent, client, tracked = _agent_with_case(_root_only_case(), monkeypatch)

    events = [
        event
        async for event in agent.process(
            session_id="s",
            category_id="平台-013",
            messages=[{"role": "user", "content": "升级前检测报错嵌套虚拟化"}],
        )
    ]

    assert tracked == [], "process_only 下不得为根因轨新开 CDD 路径"
    client.resolve_semantic_entry.assert_awaited()
    text = "".join(event.content for event in events if isinstance(event, AgentTextChunk))
    assert "没有可安全执行的语义候选" in text


@pytest.mark.asyncio
async def test_text_anchor_only_does_not_admit(monkeypatch):
    """仅 text 锚点（root_executable=False）不构成执行准入：仍走旧降级收口。"""
    monkeypatch.delenv("KB_SIGNAL_TRACK_MODE", raising=False)
    case = _root_only_case()
    case["root_executable"] = False
    case["root_signals"] = []
    case["root_display_anchors"] = [{"signal_text": "嵌套虚拟化开关", "keywords": ["kvm_intel nested=1"]}]
    agent, client, tracked = _agent_with_case(case, monkeypatch)

    events = [
        event
        async for event in agent.process(
            session_id="s",
            category_id="平台-013",
            messages=[{"role": "user", "content": "升级前检测报错嵌套虚拟化"}],
        )
    ]

    assert tracked == []
    client.resolve_semantic_entry.assert_awaited()


@pytest.mark.asyncio
async def test_admission_flag_observability(monkeypatch):
    """knowledge_snapshot 阶段必须上报 root_track_admissible，供调用链追踪准入判定。"""
    monkeypatch.delenv("KB_SIGNAL_TRACK_MODE", raising=False)
    agent, _, _ = _agent_with_case(_root_only_case(), monkeypatch)

    events = [
        event
        async for event in agent.process(
            session_id="s",
            category_id="平台-013",
            messages=[{"role": "user", "content": "升级前检测报错嵌套虚拟化"}],
        )
    ]

    snapshot = next(
        event for event in events if isinstance(event, AgentStageUpdate) and event.stage == "knowledge_snapshot"
    )
    assert snapshot.metadata["root_executable_kbd_count"] == 1
    assert snapshot.metadata["root_track_admissible"] is True
