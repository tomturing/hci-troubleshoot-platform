"""
Authorization Model - 人工授权审计模型
从 shared 层重导出，避免重复注册 SQLAlchemy 表冲突。
"""

from shared.models.audit import Authorization  # noqa: F401

__all__ = ["Authorization"]
