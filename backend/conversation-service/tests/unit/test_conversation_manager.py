"""
ConversationManager 单元测试

测试 detect_stage_transition() 的各种场景：
- 正常阶段推进（S0→S1, S1→S2, ...）
- 用户重置请求（→S0）
- 边界情况（最后阶段、非法阶段）
- 无触发词时不转换
"""

import pytest

from app.services.conversation_manager import ConversationManager


@pytest.fixture
def manager() -> ConversationManager:
    """创建一个无状态的 ConversationManager 实例"""
    return ConversationManager()


class TestStageTransition:
    """正常阶段推进测试"""

    def test_s0_to_s1_on_fault_confirmed(self, manager: ConversationManager):
        """S0→S1：AI 回复确认故障方向时触发"""
        reply = "根据您的描述，确认您的故障是虚拟机开机失败，定位到存储方向。"
        result = manager.detect_stage_transition("S0", reply, "我的虚拟机开机失败")
        assert result == "S1"

    def test_s1_to_s2_on_root_cause_analysis(self, manager: ConversationManager):
        """S1→S2：AI 开始生成根因假设时触发"""
        reply = "故障分类已确定为虚拟机开机失败，开始分析可能的根因假设："
        result = manager.detect_stage_transition("S1", reply, "是的")
        assert result == "S2"

    def test_s1_to_s2_on_hypothesis_keyword(self, manager: ConversationManager):
        """S1→S2：AI 提到根因假设词时触发"""
        reply = "根据我的判断，以下是几个根因假设：1. 存储不可访问 2. 宿主资源不足"
        result = manager.detect_stage_transition("S1", reply, "")
        assert result == "S2"

    def test_s2_to_s3_on_diagnostic_start(self, manager: ConversationManager):
        """S2→S3：AI 开始执行诊断命令时触发"""
        reply = "现在开始执行诊断，请运行以下命令检查存储状态：acli storage get"
        result = manager.detect_stage_transition("S2", reply, "")
        assert result == "S3"

    def test_s3_to_s4_on_evidence_collected(self, manager: ConversationManager):
        """S3→S4：AI 有足够证据时触发"""
        reply = "根据以上结果，存储挂载失败是确定的，诊断结果：磁盘路径不可达"
        result = manager.detect_stage_transition("S3", reply, "")
        assert result == "S4"

    def test_s4_to_s5_on_root_cause_confirmed(self, manager: ConversationManager):
        """S4→S5：AI 使用"根因确认："格式时触发"""
        reply = "根因确认：存储节点 node-01 的磁盘出现 I/O 错误，导致虚拟机无法挂载根磁盘。"
        result = manager.detect_stage_transition("S4", reply, "")
        assert result == "S5"

    def test_s5_to_s6_on_solution_output(self, manager: ConversationManager):
        """S5→S6：AI 要求用户执行并反馈时触发"""
        reply = "请按上述步骤操作，执行以上步骤后请告知我是否成功解决问题。"
        result = manager.detect_stage_transition("S5", reply, "")
        assert result == "S6"


class TestNoTransition:
    """无触发词时不应转换"""

    def test_no_transition_on_plain_answer(self, manager: ConversationManager):
        """普通回答不触发任何阶段转换"""
        reply = "您好，请问虚拟机的具体名称是什么？"
        result = manager.detect_stage_transition("S0", reply, "虚拟机开机失败")
        assert result is None

    def test_no_transition_on_last_stage(self, manager: ConversationManager):
        """已到 S6（最后阶段），不再触发推进"""
        reply = "根因确认：问题已解决，诊断结束。"
        result = manager.detect_stage_transition("S6", reply, "")
        assert result is None

    def test_no_transition_skipping_stage(self, manager: ConversationManager):
        """只推进一个阶段，不能跨阶段跳跃"""
        # S0 不会因为 S4 的触发词而跳到 S5
        reply = "根因确认：存储 I/O 错误"
        result = manager.detect_stage_transition("S0", reply, "")
        # S0→S1 的触发词是"确认是XXX故障定位"，此处不含，应为 None
        assert result is None


class TestReset:
    """用户重置测试"""

    def test_reset_from_s3(self, manager: ConversationManager):
        """用户说"重新来"时，从任何阶段重置到 S0"""
        result = manager.detect_stage_transition("S3", "好的，我们重新开始。", "重新来")
        assert result == "S0"

    def test_reset_not_triggered_on_s0(self, manager: ConversationManager):
        """已在 S0，重置请求不触发（避免返回 S0→S0 的无意义转换）"""
        result = manager.detect_stage_transition("S0", "好的，重新描述一下。", "重新来")
        assert result is None

    def test_reset_keyword_variants(self, manager: ConversationManager):
        """多种重置关键词都能触发"""
        for kw in ["换个问题", "另一个问题", "重新开始"]:
            result = manager.detect_stage_transition("S2", "好的。", kw)
            assert result == "S0", f"关键词 '{kw}' 未触发重置"


class TestEdgeCases:
    """边界情况"""

    def test_invalid_stage_returns_none(self, manager: ConversationManager):
        """传入非法阶段，返回 None"""
        result = manager.detect_stage_transition("S99", "根因确认：xxx", "")
        assert result is None

    def test_empty_reply(self, manager: ConversationManager):
        """空回复不触发转换"""
        result = manager.detect_stage_transition("S0", "", "测试")
        assert result is None

    def test_get_stage_label(self, manager: ConversationManager):
        """get_stage_label 返回正确的中文标签"""
        assert manager.get_stage_label("S0") == "S0-意图识别"
        assert manager.get_stage_label("S3") == "S3-验证执行"
        assert manager.get_stage_label("S99") == "S99"  # 未知阶段返回原值
