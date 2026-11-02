"""DSH 诊断会话归档模型。

设计要点：
- DSH 是 Agent 运行时，会话真源是它自己的 ``$DSH_HOME/sessions/**/session.v4.jsonl.zstd``；
  本表只是**可检索投影**，删掉可以重放，不允许反过来当成真源。
- ``case_id`` 不建外键：DSH 会话可能引用本平台不存在的工单号（现场测试号、其他环境号），
  外键会让整条记录入库失败，从而丢掉诊断证据本身。
- ``messages`` 是快照而非日志：解析时按条数与单条长度截断，保证一屏可读。
"""

from shared.database.postgres import Base
from shared.models.base import TimestampMixin
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID


class DshDiagnosisSession(Base, TimestampMixin):
    """DSH 诊断会话（对话 + 最终结论）。"""

    __tablename__ = "dsh_diagnosis_session"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default="gen_random_uuid()")
    session_id = Column(String(120), nullable=False, unique=True)
    # 允许为空：识别不到工单号时保留记录但不展示，绝不猜一个工单挂上去
    case_id = Column(String(20), nullable=True, index=True)
    title = Column(Text, nullable=True)
    workspace = Column(String(255), nullable=True)
    agent_preset = Column(String(64), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, nullable=False, default=0)
    tool_call_count = Column(Integer, nullable=False, default=0)
    conclusion_markdown = Column(Text, nullable=True)
    messages = Column(JSONB, nullable=False, default=list)
    binding_source = Column(String(16), nullable=False, default="none")
    source_path = Column(Text, nullable=False)
    source_size = Column(BigInteger, nullable=False, default=0)
    source_mtime = Column(DateTime(timezone=True), nullable=True)
    content_sha256 = Column(String(64), nullable=True)
    trace_id = Column(String(64), nullable=True)
    # 布尔列留作后续「未关联会话」列表的过滤依据，当前解析器恒定写入 True
    truncated = Column(Boolean, nullable=False, default=False)
    ingested_at = Column(DateTime(timezone=True), nullable=True)
