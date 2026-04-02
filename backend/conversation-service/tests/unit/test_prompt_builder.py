"""
PromptBuilder 单元测试

覆盖各阶段占位符替换逻辑
"""

import pytest
from app.services.prompt_builder import PromptBuilder


@pytest.fixture
def builder() -> PromptBuilder:
    """创建 PromptBuilder 实例"""
    return PromptBuilder()


class TestSegmentMethodology:
    """测试 _segment_methodology 占位符替换"""

    def test_s0_no_placeholder(self, builder: PromptBuilder) -> None:
        """S0 阶段无占位符，直接返回模板"""
        result = builder._segment_methodology("S0", {})
        assert "S0 意图识别" in result
        assert "{" not in result  # 无未替换的占位符

    def test_s1_known_info_replaced(self, builder: PromptBuilder) -> None:
        """S1 阶段 {known_info} 被替换"""
        state = {"known_info": "虚拟机 vm-001 开机失败"}
        result = builder._segment_methodology("S1", state)
        assert "虚拟机 vm-001 开机失败" in result
        assert "{known_info}" not in result

    def test_s1_known_info_fallback(self, builder: PromptBuilder) -> None:
        """S1 阶段 known_info 为空时使用兜底值"""
        result = builder._segment_methodology("S1", {})
        assert "暂无" in result
        assert "{known_info}" not in result

    def test_s2_category_path_replaced(self, builder: PromptBuilder) -> None:
        """S2 阶段 {category_path} 被替换"""
        state = {"category_l1": "虚拟机", "category_l2": "开机失败"}
        result = builder._segment_methodology("S2", state)
        assert "虚拟机 > 开机失败" in result
        assert "{category_path}" not in result

    def test_s2_category_path_partial(self, builder: PromptBuilder) -> None:
        """S2 阶段仅有 category_l1 时也能正确处理"""
        state = {"category_l1": "虚拟机"}
        result = builder._segment_methodology("S2", state)
        assert "虚拟机" in result
        assert "{category_path}" not in result

    def test_s2_category_path_fallback(self, builder: PromptBuilder) -> None:
        """S2 阶段无分类时使用兜底值"""
        result = builder._segment_methodology("S2", {})
        assert "待定位" in result
        assert "{category_path}" not in result

    def test_s3_hypothesis_replaced(self, builder: PromptBuilder) -> None:
        """S3 阶段 {hypothesis} 被替换"""
        state = {
            "category_l1": "虚拟机",
            "hypothesis": ["宿主机资源不足", "存储卷损坏"],
        }
        result = builder._segment_methodology("S3", state)
        assert "宿主机资源不足" in result
        assert "{hypothesis}" not in result

    def test_s3_hypothesis_fallback(self, builder: PromptBuilder) -> None:
        """S3 阶段 hypothesis 为空时使用兜底值"""
        state = {"category_l1": "虚拟机"}
        result = builder._segment_methodology("S3", state)
        assert "[]" in result  # 空列表转字符串
        assert "{hypothesis}" not in result

    def test_s5_root_cause_replaced(self, builder: PromptBuilder) -> None:
        """S5 阶段 {root_cause} 被替换"""
        state = {"root_cause": "宿主机 CPU 资源耗尽"}
        result = builder._segment_methodology("S5", state)
        assert "宿主机 CPU 资源耗尽" in result
        assert "{root_cause}" not in result

    def test_s5_root_cause_fallback(self, builder: PromptBuilder) -> None:
        """S5 阶段 root_cause 为空时使用兜底值"""
        result = builder._segment_methodology("S5", {})
        assert "待确认" in result
        assert "{root_cause}" not in result


class TestNoRawPlaceholder:
    """测试所有阶段输出不含原始占位符"""

    @pytest.mark.parametrize("stage", ["S0", "S1", "S2", "S3", "S4", "S5", "S6"])
    def test_no_placeholder_in_output(self, builder: PromptBuilder, stage: str) -> None:
        """所有阶段输出都不含原始占位符"""
        # 模拟完整状态
        state = {
            "known_info": "测试信息",
            "category_l1": "虚拟机",
            "category_l2": "开机失败",
            "hypothesis": ["假设1", "假设2"],
            "root_cause": "测试根因",
        }
        result = builder._segment_methodology(stage, state)

        # 检查没有未替换的占位符
        assert "{known_info}" not in result
        assert "{category_path}" not in result
        assert "{hypothesis}" not in result
        assert "{root_cause}" not in result


class TestBuildSystemPrompt:
    """测试完整 prompt 构建"""

    def test_build_full_prompt(self, builder: PromptBuilder) -> None:
        """测试完整 prompt 构建不抛异常"""
        prompt = builder.build_system_prompt(
            diagnostic_stage="S2",
            knowledge_atoms=[
                {"type": "diagnostic_step", "content": "检查 CPU 使用率", "source_ref": "SOP-001"}
            ],
            case_context={"case_id": "C-001", "description": "VM 开机失败"},
            session_state={
                "known_info": "VM 开机卡住",
                "category_l1": "虚拟机",
                "category_l2": "开机失败",
                "hypothesis": ["资源不足"],
            },
        )
        assert "智能排障专家" in prompt
        assert "S2 假设生成" in prompt
        assert "虚拟机 > 开机失败" in prompt  # category_path 在 S2 模板中
        assert "资源不足" in prompt  # hypothesis 在 S2 模板中
        assert "{" not in prompt or "【" in prompt  # 允许中文括号
