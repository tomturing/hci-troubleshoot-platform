"""
SkillDefinition Model - 技能定义表
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class SkillDefinition(Base):
    """技能定义表"""

    __tablename__ = "skill_definition"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_name = Column(String(100), nullable=False, unique=True)  # 技能唯一标识（如 disk_vendor_lifetime）
    display_name = Column(String(200), nullable=False)  # 展示名（如'硬盘厂商识别与寿命判定'）
    description = Column(Text, nullable=False)  # 技能描述
    parameters_schema = Column(JSONB, nullable=False, default=dict)  # 输入参数 Schema
    output_schema = Column(JSONB, nullable=False, default=dict)  # 输出参数 Schema
    is_active = Column(Boolean, nullable=False, default=True)  # 是否启用
    version = Column(String(20), nullable=False, default="1.0")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self):
        return (
            f"<SkillDefinition(id={self.id}, skill_name={self.skill_name!r}, "
            f"display_name={self.display_name!r}, is_active={self.is_active})>"
        )
