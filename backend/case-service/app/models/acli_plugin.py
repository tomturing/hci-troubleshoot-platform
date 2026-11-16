"""acli 插件管理 ORM：环境 / 插件 / 不可变规格 / 下发任务。

与 ``database/desired_schema.sql`` 及两份 Helm init-configmap 三处同源。
四张表的职责边界见 2026-11-14 服务端编排重新设计事件文档：
规格表不含任何主机与凭据信息，凭据只在环境表里存 Secret 引用。
"""

from __future__ import annotations

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json(value) -> dict:
    return dict(value or {})


class AcliPluginEnvironment(Base):
    """下发目标环境；``credential_ref`` 指向 Secret key，明文不入库。"""

    __tablename__ = "acli_plugin_environment"

    env_code = Column(String(32), primary_key=True)
    display_name = Column(String(128), nullable=False, default="", server_default="")
    node_ip = Column(String(64), nullable=False)
    ssh_port = Column(Integer, nullable=False, default=22, server_default="22")
    auth_type = Column(String(16), nullable=False, default="key", server_default="'key'")
    credential_ref = Column(String(128), nullable=False, default="", server_default="")
    bridge_url = Column(String(256), nullable=False, default="", server_default="")
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default="CURRENT_TIMESTAMP",
    )

    def to_dict(self) -> dict:
        return {
            "env_code": self.env_code,
            "display_name": self.display_name,
            "node_ip": self.node_ip,
            "ssh_port": self.ssh_port,
            "auth_type": self.auth_type,
            "credential_ref": self.credential_ref,
            "bridge_url": self.bridge_url,
            "enabled": self.enabled,
        }


class AcliPlugin(Base):
    """插件当前态；current/desired spec_hash 分离用于识别待下发变更。"""

    __tablename__ = "acli_plugin"

    plugin_name = Column(String(64), primary_key=True)
    display_name = Column(String(200), nullable=False, default="", server_default="")
    update_mode = Column(String(24), nullable=False, default="third_party", server_default="'third_party'")
    current_spec_hash = Column(String(64), nullable=False, default="", server_default="")
    desired_spec_hash = Column(String(64), nullable=False, default="", server_default="")
    last_deploy_status = Column(String(32), nullable=False, default="never", server_default="'never'")
    last_deploy_at = Column(DateTime(timezone=True), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(String(128), nullable=False, default="", server_default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default="CURRENT_TIMESTAMP",
    )

    def to_dict(self) -> dict:
        return {
            "plugin_name": self.plugin_name,
            "display_name": self.display_name,
            "update_mode": self.update_mode,
            "current_spec_hash": self.current_spec_hash,
            "desired_spec_hash": self.desired_spec_hash,
            "last_deploy_status": self.last_deploy_status,
            "last_deploy_at": self.last_deploy_at.isoformat() if self.last_deploy_at else None,
            "enabled": self.enabled,
            "created_by": self.created_by,
        }


class AcliPluginSpec(Base):
    """不可变规格版本；同一 ``spec_hash`` 全局唯一，重复发布可幂等复用。"""

    __tablename__ = "acli_plugin_spec"

    spec_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    plugin_name = Column(String(64), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    spec_json = Column(JSONB, nullable=False)
    spec_hash = Column(String(64), nullable=False, unique=True)
    validation = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_by = Column(String(128), nullable=False, default="", server_default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, server_default="CURRENT_TIMESTAMP")

    def to_dict(self) -> dict:
        return {
            "spec_id": str(self.spec_id),
            "plugin_name": self.plugin_name,
            "revision": self.revision,
            "spec_json": self.spec_json,
            "spec_hash": self.spec_hash,
            "validation": _json(self.validation),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AcliPluginDeployment(Base):
    """下发任务；租约、重试与回执和业务状态正交，按 exec_id 与 bridge 日志对齐。"""

    __tablename__ = "acli_plugin_deployment"

    deploy_id = Column(String(64), primary_key=True)
    plugin_name = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False)
    spec_hash = Column(String(64), nullable=False)
    env_code = Column(String(32), nullable=False)
    node_ip = Column(String(64), nullable=False, default="", server_default="")
    status = Column(String(24), nullable=False, default="pending", server_default="'pending'")
    lease_owner = Column(String(128), nullable=False, default="", server_default="")
    lease_until = Column(DateTime(timezone=True), nullable=True)
    attempt = Column(Integer, nullable=False, default=0, server_default="0")
    exec_id = Column(String(64), nullable=False, default="", server_default="")
    trace_id = Column(String(128), nullable=False, default="", server_default="")
    verification = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    bridge_result = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    error_code = Column(String(64), nullable=False, default="", server_default="")
    error_message = Column(Text, nullable=False, default="", server_default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, server_default="CURRENT_TIMESTAMP")
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "deploy_id": self.deploy_id,
            "plugin_name": self.plugin_name,
            "revision": self.revision,
            "spec_hash": self.spec_hash,
            "env_code": self.env_code,
            "node_ip": self.node_ip,
            "status": self.status,
            "attempt": self.attempt,
            "exec_id": self.exec_id,
            "trace_id": self.trace_id,
            "verification": _json(self.verification),
            "bridge_result": _json(self.bridge_result),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
