"""
DiagnosticState Pydantic 模型单元测试
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


from app.models.diagnostic_state import DiagnosticSession, StageTransition


class TestStageTransition:
    """StageTransition 测试用例"""

    def test_create_transition(self):
        """测试创建阶段转换"""
        transition = StageTransition(
            from_stage="S0",
            to_stage="S1",
            triggered_by="llm_output",
        )

        assert transition.from_stage == "S0"
        assert transition.to_stage == "S1"
        assert transition.triggered_by == "llm_output"
        assert transition.confidence == 1.0

    def test_default_confidence(self):
        """测试默认置信度为 1.0"""
        transition = StageTransition(
            from_stage="S1",
            to_stage="S2",
            triggered_by="tool_result",
        )

        assert transition.confidence == 1.0

    def test_custom_confidence(self):
        """测试自定义置信度"""
        transition = StageTransition(
            from_stage="S2",
            to_stage="S3",
            triggered_by="user_input",
            confidence=0.85,
        )

        assert transition.confidence == 0.85

    def test_serialization(self):
        """测试序列化"""
        transition = StageTransition(
            from_stage="S0",
            to_stage="S1",
            triggered_by="llm_output",
            confidence=0.9,
        )

        data = transition.model_dump()
        assert data["from_stage"] == "S0"
        assert data["to_stage"] == "S1"
        assert data["triggered_by"] == "llm_output"
        assert data["confidence"] == 0.9

    def test_deserialization(self):
        """测试反序列化"""
        data = {
            "from_stage": "S1",
            "to_stage": "S2",
            "triggered_by": "auto",
            "confidence": 0.75,
        }

        transition = StageTransition(**data)
        assert transition.from_stage == "S1"
        assert transition.to_stage == "S2"
        assert transition.triggered_by == "auto"
        assert transition.confidence == 0.75


class TestDiagnosticSession:
    """DiagnosticSession 测试用例"""

    def test_create_session(self):
        """测试创建诊断会话"""
        session = DiagnosticSession(
            conversation_id="conv-001",
            case_id="case-001",
        )

        assert session.conversation_id == "conv-001"
        assert session.case_id == "case-001"
        assert session.current_stage == "S0"
        assert session.stage_history == []
        assert session.transitions == []
        assert session.hypotheses == []
        assert session.confirmed_facts == []

    def test_default_values(self):
        """测试默认值"""
        session = DiagnosticSession(conversation_id="conv-001")

        assert session.case_id == ""
        assert session.current_stage == "S0"
        assert session.stage_history == []
        assert session.hypotheses == []
        assert session.confirmed_facts == []
        assert session.root_cause is None
        assert session.solution is None
        assert session.metadata == {}

    def test_advance_to(self):
        """测试阶段推进"""
        session = DiagnosticSession(conversation_id="conv-001")
        transition = session.advance_to("S1")

        assert session.current_stage == "S1"
        assert session.stage_history == ["S0"]
        assert len(session.transitions) == 1
        assert transition.from_stage == "S0"
        assert transition.to_stage == "S1"
        assert transition.triggered_by == "llm_output"

    def test_advance_to_custom_trigger(self):
        """测试自定义触发原因"""
        session = DiagnosticSession(conversation_id="conv-001")
        transition = session.advance_to("S1", triggered_by="tool_result")

        assert transition.triggered_by == "tool_result"

    def test_multiple_advances(self):
        """测试多次推进"""
        session = DiagnosticSession(conversation_id="conv-001")
        session.advance_to("S1")
        session.advance_to("S2")
        session.advance_to("S3")

        assert session.current_stage == "S3"
        assert session.stage_history == ["S0", "S1", "S2"]
        assert len(session.transitions) == 3

    def test_add_hypothesis(self):
        """测试添加假设"""
        session = DiagnosticSession(conversation_id="conv-001")

        session.add_hypothesis("可能是网络问题")
        session.add_hypothesis("也可能是存储问题")
        session.add_hypothesis("可能是网络问题")  # 重复不应添加

        assert len(session.hypotheses) == 2
        assert "可能是网络问题" in session.hypotheses

    def test_confirm_fact(self):
        """测试确认事实"""
        session = DiagnosticSession(conversation_id="conv-001")

        session.confirm_fact("VM 启动失败")
        session.confirm_fact("错误码: 1001")
        session.confirm_fact("VM 启动失败")  # 重复不应添加

        assert len(session.confirmed_facts) == 2

    def test_pending_questions(self):
        """测试待确认问题"""
        session = DiagnosticSession(conversation_id="conv-001")

        session.add_pending_question("VM 的 ID 是？")
        session.add_pending_question("故障发生时间？")

        assert len(session.pending_questions) == 2

        session.clear_pending_questions()
        assert len(session.pending_questions) == 0

    def test_to_context_dict(self):
        """测试转换为上下文字典"""
        session = DiagnosticSession(
            conversation_id="conv-001",
            case_id="case-001",
        )
        session.advance_to("S2")
        session.add_hypothesis("假设1")
        session.confirm_fact("事实1")

        ctx = session.to_context_dict()

        assert ctx["current_stage"] == "S2"
        assert ctx["stage_history"] == ["S0"]
        assert ctx["hypotheses"] == ["假设1"]
        assert ctx["confirmed_facts"] == ["事实1"]

    def test_serialization_roundtrip(self):
        """测试序列化往返"""
        session = DiagnosticSession(
            conversation_id="conv-001",
            case_id="case-001",
            current_stage="S3",
        )
        session.add_hypothesis("假设 A")
        session.confirm_fact("事实 B")

        # 序列化
        data = session.model_dump()

        # 反序列化
        restored = DiagnosticSession(**data)

        assert restored.conversation_id == session.conversation_id
        assert restored.case_id == session.case_id
        assert restored.current_stage == session.current_stage
        assert restored.hypotheses == session.hypotheses
        assert restored.confirmed_facts == session.confirmed_facts


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
