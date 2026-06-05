"""
SkillDefinition ORM 模型 - 技能定义表

遵循 Agent Skills Open Standard (https://agentskills.io)
Skill 的本质是"过程性知识包"，而非函数接口（Tool 才有 parameters_schema / output_schema）。

字段映射关系（ORM ↔ SKILL.md frontmatter）：
  skill_name      ←→ name（kebab-case 唯一标识）
  description     ←→ description（供 Agent 发现阶段，~100 tokens）
  instructions_md ←→ SKILL.md 正文（供 Agent 激活阶段，< 5000 tokens）
  compatibility   ←→ compatibility（可选，环境依赖说明）
  license         ←→ license（可选）
  allowed_tools   ←→ allowed-tools（可选，实验性）
  metadata_json   ←→ metadata（可选，key-value 扩展）

平台扩展字段（超出标准规范）：
  display_name    — 中文展示名，管理控制台使用
  is_active       — 启用开关
  assets_json     — 资源文件内联（模拟标准 assets/ 目录）
  references_json — 参考文档内联（模拟标准 references/ 目录）
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from ..database.postgres import Base


class SkillDefinitionORM(Base):
    """
    技能定义表（Agent Skills Open Standard 合规版本）

    每条记录 = 一个 Skill（知识包），而非函数调用接口。
    Agent 通过 渐进式加载（Progressive Disclosure）机制使用：
      发现阶段：只读 skill_name + description
      激活阶段：读取 instructions_md 全文
      执行阶段：按需读取 assets_json / references_json 中的资源
    """

    __tablename__ = "skill_definition"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ===== 标准规范字段 =====

    # 对应标准 name 字段：kebab-case，1-64字符，小写字母+数字+连字符
    skill_name = Column(String(64), nullable=False, unique=True)

    # 对应标准 description 字段：描述"做什么"和"何时触发"，供 Agent 发现阶段使用
    description = Column(String(1024), nullable=False)

    # 对应 SKILL.md 正文：Step-by-step 指令 + Gotchas + 示例 + 输出模板
    instructions_md = Column(Text, nullable=False, default="")

    # 对应标准 compatibility 字段（可选）：环境依赖说明
    compatibility = Column(String(500), nullable=True)

    # 对应标准 license 字段（可选）
    license = Column(String(100), nullable=True)

    # 对应标准 allowed-tools 字段（可选，实验性）：空格分隔的预批准工具列表
    allowed_tools = Column(Text, nullable=True)

    # 对应标准 metadata 字段：任意 key-value 扩展元数据（含 author / category / tags）
    metadata_json = Column(JSONB, nullable=False, default=dict)

    # ===== 平台扩展字段 =====

    # 中文展示名，管理控制台使用（非标准字段）
    display_name = Column(String(200), nullable=True)

    # 启用开关（非标准字段）
    is_active = Column(Boolean, nullable=False, default=True)

    # 资源文件内联存储，模拟标准 assets/ 目录（非标准字段）
    # 格式：[{"filename": "template.md", "type": "template", "content": "..."}]
    assets_json = Column(JSONB, nullable=False, default=list)

    # 参考文档内联存储，模拟标准 references/ 目录（非标准字段）
    # 格式：[{"filename": "REFERENCE.md", "title": "...", "content": "..."}]
    references_json = Column(JSONB, nullable=False, default=list)

    trace_id = Column(String(64), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<SkillDefinitionORM(id={self.id}, skill_name={self.skill_name!r}, "
            f"display_name={self.display_name!r}, is_active={self.is_active})>"
        )
