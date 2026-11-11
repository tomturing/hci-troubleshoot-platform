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
    # 全平台只有一个工单号：acli log_collect -s 与产物名都直接用 case_id
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
    # 平台对象索引：仅 P2 有值，P1 为 NULL（平台不留副本）。
    # 与 archive_* 解耦：archive_path 在 P1 是节点绝对路径、在 P2 是对象 key，
    # 语义混用导致"平台有没有留包"只能进容器 ls。
    object_key = Column(Text, nullable=True)
    object_size = Column(BigInteger, nullable=True)
    object_sha256 = Column(String(64), nullable=True)
    # P2 段①：从 HCI 拉取到客户 PC 的进度，rsync/SFTP 断点续传依据
    pull_offset_bytes = Column(BigInteger, nullable=True)
    pull_sha256 = Column(String(64), nullable=True)
    # P2 段③：日志服务器（5888）分块上传的续传状态，必须逐块写入
    relay_upload_id = Column(Text, nullable=True)
    relay_chunk_index = Column(Integer, nullable=True)
    relay_chunk_total = Column(Integer, nullable=True)
    relay_restarts = Column(Integer, nullable=False, default=0, server_default="0")
    relay_chunk_size = Column(Integer, nullable=True)
    # 当前（或最近一次）代传尝试轮次：`_run_with_retry` 每轮开始写一次。
    # 与 relay_restarts 的区别：restarts 记的是"会话被丢弃重建"次数，attempt 记的是
    # "整轮重试"次数——用户问"到底试了几次"时，答案是后者。
    relay_attempt = Column(Integer, nullable=False, default=0, server_default="0")
    # 最近一次失败原因（含瞬时失败）。终态成功时清空：成功任务挂一条历史错误只会误导排障。
    relay_last_error = Column(Text, nullable=True)
    relay_last_error_at = Column(DateTime(timezone=True), nullable=True)
    # 代传状态与任务主状态机解耦：主状态反映业务阶段，relay_status 反映归档结果
    relay_status = Column(String(16), nullable=False, default="none", server_default="'none'")
    relay_updated_at = Column(DateTime(timezone=True), nullable=True)
    # 交付状态与采集状态正交：status 说"采集走到哪一步"，delivery_status 说"产物是否真到了平台"。
    # 二者混用一个字段会重演 2026-10-27 的假完成——节点报 completed 就写 completed，
    # 而平台侧一个字节都没有。补交付必须能独立记录、独立重试。
    delivery_status = Column(String(16), nullable=False, default="none", server_default="'none'")
    delivery_started_at = Column(DateTime(timezone=True), nullable=True)
    delivery_finished_at = Column(DateTime(timezone=True), nullable=True)
    delivery_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    delivery_error = Column(Text, nullable=True)
    # 端到端完整性结论：verified = 节点源包摘要与平台合并结果一致（有基线且相符）；
    # unverified = 没拿到基线，只做了传输层校验，不得对外宣称"完整性已校验"。
    # 校验失败不是一种状态：它表现为 complete 返回 4xx，制品不会落库。
    integrity_state = Column(
        String(16), nullable=False, default="unverified", server_default="'unverified'"
    )
    # 清理：平台只标记与告警，绝不自动删除，也不提供删除 API
    cleanup_state = Column(String(16), nullable=False, default="none", server_default="'none'")
    cleanup_reason = Column(String(64), nullable=True)
    cleanup_ack_by = Column(String(64), nullable=True)
    cleanup_ack_at = Column(DateTime(timezone=True), nullable=True)
    archive_deleted = Column(Boolean, nullable=False, default=False, server_default="false")
    archive_deleted_at = Column(DateTime(timezone=True), nullable=True)
    archive_deleted_by = Column(String(64), nullable=True)
    # 自定义场景（acli -c）：只追加路径，不替代 modules（config.modules 永不生效）。
    # 三个字段缺一不可：id+revision 证明"用的哪一版"，sha256 证明"下发的就是这一版"。
    custom_scenario_id = Column(UUID(as_uuid=True), nullable=True)
    custom_scenario_revision = Column(Integer, nullable=True)
    config_path = Column(Text, nullable=True)
    config_sha256 = Column(String(64), nullable=True)
    path_count = Column(Integer, nullable=True)
    # 下发前预检未命中的路径：非空意味着"这次可能少收"，必须让用户看见。
    preflight_missing = Column(JSONB, nullable=True)
    # spc 场景：清单是节点本地文件，bridge 逐台覆写后再下发。
    # 必须落库"写了哪几台、备份叫什么、还原了没有"——否则事后无法判断
    # 节点上残留的是平台清单还是原件，也无法知道哪台需要人工兜底。
    spc_slot = Column(String(32), nullable=True)
    spc_sha256 = Column(String(64), nullable=True)
    spc_hosts = Column(JSONB, nullable=True)
    # 本次下发的全部槽位；多槽位时终态与平台兜底还原必须逐槽位重放。
    spc_slots = Column(JSONB, nullable=True)
    restore_status = Column(String(16), nullable=False, default="none", server_default="'none'")
    detail = Column(JSONB, nullable=False, default=dict)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<LogCollectTask(task_id={self.task_id}, case_id={self.case_id}, status={self.status})>"


class LogCollectCustomScenario(Base, TimestampMixin):
    """日志收集自定义场景（acli ``-c``）。

    第一性原理：``-c`` 的物理形态是"节点上的一个 JSON 文件"，平台唯一能写节点文件
    的通道是 bridge 的 SSH 会话。因此场景必须先在平台侧结构化保存，下发时再序列化。

    版本化是硬要求：场景被任务引用后，编辑必须生成新 ``revision``（复合主键），
    否则历史工单"当时收的是什么"会被后来的编辑永久抹掉。
    """

    __tablename__ = "log_collect_custom_scenario"

    scenario_id = Column(UUID(as_uuid=True), primary_key=True, server_default="gen_random_uuid()")
    revision = Column(Integer, primary_key=True, nullable=False, default=1)
    code = Column(String(32), nullable=False)
    label = Column(String(64), nullable=False)
    summary = Column(String(200), nullable=True)
    applies_to = Column(String(200), nullable=True)
    # 结构化路径项：[{path, category, note}]；下发时只取 path 拼逗号串。
    # 刻意不在库里存"下发的 JSON 原文"：平台模型与 acli 契约解耦，序列化只发生一处。
    items = Column(JSONB, nullable=False, default=list)
    item_count = Column(Integer, nullable=False, default=0, server_default="0")
    config_sha256 = Column(String(64), nullable=True)
    config_bytes = Column(Integer, nullable=True)
    source = Column(String(16), nullable=False, default="visual", server_default="'visual'")
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    sort_order = Column(Integer, nullable=False, default=100, server_default="100")
    review_state = Column(String(16), nullable=False, default="pending_review", server_default="'pending_review'")
    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<LogCollectCustomScenario(code={self.code}, revision={self.revision})>"


class LogCollectDefaultScenario(Base):
    """默认场景清单覆写（按 acli 模块）。

    平台只存"被改过的模块"：没改过的模块用随镜像发布的清单快照，
    恢复默认即删除本行。``module_code`` 同时是节点上被覆写的清单文件名，
    因此必须与 acli ``-m`` 白名单一致（由 service 层校验）。
    """

    __tablename__ = "log_collect_default_scenario"

    module_code = Column(String(32), primary_key=True)
    revision = Column(Integer, nullable=False, default=1)
    items = Column(JSONB, nullable=False, default=list)
    # 唯一调用链：把覆写保存动作与之后的下发/还原事件串成一条可检索链路。
    trace_id = Column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<LogCollectDefaultScenario(module={self.module_code}, revision={self.revision})>"
