"""
Environment 数据模型

存储前端采集的 HCI 现场环境数据，JSONB 全量存储。
env_type 决定 env_data 的结构：
  - cluster: 集群版本、节点列表、集群状态
  - host: 主机配置、资源配额
  - vm: 虚拟机列表、运行状态
  - network: 网络拓扑、VLAN 配置
  - alert: 告警列表（用于 S0 Prompt 注入）
  - task: 任务状态列表（用于 S0 Prompt 注入）
"""

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin
from sqlalchemy import Column, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID


class Environment(Base, TimestampMixin, TraceableMixin):
    """环境信息表 — 存储 HCI 现场环境数据"""

    __tablename__ = "environment"

    environment_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    case_id = Column(
        String(20),
        ForeignKey("case.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联工单 ID",
    )
    env_type = Column(
        String(50),
        nullable=False,
        index=True,
        comment="环境类型：cluster/host/vm/network/alert/task",
    )
    env_data = Column(
        JSONB,
        nullable=False,
        comment="环境数据 JSONB 内容，结构随 env_type 变化",
    )
    collected_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="数据采集时间",
    )

    def __repr__(self):
        return f"<Environment(environment_id={self.environment_id}, case_id={self.case_id}, env_type={self.env_type!r})>"
