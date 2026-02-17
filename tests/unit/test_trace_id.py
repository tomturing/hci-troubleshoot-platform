"""
TraceID工具单元测试
"""

import pytest
import time
from backend.shared.utils.trace_id import (
    generate_trace_id,
    validate_trace_id,
    extract_timestamp
)


class TestTraceIDGeneration:
    """TraceID生成测试"""
    
    def test_generate_trace_id_format(self):
        """测试生成的TraceID格式正确"""
        trace_id = generate_trace_id()
        
        # 检查格式: hci-{timestamp}-{random}
        assert trace_id.startswith("hci-")
        parts = trace_id.split("-")
        assert len(parts) == 3
        assert parts[0] == "hci"
        
        # 检查timestamp是数字
        assert parts[1].isdigit()
        
        # 检查random部分长度为6
        assert len(parts[2]) == 6
        assert parts[2].isalnum()
    
    def test_generate_trace_id_unique(self):
        """测试生成的TraceID唯一性"""
        trace_ids = [generate_trace_id() for _ in range(100)]
        assert len(set(trace_ids)) == 100
    
    def test_generate_trace_id_timestamp_recent(self):
        """测试生成的TraceID包含最近的时间戳"""
        trace_id = generate_trace_id()
        timestamp = extract_timestamp(trace_id)
        
        current_time = int(time.time())
        # 时间戳应该在当前时间的±2秒内
        assert abs(current_time - timestamp) < 2


class TestTraceIDValidation:
    """TraceID验证测试"""
    
    def test_validate_valid_trace_id(self):
        """测试验证有效的TraceID"""
        trace_id = generate_trace_id()
        assert validate_trace_id(trace_id) is True
    
    def test_validate_invalid_format(self):
        """测试验证无效格式的TraceID"""
        invalid_ids = [
            "",
            "invalid",
            "hci-123",
            "hci-abc-xyz",
            "abc-123-xyz",
            "hci-123456789-toolong",
            "hci-123456789-xyz!",
        ]
        
        for trace_id in invalid_ids:
            assert validate_trace_id(trace_id) is False
    
    def test_validate_none(self):
        """测试验证None"""
        assert validate_trace_id(None) is False
    
    def test_validate_empty_string(self):
        """测试验证空字符串"""
        assert validate_trace_id("") is False


class TestTimestampExtraction:
    """时间戳提取测试"""
    
    def test_extract_timestamp_valid(self):
        """测试从有效TraceID提取时间戳"""
        trace_id = "hci-1708012345-abc123"
        timestamp = extract_timestamp(trace_id)
        assert timestamp == 1708012345
    
    def test_extract_timestamp_invalid(self):
        """测试从无效TraceID提取时间戳"""
        invalid_ids = [
            "invalid",
            "hci-abc-xyz",
            "",
            None
        ]
        
        for trace_id in invalid_ids:
            assert extract_timestamp(trace_id) is None
    
    def test_extract_timestamp_generated(self):
        """测试从生成的TraceID提取时间戳"""
        before = int(time.time())
        trace_id = generate_trace_id()
        after = int(time.time())
        
        timestamp = extract_timestamp(trace_id)
        assert before <= timestamp <= after


class TestTraceIDEdgeCases:
    """边界情况测试"""
    
    def test_trace_id_with_special_chars(self):
        """测试包含特殊字符的TraceID"""
        invalid_ids = [
            "hci-123456789-abc!@#",
            "hci-123456789-ABC",  # 大写字母
            "hci-123456789-abc def",  # 空格
        ]
        
        for trace_id in invalid_ids:
            assert validate_trace_id(trace_id) is False
    
    def test_trace_id_length_variations(self):
        """测试不同长度的TraceID"""
        # random部分必须是6位
        assert validate_trace_id("hci-123456789-abc12") is False  # 5位
        assert validate_trace_id("hci-123456789-abc123") is True   # 6位
        assert validate_trace_id("hci-123456789-abc1234") is False # 7位
