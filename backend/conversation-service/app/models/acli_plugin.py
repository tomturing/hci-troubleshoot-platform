"""acli 手工插件定义与下发回执的数据模型。

平台侧只保存「生成 HCI 插件四要素」所需的输入与下发结果；
设备侧真正生效由 terminal_bridge 的 ssh_exec_process 通道完成。
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Boolean, Column, DateTime, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AcliPlugin(Base):
    __tablename__ = "acli_plugin"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plugin_name = Column(String(64), nullable=False, unique=True)
    plugin_uuid = Column(String(36), nullable=False, default="")
    display_name = Column(String(200), nullable=False)
    title_zh = Column(String(300), nullable=False, default="")
    title_en = Column(String(300), nullable=False, default="")
    description_zh = Column(Text, nullable=False, default="")
    description_en = Column(Text, nullable=False, default="")
    vendor_name = Column(String(200), nullable=False, default="HTP")
    change_log = Column(Text, nullable=False, default="")
    version = Column(String(20), nullable=False, default="0.0.1")
    acli_version = Column(String(64), nullable=False, default=">=1.0.0")
    hci_version = Column(String(64), nullable=False, default="")
    source = Column(SmallInteger, nullable=False, default=3)
    status = Column(SmallInteger, nullable=False, default=1)
    arch = Column(SmallInteger, nullable=False, default=1)
    container = Column(String(64), nullable=False, default="")
    interpreter = Column(String(64), nullable=False, default="bash")
    script_filename = Column(String(255), nullable=False, default="troubleshoot.sh")
    script_content = Column(Text, nullable=False)
    parameters = Column(JSONB, nullable=False, default=list)
    extra_files = Column(JSONB, nullable=False, default=list)
    enable_completion = Column(Boolean, nullable=False, default=True)
    passthrough_help = Column(Boolean, nullable=False, default=False)
    enable_pid_lock = Column(Boolean, nullable=False, default=False)
    timeout_seconds = Column(Integer, nullable=False, default=120)
    smoke_args = Column(String(255), nullable=False, default="")
    device_host = Column(String(128), nullable=False, default="")
    device_port = Column(Integer, nullable=False, default=22)
    device_username = Column(String(64), nullable=False, default="root")
    node_ip = Column(String(64), nullable=False, default="")
    source_url = Column(Text, nullable=False, default="")
    spec_hash = Column(String(64), nullable=False, default="")
    last_deploy_id = Column(String(64), nullable=False, default="")
    last_deploy_status = Column(String(32), nullable=False, default="never")
    last_deploy_at = Column(DateTime(timezone=True), nullable=True)
    last_deploy_detail = Column(JSONB, nullable=False, default=dict)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "plugin_name": self.plugin_name,
            "plugin_uuid": self.plugin_uuid,
            "display_name": self.display_name,
            "title_zh": self.title_zh,
            "title_en": self.title_en,
            "description_zh": self.description_zh,
            "description_en": self.description_en,
            "vendor_name": self.vendor_name,
            "change_log": self.change_log,
            "version": self.version,
            "acli_version": self.acli_version,
            "hci_version": self.hci_version,
            "source": self.source,
            "status": self.status,
            "arch": self.arch,
            "container": self.container,
            "interpreter": self.interpreter,
            "script_filename": self.script_filename,
            "script_content": self.script_content,
            "parameters": self.parameters or [],
            "extra_files": self.extra_files or [],
            "enable_completion": self.enable_completion,
            "passthrough_help": self.passthrough_help,
            "enable_pid_lock": self.enable_pid_lock,
            "timeout_seconds": self.timeout_seconds,
            "smoke_args": self.smoke_args or "",
            "device_host": self.device_host,
            "device_port": self.device_port,
            "device_username": self.device_username,
            "node_ip": self.node_ip,
            "source_url": self.source_url,
            "spec_hash": self.spec_hash,
            "last_deploy_id": self.last_deploy_id,
            "last_deploy_status": self.last_deploy_status,
            "last_deploy_at": self.last_deploy_at.isoformat() if self.last_deploy_at else None,
            "last_deploy_detail": self.last_deploy_detail or {},
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AcliPluginDeployment(Base):
    __tablename__ = "acli_plugin_deployment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deploy_id = Column(String(64), nullable=False, unique=True)
    plugin_id = Column(Integer, nullable=False)
    plugin_name = Column(String(64), nullable=False)
    version = Column(String(20), nullable=False)
    spec_hash = Column(String(64), nullable=False)
    device_host = Column(String(128), nullable=False, default="")
    node_ip = Column(String(64), nullable=False, default="")
    exec_id = Column(String(64), nullable=False, default="")
    trace_id = Column(String(128), nullable=False, default="")
    status = Column(String(32), nullable=False)
    exit_code = Column(Integer, nullable=True)
    stdout = Column(Text, nullable=False, default="")
    stderr = Column(Text, nullable=False, default="")
    verification = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "deploy_id": self.deploy_id,
            "plugin_id": self.plugin_id,
            "plugin_name": self.plugin_name,
            "version": self.version,
            "spec_hash": self.spec_hash,
            "device_host": self.device_host,
            "node_ip": self.node_ip,
            "exec_id": self.exec_id,
            "trace_id": self.trace_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "verification": self.verification or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
