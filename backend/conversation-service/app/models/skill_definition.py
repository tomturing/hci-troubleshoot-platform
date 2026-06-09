"""
SkillDefinition Model - 技能定义表（conversation-service 本地重导出）

从 shared.models.skill_definition 重导出 SkillDefinitionORM，
并提供 SkillDefinition 别名，避免在同一 SQLAlchemy Base 中重复注册
'skill_definition' 表而导致的 InvalidRequestError。

详细字段定义见：backend/shared/models/skill_definition.py
"""

from shared.models.skill_definition import SkillDefinitionORM

# 提供与原来相同的短别名，避免改动路由和测试中的导入
SkillDefinition = SkillDefinitionORM

__all__ = ["SkillDefinition", "SkillDefinitionORM"]
