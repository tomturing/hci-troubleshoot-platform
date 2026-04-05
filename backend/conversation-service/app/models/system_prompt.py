"""
SystemPrompt Model - System Instructions 模板表

支持 S0-S6 各诊断阶段的 Prompt 版本管理和 A/B 测试。
每个阶段可维护多个版本，is_active=true 的版本被激活。

audit_log.system_prompt_id 记录每次使用的模板版本，
用于效果追踪和快速回滚（发现问题时直接 UPDATE is_active=false）。
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text


class SystemPrompt(Base):
    """System Instructions 模板表"""

    __tablename__ = "system_prompt"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stage = Column(String(5), nullable=False, index=True)              # S0/S1/S2/S3/S4/S5/S6/BASE
    name = Column(String(100), nullable=False, unique=True)            # 唯一名称，如 s0_intent_recognition_v2
    description = Column(Text, nullable=True)                          # 模板说明：用途、设计思路
    content_template = Column(Text, nullable=False)                    # Prompt 模板，使用 {placeholder} 占位符
    version = Column(String(20), nullable=False, default="1.0")
    is_active = Column(Boolean, nullable=False, default=True)          # true=当前激活版本；同 stage 只能有一个 true
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self):
        return (
            f"<SystemPrompt(id={self.id}, stage={self.stage!r}, "
            f"name={self.name!r}, version={self.version!r}, is_active={self.is_active})>"
        )
