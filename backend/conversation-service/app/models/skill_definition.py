"""
SkillDefinition Model - 技能定义表（conversation-service 本地 ORM）

遵循 Agent Skills Open Standard (https://agentskills.io)
详细说明见 backend/shared/models/skill_definition.py
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class SkillDefinition(Base):
    """技能定义表（Agent Skills Open Standard 合规版本）"""

    __tablename__ = "skill_definition"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ===== 标准规范字段 =====
    skill_name = Column(String(64), nullable=False, unique=True)          # 对应标准 name 字段
    description = Column(String(1024), nullable=False)                     # 发现阶段使用
    instructions_md = Column(Text, nullable=False, default="")             # SKILL.md 正文
    compatibility = Column(String(500), nullable=True)                     # 环境依赖说明
    license = Column(String(100), nullable=True)                           # 许可证
    allowed_tools = Column(Text, nullable=True)                            # 预批准工具列表
    metadata_json = Column(JSONB, nullable=False, default=dict)            # 扩展元数据

    # ===== 平台扩展字段 =====
    display_name = Column(String(200), nullable=True)                      # 中文展示名
    is_active = Column(Boolean, nullable=False, default=True)              # 启用开关
    assets_json = Column(JSONB, nullable=False, default=list)              # 资源文件内联
    references_json = Column(JSONB, nullable=False, default=list)          # 参考文档内联
    trace_id = Column(String(64), nullable=True)

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
