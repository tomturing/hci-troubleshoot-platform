"""
KB Service SQLAlchemy 模型 — kbd_review_owner

发布审核责任人表，用于维护可分配的审核责任人列表。
"""

from __future__ import annotations

from datetime import datetime

from shared.database.postgres import Base
from sqlalchemy import BigInteger, Column, DateTime, String
from sqlalchemy.sql import func


class KbdReviewOwner(Base):
    """发布审核责任人模型

    字段说明：
    - id: 主键，自增
    - name: 责任人姓名（必填）
    - email: 责任人邮箱（可选，唯一）
    - created_at: 创建时间
    - updated_at: 最后更新时间
    """

    __tablename__ = "kbd_review_owner"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=True, unique=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
