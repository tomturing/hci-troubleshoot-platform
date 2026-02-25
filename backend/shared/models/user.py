"""
User Database Model
"""
from sqlalchemy import Column, String, JSON, DateTime
from sqlalchemy.dialects.postgresql import UUID
import uuid

from ..database.postgres import Base
from .base import TimestampMixin, TraceableMixin
from datetime import datetime

class User(Base, TimestampMixin, TraceableMixin):
    """User Model"""
    __tablename__ = "user"

    user_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(100))
    email = Column(String(255))
    user_type = Column(String(20), default="temporary", nullable=False)
    metadata_ = Column("metadata", JSON, default={})  # metadata is reserved word in SQLAlchemy sometimes, safe to map
    last_login_at = Column(DateTime(timezone=True))

    def __repr__(self):
        return f"<User(user_id={self.user_id}, client_id={self.client_id})>"
