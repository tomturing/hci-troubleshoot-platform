"""
日志收集任务数据模型。

设计要点：
- 任务挂在工单下（case_id 外键，ON DELETE CASCADE），与 environment 同级；
- 执行发生在 terminal_bridge，平台只保存控制面状态与制品元数据；
- ``origin_node_ip`` 不可省略：acli progress 只在该节点有效，轮询必须路由回去。
"""

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID


class LogCollectTask(Base, TimestampMixin, TraceableMixin):
    """日志收集任务表"""

    __tablename__ = "log_collect_task"

    task_id = Column(UUID(as_uuid=True), primary_key=True, server_default="gen_random_uuid()")
    case_id = Column(String(20), ForeignKey("case.case_id", ondelete="CASCADE"), nullable=False, index=True)
    # 用户输入的 ICare 单号，直接作为 acli log_collect -s，并成为产物名
    ticket_no = Column(String(32), nullable=False)
    # 发起采集的节点：progress 只反映该节点的本地视图
    origin_node_ip = Column(INET, nullable=False)
    modules = Column(ARRAY(Text), nullable=False)
    date_from = Column(Date, nullable=False)
    date_to = Column(Date, nullable=False)
    ips = Column(ARRAY(Text), nullable=False)
    extra_paths = Column(ARRAY(Text), nullable=True)
    remote_url = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    progress = Column(SmallInteger, nullable=False, default=0)
    arch = Column(String(16), nullable=True)
    acli_version = Column(String(32), nullable=True)
    upload_mode = Column(String(16), nullable=True)
    archive_path = Column(Text, nullable=True)
    archive_size = Column(BigInteger, nullable=True)
    archive_sha256 = Column(String(64), nullable=True)
    # P3 段①：从 HCI 拉取到客户 PC 的进度，rsync/SFTP 断点续传依据
    pull_offset_bytes = Column(BigInteger, nullable=True)
    pull_sha256 = Column(String(64), nullable=True)
    # P3 段③：日志服务器（5888）分块上传的续传状态，必须逐块写入
    relay_upload_id = Column(Text, nullable=True)
    relay_chunk_index = Column(Integer, nullable=True)
    relay_chunk_total = Column(Integer, nullable=True)
    relay_restarts = Column(Integer, nullable=False, default=0, server_default="0")
    relay_chunk_size = Column(Integer, nullable=True)
    # 代传状态与任务主状态机解耦：主状态反映业务阶段，relay_status 反映归档结果
    relay_status = Column(String(16), nullable=False, default="none", server_default="'none'")
    relay_updated_at = Column(DateTime(timezone=True), nullable=True)
    # 清理：平台只标记与告警，绝不自动删除，也不提供删除 API
    cleanup_state = Column(String(16), nullable=False, default="none", server_default="'none'")
    cleanup_reason = Column(String(64), nullable=True)
    cleanup_ack_by = Column(String(64), nullable=True)
    cleanup_ack_at = Column(DateTime(timezone=True), nullable=True)
    archive_deleted = Column(Boolean, nullable=False, default=False, server_default="false")
    archive_deleted_at = Column(DateTime(timezone=True), nullable=True)
    archive_deleted_by = Column(String(64), nullable=True)
    detail = Column(JSONB, nullable=False, default=dict)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<LogCollectTask(task_id={self.task_id}, case_id={self.case_id}, status={self.status})>"
