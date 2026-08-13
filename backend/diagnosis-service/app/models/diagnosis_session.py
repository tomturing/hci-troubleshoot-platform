"""诊断会话 ORM 模型。"""

import uuid

from shared.database.postgres import Base
from shared.models.base import TimestampMixin
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.domain.session_state import DiagnosisSessionStatus

SESSION_STATUS_DB_ENUM = SQLEnum(
    DiagnosisSessionStatus, name="diagnosis_session_status", values_callable=lambda e: [x.value for x in e]
)


class DiagnosisSession(Base, TimestampMixin):
    """一次离线诊断的会话根实体。"""

    __tablename__ = "diagnosis_session"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_diagnosis_session_tenant_idempotency",
        ),
        CheckConstraint("version >= 1", name="ck_diagnosis_session_version"),
        CheckConstraint("supplement_count BETWEEN 0 AND 1", name="ck_diagnosis_session_supplement_count"),
        CheckConstraint(
            "incident_end_time >= incident_start_time",
            name="ck_diagnosis_session_incident_window",
        ),
    )

    session_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # case 表归 case-service（工单服务）所有，不在本服务 ORM 元数据中重复注册。
    # use_alter 避免 SQLAlchemy 刷新本服务实体时尝试解析跨服务 Mapper 依赖。
    case_id = Column(
        String(20),
        ForeignKey(
            "case.case_id",
            name="fk_diagnosis_session_case_id",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        nullable=False,
    )
    tenant_id = Column(String(128), nullable=False)
    created_by = Column(String(128), nullable=False)
    assigned_to = Column(String(128), nullable=True)
    product_line = Column(String(32), nullable=False, default="HCI")
    selected_scenario = Column(String(100), nullable=False)
    selected_category = Column(String(100), nullable=True)
    resolved_category = Column(String(100), nullable=True)
    incident_start_time = Column(DateTime(timezone=True), nullable=False)
    incident_end_time = Column(DateTime(timezone=True), nullable=False)
    incident_timezone = Column(String(64), nullable=False)
    affected_objects = Column(JSONB, nullable=False, default=list)
    impact_scope = Column(String(64), nullable=False)
    incident_status = Column(String(32), nullable=False)
    recent_change_description = Column(Text, nullable=True)
    experimental = Column(Boolean, nullable=False, default=False)
    status = Column(SESSION_STATUS_DB_ENUM, nullable=False, default=DiagnosisSessionStatus.CREATED)
    resume_status = Column(SESSION_STATUS_DB_ENUM, nullable=True)
    supplement_count = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    failure_code = Column(String(64), nullable=True)
    failure_message = Column(Text, nullable=True)
    trace_id = Column(String(64), nullable=False)

    def __repr__(self) -> str:
        return f"<DiagnosisSession(session_id={self.session_id}, status={self.status})>"
