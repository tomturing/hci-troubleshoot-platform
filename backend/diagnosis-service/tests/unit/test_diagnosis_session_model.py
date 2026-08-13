"""诊断会话 ORM 与声明式 Schema 契约测试。"""

from pathlib import Path

from app.domain.session_state import DiagnosisSessionStatus
from app.models.collection_plan import CollectionPlan, CollectionPlanItem
from app.models.collector_artifact import CollectorArtifact, CollectorArtifactItem
from app.models.collector_definition import CollectorDefinition
from app.models.diagnosis_session import DiagnosisSession
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.sql.sqltypes import Enum as SQLEnum


def test_model_contains_required_tenant_trace_and_idempotency_columns():
    """领域根实体必须包含租户、Trace 和幂等字段。"""

    expected = {
        "session_id",
        "case_id",
        "tenant_id",
        "created_by",
        "status",
        "resume_status",
        "version",
        "idempotency_key",
        "request_hash",
        "trace_id",
        "created_at",
        "updated_at",
    }

    assert expected.issubset(DiagnosisSession.__table__.columns.keys())
    assert DiagnosisSession.__table__.columns.tenant_id.nullable is False
    assert DiagnosisSession.__table__.columns.trace_id.nullable is False
    case_foreign_key = next(iter(DiagnosisSession.__table__.columns.case_id.foreign_keys))
    assert case_foreign_key.use_alter is True
    assert case_foreign_key.name == "fk_diagnosis_session_case_id"


def test_status_enum_matches_domain_state_machine():
    """数据库枚举值必须覆盖领域状态机。"""

    column_type = DiagnosisSession.__table__.columns.status.type
    assert isinstance(column_type, SQLEnum)
    assert column_type.name == "diagnosis_session_status"
    assert set(column_type.enums) == {status.value for status in DiagnosisSessionStatus}


def test_idempotency_and_business_constraints_are_declared():
    """模型必须声明并发和 P0 补采约束。"""

    constraints = DiagnosisSession.__table__.constraints
    unique_names = {item.name for item in constraints if isinstance(item, UniqueConstraint)}
    check_names = {item.name for item in constraints if isinstance(item, CheckConstraint)}

    assert "uq_diagnosis_session_tenant_idempotency" in unique_names
    assert "ck_diagnosis_session_version" in check_names
    assert "ck_diagnosis_session_supplement_count" in check_names
    assert "ck_diagnosis_session_incident_window" in check_names


def test_declarative_schema_contains_model_contract():
    """声明式数据库文件必须与 ORM 关键契约同步。"""

    repository_root = Path(__file__).resolve().parents[4]
    schema_sql = (repository_root / "database" / "desired_schema.sql").read_text(encoding="utf-8")
    extras_sql = (repository_root / "database" / "desired_extras.sql").read_text(encoding="utf-8")

    assert "CREATE TYPE diagnosis_session_status AS ENUM" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS diagnosis_session" in schema_sql
    assert "CONSTRAINT uq_diagnosis_session_tenant_idempotency" in schema_sql
    assert "CONSTRAINT ck_diagnosis_session_supplement_count" in schema_sql
    assert "CONSTRAINT ck_diagnosis_session_incident_window" in schema_sql
    assert "CREATE TRIGGER update_diagnosis_session_updated_at" in extras_sql
    assert "CREATE TABLE IF NOT EXISTS collection_plan" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS collection_plan_item" in schema_sql
    assert "CONSTRAINT uq_collection_plan_tenant_idempotency" in schema_sql
    assert "CREATE TRIGGER update_collection_plan_updated_at" in extras_sql
    assert "CREATE TABLE IF NOT EXISTS collector_definition" in schema_sql
    assert "CONSTRAINT ck_collector_definition_review_status" in schema_sql
    assert "CREATE TRIGGER update_collector_definition_updated_at" in extras_sql
    assert "CREATE TABLE IF NOT EXISTS collector_artifact" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS collector_artifact_item" in schema_sql
    assert "CONSTRAINT uq_collector_artifact_tenant_idempotency" in schema_sql
    assert "public_key_base64 text NOT NULL" in schema_sql
    assert "CREATE TRIGGER update_collector_artifact_updated_at" in extras_sql
    assert "source_kbd_id bigint" in schema_sql
    assert "source_kbd_revision integer" in schema_sql
    assert "source_signal_id varchar(128)" in schema_sql
    assert "execution_contract_checksum varchar(64)" in schema_sql
    assert "ck_offline_signal_collector_mapping_exact_source" in schema_sql
    assert "uq_offline_signal_mapping_scope" not in schema_sql

    for status in DiagnosisSessionStatus:
        assert f"'{status.value}'" in schema_sql


def test_collection_plan_tables_preserve_trace_and_idempotency_constraints():
    """采集计划表包含 Trace ID、幂等和执行顺序约束。"""

    assert CollectionPlan.__table__.c.trace_id.nullable is False
    assert CollectionPlanItem.__table__.c.trace_id.nullable is False
    plan_constraints = {constraint.name for constraint in CollectionPlan.__table__.constraints}
    item_constraints = {constraint.name for constraint in CollectionPlanItem.__table__.constraints}
    assert "uq_collection_plan_tenant_idempotency" in plan_constraints
    assert "uq_collection_plan_session_sequence_revision" in plan_constraints
    assert "ck_collection_plan_revision" in plan_constraints
    assert "ck_collection_plan_status" in plan_constraints
    assert "uq_collection_plan_item_sequence" in item_constraints
    assert "ck_collection_plan_item_required_level" in item_constraints
    assert "ck_collection_plan_item_activation_state" in item_constraints


def test_collector_tables_preserve_approval_signature_and_trace_contracts():
    """Collector 事实源和制品表包含审批、签名、幂等及 Trace 字段。"""

    assert CollectorDefinition.__table__.c.trace_id.nullable is False
    assert CollectorArtifact.__table__.c.trace_id.nullable is False
    assert CollectorArtifactItem.__table__.c.trace_id.nullable is False
    assert CollectorArtifact.__table__.c.signature_base64.nullable is False
    assert CollectorArtifact.__table__.c.public_key_base64.nullable is False
    definition_constraints = {constraint.name for constraint in CollectorDefinition.__table__.constraints}
    artifact_constraints = {constraint.name for constraint in CollectorArtifact.__table__.constraints}
    assert "ck_collector_definition_review_status" in definition_constraints
    assert "uq_collector_artifact_tenant_idempotency" in artifact_constraints
    assert "uq_collector_artifact_plan_target" in artifact_constraints
