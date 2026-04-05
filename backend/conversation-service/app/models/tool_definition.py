"""
ToolDefinition Model - 工具定义表（AI 工具知识库）

解决"AI 不知道如何调用工具"的根本问题：
  - 每条记录 = 一个原子工具（acli vm list 和 acli vm.start 各占一条）
  - Prompt 构建时 SELECT * FROM tool_definition WHERE is_active=true AND category='{当前故障域}'
  - 格式化后追加到 System Instructions
  - 新增工具时只需 INSERT，无需改代码

is_active=false 用于临时下线某工具（如 acli 版本升级期间），不影响会话恢复。
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Boolean, Column, DateTime, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class ToolDefinition(Base):
    """工具定义表"""

    __tablename__ = "tool_definition"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_name = Column(String(100), nullable=False, unique=True)          # 工具唯一标识（如 acli_vm_list）
    display_name = Column(String(200), nullable=False)                    # 展示名（如'获取虚拟机列表'）
    tool_type = Column(String(20), nullable=False)                        # acli / scp_api
    category = Column(String(50), nullable=True, index=True)             # 故障域: vm/storage/network/cluster/platform; NULL=通用
    description = Column(Text, nullable=False)                            # 工具功能描述（直接注入 Prompt 供 LLM 理解）
    usage_template = Column(Text, nullable=True)                          # 调用模板
    parameters_schema = Column(JSONB, nullable=False, default=dict)      # OpenAPI 3.0 格式参数 Schema
    examples = Column(JSONB, nullable=False, default=list)               # 调用示例数组
    risk_level = Column(SmallInteger, nullable=False, default=1)          # 1=只读 2=写操作 3=高危
    is_active = Column(Boolean, nullable=False, default=True)             # false=临时下线，不注入 Prompt
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
            f"<ToolDefinition(id={self.id}, tool_name={self.tool_name!r}, "
            f"category={self.category!r}, risk_level={self.risk_level}, is_active={self.is_active})>"
        )
