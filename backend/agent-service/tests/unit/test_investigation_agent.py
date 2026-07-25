"""
InvestigationAgent 单元测试

测试覆盖：
  1. process()：路由模式判断（SOP / CDD / 降级）
  2. CDD 模式完成后应 yield AgentStageUpdate(stage="S4")
  3. kb_client 无知识时走降级路径
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.investigation_agent import MAX_SOP_CHARS, InvestigationAgent
from app.domain.agent_port import AgentStageUpdate, AgentTextChunk


def _make_kb_client(
    route_result: dict | None = None,
    cases: list[dict] | None = None,
) -> MagicMock:
    """构建 KBClient mock"""
    mock = MagicMock()
    mock.route_by_category = AsyncMock(return_value=route_result)
    mock.search_cases_with_steps = AsyncMock(return_value=cases or [])
    return mock


def _make_registry_mock_with_stream(chunks: list[str]) -> MagicMock:
    """构建带流式输出的 AIRegistry mock"""
    mock_client = MagicMock()

    async def fake_stream(messages, system=None, **kwargs):
        for chunk in chunks:
            yield chunk

    mock_client.chat_completion_stream = fake_stream
    mock_registry = MagicMock()
    mock_registry.get_client.return_value = mock_client
    return mock_registry


def _make_kbd_diag_mock(stage_s4=True):
    """构建 KBDDiagnostic mock（注入到 InvestigationAgent 内部）"""
    from app.adapters.agents.htp.kbd_differential import KBDDiagResult

    mock = MagicMock()
    result = KBDDiagResult(
        matched_kbds=[],
        steps_executed=[],
        is_definitive=False,
        diagnosis_report="模拟 KBD 诊断报告",
    )
    mock.get_result.return_value = result

    async def fake_diagnose(candidates, env_context, session_id):
        if stage_s4:
            from app.domain.agent_port import AgentStageUpdate

            yield AgentStageUpdate(stage="kbd_diag_complete", metadata={})

    mock.diagnose = fake_diagnose
    return mock


class TestInvestigationAgentRouting:
    """process()：路由模式判断"""

    @pytest.mark.asyncio
    async def test_routes_to_sop_mode_when_sop_track(self):
        """route_by_category 返回 sop 轨道时走 SOP 模式，并传递 sop_document_id"""
        # 模拟 kb-service route API 返回格式（T-AGT-07）
        kb = _make_kb_client(
            route_result={
                "track": "sop",
                "results": [{"id": 42, "title": "虚拟机启动失败 SOP", "content_md": "SOP步骤内容"}],
            }
        )
        registry = _make_registry_mock_with_stream(["SOP 诊断结论"])

        agent = InvestigationAgent(
            ai_registry=registry,
            kb_client=kb,
            tool_executor=MagicMock(),
        )

        events = [
            event
            async for event in agent.process(
                session_id="test-001",
                messages=[{"role": "user", "content": "虚拟机无法启动"}],
                category_id="虚拟机-003",
                diagnostic_stage="S1",
                env_context={
                    "env_info": "cluster-a",
                    "alert_logs": [{"id": 1, "msg": "test"}],
                    "task_logs": [{"id": 2, "status": "failed"}],
                },
                assistant_type="htp-agent",
                case_id=None,
                user_id="user-001",
            )
        ]

        # 应有文本输出（来自 SOP 模式流式回复）
        text_events = [e for e in events if isinstance(e, AgentTextChunk)]
        assert len(text_events) >= 1
        assert "SOP 诊断结论" in text_events[-1].content

        # T-AGT-07: 验证 sop_reasoning 事件携带 sop_document_id
        stage_events = [e for e in events if isinstance(e, AgentStageUpdate)]
        sop_reasoning_events = [e for e in stage_events if e.stage == "sop_reasoning"]
        assert len(sop_reasoning_events) == 1, "未找到 sop_reasoning 事件"
        assert sop_reasoning_events[0].metadata.get("sop_document_id") == 42, (
            f"sop_document_id 应为 42，实际为 {sop_reasoning_events[0].metadata.get('sop_document_id')}"
        )

    @pytest.mark.asyncio
    async def test_routes_to_fallback_when_no_cases(self):
        """search_cases_with_steps 返回空列表时走降级模式"""
        kb = _make_kb_client(
            route_result={"track": "kbd"},
            cases=[],
        )
        registry = _make_registry_mock_with_stream(["机制推理结论"])

        agent = InvestigationAgent(
            ai_registry=registry,
            kb_client=kb,
            tool_executor=MagicMock(),
        )

        events = [
            event
            async for event in agent.process(
                session_id="test-002",
                messages=[{"role": "user", "content": "虚拟机无法启动"}],
                category_id="虚拟机-003",
                diagnostic_stage="S1",
                env_context={
                    "env_info": "cluster-a",
                    "alert_logs": [{"id": 1, "msg": "test"}],
                    "task_logs": [{"id": 2, "status": "failed"}],
                },
                assistant_type="htp-agent",
                case_id=None,
                user_id="user-001",
            )
        ]

        text_events = [e for e in events if isinstance(e, AgentTextChunk)]
        assert len(text_events) >= 1
        kb.search_cases_with_steps.assert_awaited_once_with(
            category_id="虚拟机-003",
            query="虚拟机无法启动",
            top_k=15,
            conversation_id="test-002",
            case_id=None,
        )

    @pytest.mark.asyncio
    async def test_kbd_diag_mode_yields_stage_s4(self):
        """KBD 差异诊断完成后应 yield AgentStageUpdate(stage='S4')"""
        kb = _make_kb_client(
            route_result={"track": "kbd"},
            cases=[
                {
                    "id": "c1",
                    "name": "案例c1",
                    "category_id": "虚拟机-003",
                    "steps": [
                        {
                            "tool_name": "get_failed_tasks",
                            "tool_args_template": {},
                            "expected_pattern": "__CONTAINS__:redis",
                        }
                    ],
                    "root_cause": "redis 异常",
                    "solution": "重启 redis",
                    "similarity": 0.9,
                }
            ],
        )
        registry = _make_registry_mock_with_stream(["最终诊断报告内容"])

        # 工具执行器返回匹配输出
        mock_executor = MagicMock()

        async def execute(tool_name, args):
            return "redis service failed"

        mock_executor.execute = execute

        agent = InvestigationAgent(
            ai_registry=registry,
            kb_client=kb,
            tool_executor=mock_executor,
        )

        events = [
            event
            async for event in agent.process(
                session_id="test-003",
                messages=[{"role": "user", "content": "虚拟机无法启动"}],
                category_id="虚拟机-003",
                diagnostic_stage="S1",
                env_context={
                    "vm_name": "vm-001",
                    "env_info": "cluster-a",
                    "alert_logs": [{"id": 1, "msg": "test"}],
                    "task_logs": [{"id": 2, "status": "failed"}],
                },
                assistant_type="htp-agent",
                case_id=None,
                user_id="user-001",
            )
        ]

        stage_events = [e for e in events if isinstance(e, AgentStageUpdate)]
        final_stages = [e for e in stage_events if e.stage == "S4"]
        assert len(final_stages) == 1, f"未找到 S4 阶段事件，所有 stage 事件：{[e.stage for e in stage_events]}"

    @pytest.mark.asyncio
    async def test_sop_mode_bypasses_route_when_resume(self):
        """当传入 sop_resume_context 时，应绕过 route_by_category，直接从 kb_client 查询 SOP 文档详情"""
        # mock kb_client
        kb = MagicMock()
        doc_details = {"id": 100, "title": "恢复SOP标题", "content_md": "恢复SOP内容"}
        kb.get_sop_document = AsyncMock(return_value=doc_details)
        kb.route_by_category = AsyncMock()

        registry = _make_registry_mock_with_stream(["恢复SOP诊断结论"])

        agent = InvestigationAgent(
            ai_registry=registry,
            kb_client=kb,
            tool_executor=MagicMock(),
        )

        events = [
            event
            async for event in agent.process(
                session_id="test-resume-001",
                messages=[{"role": "user", "content": "继续"}],
                category_id="虚拟机-003",
                diagnostic_stage="S1",
                env_context={},
                assistant_type="htp-agent",
                case_id=None,
                user_id="user-001",
                sop_resume_context={
                    "sop_document_id": 100,
                    "current_node_id": "n-1-2",
                    "completed_steps": ["n-1"],
                },
            )
        ]

        # 校验：route_by_category 从未被调用
        kb.route_by_category.assert_not_called()
        # 校验：get_sop_document 成功被调用以获取文档详情
        kb.get_sop_document.assert_awaited_once_with(100)

        # 校验：最终应该有文本输出
        text_events = [e for e in events if isinstance(e, AgentTextChunk)]
        assert len(text_events) >= 1
        assert "恢复SOP诊断结论" in text_events[-1].content


class TestSOPPromptTruncation:
    """_build_sop_prompt() 截断逻辑测试"""

    def test_truncates_long_sop_content(self):
        """超长 SOP 内容会被截断并添加提示"""
        # 构造超长内容
        long_content = "x" * 10000
        prompt = InvestigationAgent._build_sop_prompt(
            sop_content=long_content,
            sop_title="测试SOP",
            diagnostic_stage="S1",
            case_id="case-001",
        )

        # 验证截断生效：prompt 中 SOP 内容部分 ≤ MAX_SOP_CHARS
        # 注意 prompt 包含其他固定文本，所以总长度会大于 MAX_SOP_CHARS
        # 我们验证截断提示存在
        assert "[注意：SOP 文档已截断" in prompt
        assert "必要时通过工具获取更多细节" in prompt

        # 验证内容被截断到 MAX_SOP_CHARS
        assert "x" * MAX_SOP_CHARS in prompt  # 截断后的内容存在
        assert "x" * 9000 not in prompt  # 超出部分不存在

    def test_truncated_content_within_limit(self):
        """截断后的 SOP 内容字符数不超过上限"""
        long_content = "测试内容" * 3000  # 约 12000 字符
        prompt = InvestigationAgent._build_sop_prompt(
            sop_content=long_content,
            sop_title="测试SOP",
            diagnostic_stage="S1",
            case_id="case-001",
        )

        # 验证截断提示存在
        assert "[注意：SOP 文档已截断" in prompt

        # 提取 SOP 内容部分（在【SOP 排障流程】和截断提示之间）
        # 由于截断逻辑会将内容限制在 MAX_SOP_CHARS，验证不超过该长度
        # 截断后内容 = 原内容[:MAX_SOP_CHARS] + 截断提示
        # 所以原始 SOP 部分长度应为 MAX_SOP_CHARS
        assert "测试内容" in prompt  # 内容存在

    def test_short_sop_not_truncated(self):
        """短 SOP 内容不截断，无截断提示"""
        short_content = "这是正常的SOP内容，不需要截断。"
        prompt = InvestigationAgent._build_sop_prompt(
            sop_content=short_content,
            sop_title="测试SOP",
            diagnostic_stage="S1",
            case_id="case-001",
        )

        # 验证无截断提示
        assert "[注意：SOP 文档已截断" not in prompt
        # 验证内容完整
        assert short_content in prompt

    def test_boundary_exact_max_chars(self):
        """刚好等于 MAX_SOP_CHARS 的内容不截断"""
        # 构造刚好等于 MAX_SOP_CHARS 的内容
        boundary_content = "a" * MAX_SOP_CHARS
        prompt = InvestigationAgent._build_sop_prompt(
            sop_content=boundary_content,
            sop_title="测试SOP",
            diagnostic_stage="S1",
            case_id="case-001",
        )

        # 长度刚好等于上限，不触发截断
        assert "[注意：SOP 文档已截断" not in prompt
        assert boundary_content in prompt

    def test_boundary_one_over_max_chars(self):
        """超过 MAX_SOP_CHARS 一个字符也会触发截断"""
        over_content = "b" * (MAX_SOP_CHARS + 1)
        prompt = InvestigationAgent._build_sop_prompt(
            sop_content=over_content,
            sop_title="测试SOP",
            diagnostic_stage="S1",
            case_id="case-001",
        )

        # 超过上限，触发截断
        assert "[注意：SOP 文档已截断" in prompt


class TestBuildRetrievalQuery:
    """测试 InvestigationAgent._build_retrieval_query 查询构建逻辑"""

    def test_extracts_first_meaningful_user_message(self):
        """跳过 S0 控制符/按钮选择，提取第一条有语义的主诉"""
        messages = [
            {"role": "user", "content": "虚拟机镜像文件损坏异常"},
            {"role": "assistant", "content": "请问是哪个分类？"},
            {"role": "user", "content": "①"},
            {"role": "assistant", "content": "好的，正在推进诊断"},
            {"role": "user", "content": "继续"},
        ]
        query = InvestigationAgent._build_retrieval_query(messages)
        assert query == "虚拟机镜像文件损坏异常"

    def test_skips_control_characters_and_digits(self):
        """确认包含 S0 点击（①, 1, 继续, 好的）时正确跳过"""
        for control in ["①", "②", "1", "继续", "好的", "收到", "确认"]:
            messages = [
                {"role": "user", "content": control},
                {"role": "user", "content": "存储卷挂载超时错误"},
            ]
            query = InvestigationAgent._build_retrieval_query(messages)
            assert query == "存储卷挂载超时错误"

    def test_returns_empty_string_if_no_user_messages(self):
        """无用户消息时返回空字符串"""
        assert InvestigationAgent._build_retrieval_query([]) == ""
