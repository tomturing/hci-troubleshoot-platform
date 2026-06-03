"""
PromptAuditService 单元测试

验证 ORM 元数据正确编译且元数据中没有任何外键编译冲突（NoReferencedTableError）。
"""

from app.services.prompt_audit import PromptAuditService  # noqa: F401
from shared.database.postgres import Base
from shared.models.audit import AuditLog  # noqa: F401
from sqlalchemy.orm import configure_mappers


def test_sqlalchemy_metadata_compiles_successfully():
    """验证 SQLAlchemy 所有表元数据（包括 AuditLog 外键）能成功编译。

    此测试能保证不会再次发生 NoReferencedTableError 的编译期异常。
    """
    # 验证 Base.metadata 中有注册 system_prompt 和 audit_log 表
    assert "system_prompt" in Base.metadata.tables
    assert "audit_log" in Base.metadata.tables

    # configure_mappers() 会对所有定义的 SQLAlchemy ORM 模型进行映射编译。
    # 如果有任何外键指向未注册的表（如 system_prompt），在此步骤会直接抛出 NoReferencedTableError 异常。
    configure_mappers()

