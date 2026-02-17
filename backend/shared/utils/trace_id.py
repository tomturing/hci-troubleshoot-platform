"""
TraceID Generator and Utilities
TraceID 生成和工具函数
"""
import time
import random
import string
from typing import Optional


def generate_trace_id() -> str:
    """
    生成 TraceID
    
    格式: hci-{timestamp}-{random}
    示例: hci-1708012345-a1b2c3
    
    Returns:
        str: 生成的 TraceID
    """
    timestamp = int(time.time())
    random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"hci-{timestamp}-{random_str}"


def validate_trace_id(trace_id: str) -> bool:
    """
    验证 TraceID 格式
    
    Args:
        trace_id: 要验证的 TraceID
        
    Returns:
        bool: 是否有效
    """
    if not trace_id:
        return False
    
    parts = trace_id.split('-')
    if len(parts) != 3:
        return False
    
    if parts[0] != 'hci':
        return False
    
    # 验证 timestamp 是数字
    try:
        int(parts[1])
    except ValueError:
        return False
    
    # 验证 random 部分长度
    if len(parts[2]) != 6:
        return False
    
    return True


def extract_timestamp_from_trace_id(trace_id: str) -> Optional[int]:
    """
    从 TraceID 中提取时间戳
    
    Args:
        trace_id: TraceID
        
    Returns:
        Optional[int]: 时间戳，如果无效则返回 None
    """
    if not validate_trace_id(trace_id):
        return None
    
    parts = trace_id.split('-')
    try:
        return int(parts[1])
    except (ValueError, IndexError):
        return None


class TraceContext:
    """TraceID 上下文管理器"""
    
    def __init__(self):
        self._trace_id: Optional[str] = None
    
    def set_trace_id(self, trace_id: str):
        """设置当前 TraceID"""
        self._trace_id = trace_id
    
    def get_trace_id(self) -> Optional[str]:
        """获取当前 TraceID"""
        return self._trace_id
    
    def clear(self):
        """清除 TraceID"""
        self._trace_id = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.clear()


# 全局 TraceContext 实例
trace_context = TraceContext()
