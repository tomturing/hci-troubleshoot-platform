"""
基础数据模型
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, String
from sqlalchemy.ext.declarative import declared_attr

class TimestampMixin:
    """时间戳混入类"""
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class TraceableMixin:
    """可追踪混入类"""
    
    trace_id = Column(String(50), index=True, nullable=True)
