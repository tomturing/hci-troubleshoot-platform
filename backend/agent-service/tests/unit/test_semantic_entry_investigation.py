"""在线语义入口真实编排：纯语义分类、执行白名单与多轮上下文。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.investigation_agent import InvestigationAgent
from app.domain.agent_port import AgentEscalation, AgentStageUpdate, AgentTextChunk


@pytest.mark.asyncio
@pytest.mark.parametrize("sop_status", [None, "aborted"])
async def test_reference_only_category_or_aborted_sop_recommends_without_cdd(monkeypatch, sop_status):
    from shared.schemas.semantic_routing import resolve_candidates

    diagnostic = MagicMock()
    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", diagnostic)
    case = {
        "id": "1", "title": "安装系统提示缺少介质驱动程序", "support_id": "15936",
        "solution": "已审核的历史方案", "executable": False, "signals": [],
        "resource_revision": {"revision": 8},
    }
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(return_value={
        "sops": [{"id": 42, "title": "已有 SOP"}] if sop_status else [], "kbds": [case], "snapshot_id": "snap",
    })

    async def resolve(**kwargs):
        return await resolve_candidates(
            entries=[{**case, "signals_json": {"schema_version": 2, "signals": []}}],
            context=kwargs["case_context"], strong_status=kwargs["strong_producer_status"],
            include_reference_cases=kwargs["include_reference_cases"],
        )

    client.resolve_semantic_entry = AsyncMock(side_effect=resolve)
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    events = [event async for event in agent.process(
        session_id="s", category_id="vm", messages=[{"role": "user", "content": "安装系统提示缺少介质驱动程序"}],
        sop_resume_context={"sop_document_id": 42, "status": sop_status} if sop_status else None,
    )]
    diagnostic.assert_not_called()
    text = "".join(event.content for event in events if isinstance(event, AgentTextChunk))
    assert "15936" in text and "已审核的历史方案" in text and "尚未" in text
    assert not any(isinstance(event, AgentStageUpdate) and event.stage in {"S4", "sop_reasoning"} for event in events)
    assert client.resolve_semantic_entry.await_args.kwargs["expected_revisions"] == {"1": 8}


@pytest.mark.asyncio
async def test_inconclusive_cdd_passes_rejections_to_recommendations_and_preserves_report(monkeypatch):
    result = SimpleNamespace(
        is_definitive=False, conclusion_level="INCONCLUSIVE", candidate_states={"1": "REJECTED", "2": "INCONCLUSIVE"},
        diagnosis_report="现场采集失败，尚未确认。", steps_executed=[],
    )

    class Diagnostic:
        def __init__(self, **kwargs):
            pass

        async def diagnose(self, **kwargs):
            yield AgentStageUpdate(stage="checked", metadata={})

        def get_result(self):
            return result

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", Diagnostic)
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(return_value={"sops": [], "kbds": [
        {"id": "1", "name": "强信号案例", "signals": [], "executable": True},
        {"id": "2", "name": "参考案例", "signals": [], "executable": False},
    ]})
    client.resolve_semantic_entry = AsyncMock(return_value={
        "decision": "case_recommendations", "strong_producer_status": "source_unavailable",
        "candidates": [{"kbd_id": "2", "title": "参考案例", "recommendation_solution": "仅供参考"}],
    })
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    events = [event async for event in agent.process(session_id="s", category_id="vm", messages=[{"role": "user", "content": "启动失败"}])]
    request = client.resolve_semantic_entry.await_args.kwargs
    assert request["excluded_kbd_ids"] == ["1"]
    assert request["strong_producer_status"] == "source_unavailable"
    text = "".join(event.content for event in events if isinstance(event, AgentTextChunk))
    assert result.diagnosis_report in text and "不能视为未命中" in text
    assert not any(isinstance(event, AgentStageUpdate) and event.stage == "S4" for event in events)


@pytest.mark.asyncio
async def test_semantic_validation_failure_falls_back_once_and_keeps_both_rounds_rejections(monkeypatch):
    calls = []

    class Diagnostic:
        def __init__(self, **kwargs):
            self.index = len(calls)
            calls.append(self)

        async def diagnose(self, **kwargs):
            yield AgentStageUpdate(stage="checked", metadata={})

        def get_result(self):
            first = self.index == 0
            return SimpleNamespace(
                is_definitive=False, conclusion_level="NO_MATCH", diagnosis_report="检查未支持候选。",
                candidate_states={"strong" if first else "semantic": "REJECTED"},
                steps_executed=[SimpleNamespace(tool_name="qkv_task", error=None, outcome="CONTRADICTED", match_kbd_ids=set())] if first else [],
            )

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", Diagnostic)
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(return_value={"sops": [], "kbds": [
        {"id": "strong", "name": "任务案例", "executable": True, "signals": [{"acquire": {"tool": "qkv_task"}}]},
        {"id": "semantic", "name": "语义案例", "executable": True, "signals": [], "semantic_entry_profile": {"diagnosis_capability": "executable"}},
        {"id": "reference", "name": "参考案例", "executable": False, "signals": []},
    ]})
    client.resolve_semantic_entry = AsyncMock(side_effect=[
        {"decision": "executable", "candidates": [{"kbd_id": "semantic"}]},
        {"decision": "case_recommendations", "candidates": [{"kbd_id": "reference", "title": "参考案例"}]},
    ])
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    events = [event async for event in agent.process(session_id="s", category_id="vm", messages=[{"role": "user", "content": "安装失败"}])]
    assert len(calls) == 2
    request = client.resolve_semantic_entry.await_args.kwargs
    assert request["recommendation_only"] is True
    assert request["excluded_kbd_ids"] == ["semantic", "strong"]
    assert any(isinstance(event, AgentTextChunk) and "参考案例" in event.content for event in events)
    assert not any(isinstance(event, AgentStageUpdate) and event.stage == "S4" for event in events)


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


def test_evidence_request_satisfied_by_user_keywords():
    """用户消息中包含 evidence request 的多个关键词时，视为已满足。"""
    messages = [
        {"role": "assistant", "content": "当前只能请求补充证据：请提供所用 ISO 的文件名、SHA1 和文件大小"},
        {"role": "user", "content": "iso文件名是test.iso、sha1是643af07a077069e7c0784986eab7796e9a1fb854、文件大小1G"},
    ]
    assert InvestigationAgent._evidence_request_satisfied(messages, "请提供所用 ISO 的文件名、SHA1 和文件大小")


def test_iso_mention_alone_does_not_satisfy_integrity_evidence_request():
    messages = [
        {"role": "user", "content": "通过 ISO 安装 Windows Server 2016 时提示缺少驱动程序"},
    ]

    assert not InvestigationAgent._evidence_request_satisfied(
        messages, "请提供所用 ISO 的文件名、SHA1 和文件大小"
    )


def test_evidence_request_satisfied_controller_driver():
    """用户提到磁盘控制器和驱动时，满足对应 evidence request。"""
    messages = [
        {"role": "assistant", "content": "当前只能请求补充证据：请确认虚拟磁盘控制器类型以及是否已加载对应驱动"},
        {"role": "user", "content": "虚拟磁盘控制器类型是virtio并且已经加载virtio驱动"},
    ]
    assert InvestigationAgent._evidence_request_satisfied(
        messages, "请确认虚拟磁盘控制器类型以及是否已加载对应驱动（如 VirtIO）"
    )


def test_evidence_request_not_satisfied_when_no_match():
    """用户消息与 evidence request 无关时，不应标记为已满足。"""
    messages = [
        {"role": "assistant", "content": "当前只能请求补充证据：请提供安装失败截图"},
        {"role": "user", "content": "你好，请问这个问题怎么解决"},
    ]
    assert not InvestigationAgent._evidence_request_satisfied(messages, "请提供安装失败界面的完整报错截图")


def test_structured_evidence_uses_only_matching_customer_form_submission():
    candidate = {
        "kbd_id": "15936",
        "manual_evidence_fields": [
            {"id": "controller_type", "label": "控制器类型"},
            {"id": "driver_loaded", "label": "驱动已加载"},
        ],
    }
    messages = [
        {"role": "user", "content": "VirtIO，已加载"},
        {
            "role": "user",
            "content": "补充证据",
            "metadata": {
                "kind": "semantic_evidence_response",
                "candidateId": "15936",
                "values": {"controller_type": "VirtIO", "driver_loaded": "已加载", "untrusted": "忽略"},
            },
        },
    ]
    assert InvestigationAgent._structured_evidence_values(messages, candidate) == {
        "controller_type": "VirtIO",
        "driver_loaded": "已加载",
    }


def test_structured_evidence_guidance_requests_only_missing_declared_fields():
    result = InvestigationAgent._semantic_guidance_messages(
        [
            {
                "role": "user",
                "content": "补充证据",
                "metadata": {
                    "kind": "semantic_evidence_response",
                    "candidateId": "223",
                    "values": {"controller_type": "VirtIO", "driver_loaded": "已加载"},
                },
            }
        ],
        {
            "candidates": [
                {
                    "kbd_id": "223",
                    "manual_evidence_fields": [
                        {"id": "controller_type", "label": "控制器类型"},
                        {"id": "driver_loaded", "label": "驱动加载状态"},
                        {"id": "iso_filename", "label": "ISO 文件名"},
                    ],
                }
            ]
        },
    )
    assert result[-1] == "当前只能请求补充证据：请通过补证据表单填写：ISO 文件名"


def test_guidance_filters_already_provided_evidence():
    """用户已提供的 evidence request 不应重复出现在 guidance 输出中。"""
    question = '当前报错是否为“缺少介质驱动程序”？'
    result = InvestigationAgent._semantic_guidance_messages(
        [
            {"role": "assistant", "content": "目前无法确认根因。" + question},
            {"role": "assistant", "content": "当前只能请求补充证据：请提供 ISO 文件名；请提供截图；请确认磁盘控制器"},
            {"role": "user", "content": "iso文件名是test.iso，sha1是abc123"},
        ],
        {
            "next_action": {"question": question},
            "candidates": [
                {
                    "support_id": "15936",
                    "title": "ISO 安装缺少介质驱动程序",
                    "manual_evidence_request": [
                        "请提供所用 ISO 的文件名、SHA1 和文件大小",
                        "请提供安装失败界面的完整报错截图",
                        "请确认虚拟磁盘控制器类型以及是否已加载对应驱动",
                    ]
                }
            ],
        },
    )
    # 缺少合法 SHA1 和文件大小时，ISO 完整性证据不能被仅有的“iso 文件名”误过滤。
    assert any("语义案例" in item for item in result)
    evidence = next(item for item in result if "当前只能请求补充证据" in item)
    assert "ISO" in evidence
    assert "截图" in evidence
    assert "控制器" in evidence


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


@pytest.mark.asyncio
async def test_definitive_cdd_skips_reference_recommendations(monkeypatch):
    """CDD 确定性命中后，不再追加 reference_only 推荐（诊断已明确）。"""
    matched_kbd = SimpleNamespace(id="1")
    result = SimpleNamespace(
        is_definitive=True,
        conclusion_level="DEFINITIVE",
        candidate_states={"1": "SATISFIED", "2": "CONTRADICTED"},
        diagnosis_report="已确认根因：xxx。",
        steps_executed=[SimpleNamespace(tool_name="qkv_task", error=None, outcome="SATISFIED", match_kbd_ids={"1"})],
        matched_kbds=[matched_kbd],
    )

    class Diagnostic:
        def __init__(self, **kwargs):
            pass

        async def diagnose(self, **kwargs):
            yield AgentStageUpdate(stage="checked", metadata={})

        def get_result(self):
            return result

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", Diagnostic)
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(return_value={"sops": [], "kbds": [
        {"id": "1", "name": "强信号案例", "signals": [{"acquire": {"tool": "qkv_task"}}], "executable": True},
        {"id": "2", "name": "参考案例", "signals": [], "executable": False},
    ]})
    client.resolve_semantic_entry = AsyncMock()
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    events = [event async for event in agent.process(session_id="s", category_id="vm", messages=[{"role": "user", "content": "安装失败"}])]
    # 验证 S4 阶段输出（确定性 CDD）
    assert any(isinstance(event, AgentStageUpdate) and event.stage == "S4" for event in events)
    # 确定性命中后不应调用 resolve_semantic_entry（减少噪音）
    client.resolve_semantic_entry.assert_not_awaited()
    # 无 reference_recommendation 事件
    assert not any(isinstance(event, AgentStageUpdate) and event.stage == "reference_recommendation" for event in events)


@pytest.mark.asyncio
async def test_inconclusive_cdd_no_reference_kbds_skips_extra_api_call(monkeypatch):
    """非确定性 CDD 但分类内全部是可执行 KBD 时，不触发多余的推荐调用。"""
    result = SimpleNamespace(
        is_definitive=False,
        conclusion_level="INCONCLUSIVE",
        candidate_states={"1": "CONTRADICTED", "2": "CONTRADICTED"},
        diagnosis_report="现场采集不足。",
        steps_executed=[],
        matched_kbds=[],
    )

    class Diagnostic:
        def __init__(self, **kwargs):
            pass

        async def diagnose(self, **kwargs):
            yield AgentStageUpdate(stage="checked", metadata={})

        def get_result(self):
            return result

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", Diagnostic)
    client = MagicMock()
    # 所有 KBD 都是可执行的（有强生产者信号），无 reference_only KBD
    client.get_category_playbooks = AsyncMock(return_value={"sops": [], "kbds": [
        {"id": "1", "name": "强信号案例", "signals": [{"acquire": {"tool": "qkv_task"}}], "executable": True},
        {"id": "2", "name": "另一个强信号案例", "signals": [{"acquire": {"tool": "qkv_alert"}}], "executable": True},
    ]})
    # 第一次调用是语义兜底（非确定性触发），第二次不应被调用（预检查跳过）
    client.resolve_semantic_entry = AsyncMock(return_value={
        "decision": "inconclusive", "candidates": [],
    })
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    events = [event async for event in agent.process(session_id="s", category_id="vm", messages=[{"role": "user", "content": "安装失败"}])]
    # 只有语义兜底的 1 次调用，不应有第 2 次推荐调用
    assert client.resolve_semantic_entry.await_count == 1
    # 无 reference_recommendation 事件
    assert not any(isinstance(event, AgentStageUpdate) and event.stage == "reference_recommendation" for event in events)


@pytest.mark.asyncio
async def test_inconclusive_cdd_with_strong_producer_first_triggers_reference_fallback(monkeypatch):
    """非确定性 CDD + 语义兜底返回 strong_producer_first 时，显式触发 reference 推荐。"""
    result = SimpleNamespace(
        is_definitive=False,
        conclusion_level="INCONCLUSIVE",
        candidate_states={"1": "SATISFIED", "2": "CONTRADICTED"},
        diagnosis_report="多个信号矛盾，尚未确认根因。",
        steps_executed=[SimpleNamespace(tool_name="qkv_task", error=None, outcome="SATISFIED", match_kbd_ids={"1"})],
        matched_kbds=[],
    )

    class Diagnostic:
        def __init__(self, **kwargs):
            pass

        async def diagnose(self, **kwargs):
            yield AgentStageUpdate(stage="checked", metadata={})

        def get_result(self):
            return result

    monkeypatch.setattr("app.adapters.agents.htp.investigation_agent.KBDDiagnostic", Diagnostic)
    client = MagicMock()
    client.get_category_playbooks = AsyncMock(return_value={"sops": [], "kbds": [
        {"id": "1", "name": "强信号案例", "signals": [{"acquire": {"tool": "qkv_task"}}], "executable": True},
        {"id": "2", "name": "参考案例", "signals": [], "executable": False},
    ]})
    # 第一次调用（语义兜底）返回 strong_producer_first（recommend_cases 未找到匹配）
    # 第二次调用（reference 兜底）返回 case_recommendations
    client.resolve_semantic_entry = AsyncMock(side_effect=[
        {"decision": "strong_producer_first", "reason": "strong_producer_matched", "candidates": []},
        {
            "decision": "case_recommendations",
            "strong_producer_status": "matched",
            "candidates": [{"kbd_id": "2", "title": "参考案例", "support_id": "42933", "recommendation_solution": "仅供参考"}],
        },
    ])
    agent = InvestigationAgent(ai_registry=MagicMock(), kb_client=client, tool_executor=MagicMock())
    events = [event async for event in agent.process(session_id="s", category_id="vm", messages=[{"role": "user", "content": "安装失败"}])]
    # 验证 reference_recommendation 事件被触发
    assert any(isinstance(event, AgentStageUpdate) and event.stage == "reference_recommendation" for event in events)
    text = "".join(event.content for event in events if isinstance(event, AgentTextChunk))
    assert "42933" in text
    # 验证调用了 2 次 resolve_semantic_entry
    assert client.resolve_semantic_entry.await_count == 2
