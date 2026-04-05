"""
Customer Model - 客户档案表

与 User 完全独立：
  - User   = 平台登录身份（前端 localStorage client_id 或认证用户）
  - Customer = HCI 产品采购方（公司/单位级别，运营人员手动维护）

case.customer_id 为可选关联，用于按客户维度聚合工单统计。
"""

import uuid

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin
from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import JSONB, UUID


class Customer(Base, TimestampMixin, TraceableMixin):
    """客户档案表"""

    __tablename__ = "customer"

    customer_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(64), unique=True, nullable=True, index=True)    # 外部系统客户 ID（幂等键），手动新增可为 NULL
    name = Column(String(200), nullable=False, index=True)               # 客户全称（公司名称）
    short_name = Column(String(100), nullable=True)                      # 客户简称，前端空间不足时优先展示
    product_version = Column(String(50), nullable=True, index=True)      # 购买的 HCI 产品版本（如 HCI 6.x）
    region = Column(String(100), nullable=True)                          # 所在区域（华南/华北/华东）
    industry = Column(String(100), nullable=True)                        # 所属行业（金融/医疗/政务/教育）
    metadata_ = Column("metadata", JSONB, default=dict, nullable=False)  # 扩展元数据（合同编号、销售负责人等）

    def __repr__(self):
        return f"<Customer(customer_id={self.customer_id}, name={self.name!r}, code={self.code!r})>"
