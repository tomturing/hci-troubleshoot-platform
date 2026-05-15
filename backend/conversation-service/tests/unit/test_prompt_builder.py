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


class TestSegmentS0ContextInfo:
    """测试 _segment_s0_context_info 输出格式与字段"""

    def test_empty_context_returns_empty(self, builder: PromptBuilder) -> None:
        """空上下文返回空字符串"""
        result = builder._segment_s0_context_info({})
        assert result == ""

    def test_alert_fields_present(self, builder: PromptBuilder) -> None:
        """告警字段 target/type/host/description 均出现在输出中"""
        context = {
            "alert_logs": [
                {
                    "level": "CRITICAL",
                    "time": "2026-05-15 10:00:00",
                    "target": "vm-001",
                    "type": "CPU告警",
                    "host": "host-01",
                    "description": "CPU 使用率超阈值",
                }
            ],
            "task_logs": [],
        }
        result = builder._segment_s0_context_info(context)
        assert "CRITICAL" in result
        assert "vm-001" in result
        assert "CPU告警" in result
        assert "host-01" in result
        assert "CPU 使用率超阈值" in result

    def test_alert_optional_vm_field(self, builder: PromptBuilder) -> None:
        """告警含 vm 字段时出现在输出中"""
        context = {
            "alert_logs": [
                {
                    "level": "WARNING",
                    "target": "storage-01",
                    "type": "存储告警",
                    "host": "host-02",
                    "vm": "vm-abc",
                    "description": "存储延迟高",
                }
            ],
            "task_logs": [],
        }
        result = builder._segment_s0_context_info(context)
        assert "vm-abc" in result

    def test_task_fields_present(self, builder: PromptBuilder) -> None:
        """任务字段 type/host/target/errcode_tracing/trace_id 均出现在输出中"""
        context = {
            "alert_logs": [],
            "task_logs": [
                {
                    "status": "失败",
                    "type": "开机",
                    "time": "2026-05-15 09:00:00",
                    "host": "host-01",
                    "target": "vm-002",
                    "errcode_tracing": "ERR-1234",
                    "trace_id": "abc-xyz-001",
                    "description": "开机超时",
                }
            ],
        }
        result = builder._segment_s0_context_info(context)
        assert "失败" in result
        assert "开机" in result
        assert "host-01" in result
        assert "vm-002" in result
        assert "ERR-1234" in result
        assert "abc-xyz-001" in result
        assert "开机超时" in result

    def test_empty_alert_and_task_lists(self, builder: PromptBuilder) -> None:
        """alert_logs 和 task_logs 均为空列表时返回空字符串"""
        context = {"alert_logs": [], "task_logs": [], "env_info": {}}
        result = builder._segment_s0_context_info(context)
        assert result == ""

    def test_env_info_rendered(self, builder: PromptBuilder) -> None:
        """env_info 字段正确渲染到输出中"""
        context = {
            "env_info": {
                "hci_version": "6.5.0",
                "cluster_name": "cluster-dev",
                "host_count": 3,
            },
            "alert_logs": [],
            "task_logs": [],
        }
        result = builder._segment_s0_context_info(context)
        assert "6.5.0" in result
        assert "cluster-dev" in result

