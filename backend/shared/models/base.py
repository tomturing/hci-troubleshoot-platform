"""
基础数据模型
"""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, String


class TimestampMixin:
    """时间戳混入类"""

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False
    )


class TraceableMixin:
    """可追踪混入类"""

    trace_id = Column(String(64), index=True, nullable=True)
