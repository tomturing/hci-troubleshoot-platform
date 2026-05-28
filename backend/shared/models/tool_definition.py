"""
ToolDefinition ORM 模型 - 工具定义表

权威来源：docs/solution/agent/agent工具设计.md §五.3
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from ..database.postgres import Base


class ToolDefinitionORM(Base):
    """
    工具定义表（AI 工具知识库）

    每条记录 = 一个原子工具（acli_exec、bash_exec、get_active_alerts 等）
    tool_registry.py 启动时从该表加载所有激活工具。

    is_active=false 用于临时下线某工具（如 acli 版本升级期间），不影响会话恢复。
    """

    __tablename__ = "tool_definition"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_name = Column(String(100), nullable=False, unique=True)  # 工具唯一标识（如 acli_exec）
    display_name = Column(String(200), nullable=False)  # 展示名（如'执行 acli 命令'）
    tool_type = Column(String(20), nullable=False)  # acli / scp_api / sop
    category = Column(String(50), nullable=True, index=True)  # 执行路由: scp | acli | sop
    description = Column(Text, nullable=False)  # 工具功能描述（注入 Prompt 供 LLM 理解）
    usage_template = Column(Text, nullable=True)  # 调用模板（acli 插件工具使用）
    parameters_schema = Column(JSONB, nullable=False, default=dict)  # OpenAI function call schema
    examples = Column(JSONB, nullable=False, default=list)  # 调用示例数组
    risk_level = Column(SmallInteger, nullable=False, default=1)  # 1=只读 2=写操作 3=高危
    is_active = Column(Boolean, nullable=False, default=True)  # false=临时下线
    version = Column(String(20), nullable=False, default="1.0")
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
            f"<ToolDefinitionORM(id={self.id}, tool_name={self.tool_name!r}, "
            f"category={self.category!r}, risk_level={self.risk_level}, is_active={self.is_active})>"
        )
