
-- ============================================================
-- 说明：本文件是 HCI 数据库的声明式期望状态（Desired Schema）
-- 由 Atlas 工具管理，开发者修改此文件后运行 atlas migrate diff 生成迁移
-- 注意：不包含 schema_migrations 表（dbmate 工具表，已废弃）
--       也不包含 atlas_schema_revisions 表（Atlas 自动管理）
-- ============================================================

-- ============================================================
-- HCI 智能排障平台数据库 Schema
-- 完整的数据库表结构定义，包含所有表、字段、索引、外键的详细注释。v6.2 设计修正：恢复 diagnostic_item 表（conversation 子实体，与 message/tool_result 同构）；移除 conversation.hypothesis/react_state（JSONB blob 反模式）
-- Version : 6.2
-- Updated : 2026-04-04
-- ============================================================

-- 数据库类型: PostgreSQL 15

-- 设计原则:
--   trace_id: 所有业务表含 trace_id VARCHAR(64)，采用 W3C traceparent 格式
--   timestamps: 核心业务表通过触发器统一维护 created_at/updated_at（均带时区）
--   cascade_delete: 外键链路 user → case → conversation → message 均使用 ON DELETE CASCADE
--   redundancy: message.case_id 等为冗余字段，写入时同步，查询时免跨表
--   jsonb_flexibility: 不确定结构的扩展字段统一用 JSONB
--   vector_search: 通过 pgvector 扩展支持 1536 维向量，用于知识库语义检索和意图识别

-- ============================================================
-- 扩展（由 postgres init SQL 管理，不在此处声明）
-- 依赖：uuid-ossp, pgcrypto, pg_trgm, vector
-- 见 deploy/helm/hci-platform/templates/postgres/init-configmap.yaml
-- 注意：Atlas Community 不支持在 schema 文件中声明 extensions（需要 atlas login）
-- ============================================================

-- ============================================================
-- 自定义 ENUM 类型
-- ============================================================
CREATE TYPE case_status AS ENUM ('created', 'confirmed', 'in_progress', 'resolved', 'closed', 'cancelled');

CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system', 'command');




-- ============================================================
-- 表结构
-- ============================================================

-- ------------------------------------------------------------
-- 表: user  [模块: case-service]
-- 说明: 用户表 — 存储平台用户身份信息，当前版本以'临时用户'为主（前端自动生成 client_id，无需登录）
-- 用途: case-service 在接收新工单请求时，先 UPSERT user 表（以 client_id 为唯一键），再创建 case。临时用户通过 client_id（前端持久化到 localStorage）跨会话关联历史工单
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "user" (
    user_id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id varchar(255) NOT NULL UNIQUE,
    username varchar(100),
    email varchar(255),
    user_type varchar(20) NOT NULL DEFAULT 'temporary',
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    last_login_at timestamptz,
    trace_id varchar(64),
    CONSTRAINT user_pkey PRIMARY KEY (user_id)
);

COMMENT ON TABLE "user" IS '用户表 — 存储平台用户身份信息，当前版本以''临时用户''为主（前端自动生成 client_id，无需登录）';
COMMENT ON COLUMN "user".user_id IS '系统内部主键，全局唯一，不暴露给前端';
COMMENT ON COLUMN "user".client_id IS '前端生成并持久化的唯一标识（UUID v4 格式），UNIQUE 约束，UPSERT 的幂等键；前端刷新/重开后只要 localStorage 未清除即可关联历史工单';
COMMENT ON COLUMN "user".username IS '用户显示名，临时用户默认为空，认证用户可填写';
COMMENT ON COLUMN "user".email IS '邮箱，可选；用于未来通知功能';
COMMENT ON COLUMN "user".user_type IS '用户类型枚举：temporary（临时用户，无需登录）/ authenticated（认证用户，预留）';
COMMENT ON COLUMN "user".metadata IS '扩展元数据，存储不在固定字段中的用户属性（如设备信息、语言偏好等）';
COMMENT ON COLUMN "user".created_at IS '首次创建时间，由 TimestampMixin 注入，只读';
COMMENT ON COLUMN "user".updated_at IS '最后更新时间，由触发器 update_user_updated_at 自动维护，禁止手动更新';
COMMENT ON COLUMN "user".last_login_at IS '最后登录/活跃时间，临时用户每次建立 WebSocket 连接时更新';
COMMENT ON COLUMN "user".trace_id IS '创建该用户的请求追踪 ID（W3C traceparent），用于问题溯源';

-- 索引: user
-- P1-1: idx_user_client_id 已移除（client_id 有 UNIQUE 约束，隐含 B-tree 索引，无需重复创建）
-- O-001: idx_user_trace_id 已移除（链路追踪通过 Tempo/日志查找，不走数据库）
-- 用户类型统计（低基数，按需保留用于分析查询）
CREATE INDEX IF NOT EXISTS idx_user_type ON "user" (user_type);


-- ------------------------------------------------------------
-- 表: customer  [模块: case-service]
-- 说明: 客户表 — 存储购买 HCI 产品的客户公司信息，是工单的可选关联实体（与 user 表完全独立：user=端侧登录身份，customer=HCI 产品购买方）
-- 用途: 运营人员手动导入或通过数据管道同步客户档案（对应公司/单位级别，非用户个人）。case.customer_id 选填，关联后可按客户维度统计工单量、SLA、产品版本故障分布。联系人信息等 PII 字段按需通过 metadata 扩展或由外部 CRM 系统管理
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer (
    customer_id uuid NOT NULL DEFAULT gen_random_uuid(),
    code varchar(64) UNIQUE,
    name varchar(200) NOT NULL,
    short_name varchar(100),
    product_version varchar(50),
    region varchar(100),
    industry varchar(100),
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT customer_pkey PRIMARY KEY (customer_id)
);

COMMENT ON TABLE customer IS '客户表 — 存储购买 HCI 产品的客户公司信息，是工单的可选关联实体（与 user 表完全独立：user=端侧登录身份，customer=HCI 产品购买方）';
COMMENT ON COLUMN customer.customer_id IS '客户主键，全局唯一，系统内部使用';
COMMENT ON COLUMN customer.code IS '客户编码（幂等键），对应外部系统客户 ID，用于数据导入去重；手动新增未指定编码时可为 NULL';
COMMENT ON COLUMN customer.name IS '客户全称（公司名称），工单列表展示时显示';
COMMENT ON COLUMN customer.short_name IS '客户简称，前端空间不足时优先展示';
COMMENT ON COLUMN customer.product_version IS '客户购买的 HCI 产品版本（如 HCI 6.x / HCI 7.x），用于分版本统计故障分布';
COMMENT ON COLUMN customer.region IS '客户所在区域（如华南、华北、华东）';
COMMENT ON COLUMN customer.industry IS '客户所属行业（如金融、医疗、政务、教育）';
COMMENT ON COLUMN customer.metadata IS '扩展元数据，存储合同编号、销售负责人等非固定属性';
COMMENT ON COLUMN customer.created_at IS '创建时间';
COMMENT ON COLUMN customer.updated_at IS '最后更新时间，触发器自动维护';
COMMENT ON COLUMN customer.trace_id IS '创建该客户记录的请求追踪 ID（W3C traceparent）';

-- 索引: customer
-- 客户名称模糊查询
CREATE INDEX IF NOT EXISTS idx_customer_name ON customer (name);
-- 编码精确查询（数据导入幂等）
CREATE INDEX IF NOT EXISTS idx_customer_code ON customer (code);
-- 产品版本维度统计
CREATE INDEX IF NOT EXISTS idx_customer_product_version ON customer (product_version);
-- 区域维度统计
CREATE INDEX IF NOT EXISTS idx_customer_region ON customer (region);

-- 触发器: 自动刷新 updated_at

-- ------------------------------------------------------------
-- 表: case  [模块: case-service]
-- 说明: 工单表 — 排障工单是整个平台的核心业务实体，记录一次完整的客户问题从提交到关闭的全生命周期
-- 用途: 前端提交问题描述 → case-service 创建 case → conversation-service 创建 conversation。用户发送 /close 命令 → case-service 更新 status = closed
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "case" (
    case_id varchar(20) NOT NULL,
    user_id uuid NOT NULL,
    client_id varchar(255) NOT NULL,
    customer_id uuid,
    title varchar(500) NOT NULL,
    description text,
    status case_status NOT NULL DEFAULT 'created',
    priority varchar(20) DEFAULT 'medium',
    category varchar(100),
    assistant_type varchar(50) NOT NULL DEFAULT 'openclaw',
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    confirmed_at timestamptz,
    resolved_at timestamptz,
    closed_at timestamptz,
    close_reason varchar(20) CHECK (close_reason IN ('user_command','timeout','abandon','admin_close')),
    trace_id varchar(64),
    CONSTRAINT fk_case_user_id FOREIGN KEY (user_id) REFERENCES "user" (user_id) ON DELETE CASCADE,
    CONSTRAINT fk_case_customer_id FOREIGN KEY (customer_id) REFERENCES customer (customer_id) ON DELETE SET NULL,
    CONSTRAINT case_pkey PRIMARY KEY (case_id)
);

COMMENT ON TABLE "case" IS '工单表 — 排障工单是整个平台的核心业务实体，记录一次完整的客户问题从提交到关闭的全生命周期';
COMMENT ON COLUMN "case".case_id IS '业务可读工单号，格式 Q{YYYYMMDD}{NNNNN}，由数据库函数 generate_case_id() 在服务层调用生成，当天序号从 00001 开始自增；不使用 UUID 是为了人工沟通友好';
COMMENT ON COLUMN "case".user_id IS '关联 user.user_id，ON DELETE CASCADE，删除用户时级联删除所有工单';
COMMENT ON COLUMN "case".client_id IS '冗余字段，复制自 user.client_id，用于无 JOIN 快速查询''某客户端的所有工单''';
COMMENT ON COLUMN "case".customer_id IS '关联客户，选填（工单创建时可不指定）；ON DELETE SET NULL，删除客户档案不影响历史工单数据';
COMMENT ON COLUMN "case".title IS '工单标题，由前端提交时截取问题描述前 500 字符，或由 AI 自动生成摘要';
COMMENT ON COLUMN "case".description IS '问题完整描述，前端第一条消息内容，可为 NULL（后续消息补充）';
COMMENT ON COLUMN "case".status IS '工单生命周期状态枚举：created（创建瞬间，毫秒级驻留）/ confirmed（已确认，AI 对话进行中，主要工作状态）/ in_progress（升级为人工处理，预留）/ resolved（问题已解决待用户确认，预留）/ closed（终态）/ cancelled（预留）';
COMMENT ON COLUMN "case".priority IS '优先级：low / medium / high / urgent；当前由人工设置，未来可由 AI 自动评级';
COMMENT ON COLUMN "case".category IS '故障分类（如 vm/storage/network/cluster/backup），由前端选择或 AI 自动分类后回填';
COMMENT ON COLUMN "case".assistant_type IS 'AI 助手类型标识，当前值为 openclaw；预留多助手切换能力（如 LearningClaw/ProductionClaw 分流）';
COMMENT ON COLUMN "case".metadata IS '扩展元数据，如附件 URL、打标结果、人工备注等';
COMMENT ON COLUMN "case".created_at IS '工单创建时间';
COMMENT ON COLUMN "case".updated_at IS '最后更新时间，触发器自动维护';
COMMENT ON COLUMN "case".confirmed_at IS '状态变为 confirmed 的时间戳；当前版本创建后自动确认（毫秒级），是计算''响应时效''的起点';
COMMENT ON COLUMN "case".resolved_at IS '状态变为 resolved 的时间戳（预留字段，当前版本未使用）';
COMMENT ON COLUMN "case".closed_at IS '状态变为 closed 的时间戳，closed_at - confirmed_at 为工单处理时长';
COMMENT ON COLUMN "case".close_reason IS '关闭原因：user_command=用户主动关闭 / timeout=超时 / abandon=用户放弃 / admin_close=管理员关闭；被动信号轨道核心数据，权重 20% 的综合质量评分来源';
COMMENT ON COLUMN "case".trace_id IS '创建工单的请求 trace ID';

-- 索引: case
-- 用户工单查询
CREATE INDEX IF NOT EXISTS idx_case_user_id ON "case" (user_id);
-- 客户端工单查询
CREATE INDEX IF NOT EXISTS idx_case_client_id ON "case" (client_id);
-- 状态过滤
CREATE INDEX IF NOT EXISTS idx_case_status ON "case" (status);
-- 时间排序
CREATE INDEX IF NOT EXISTS idx_case_created_at ON "case" (created_at DESC);
-- 链路追踪
CREATE INDEX IF NOT EXISTS idx_case_trace_id ON "case" (trace_id);
-- 分类统计
CREATE INDEX IF NOT EXISTS idx_case_category ON "case" (category);
-- 复合索引：查询某客户端的活跃工单
CREATE INDEX IF NOT EXISTS idx_case_client_status ON "case" (client_id, status);
-- 助手类型统计
CREATE INDEX IF NOT EXISTS idx_case_assistant_type ON "case" (assistant_type);
-- 客户维度工单查询
CREATE INDEX IF NOT EXISTS idx_case_customer_id ON "case" (customer_id) WHERE customer_id IS NOT NULL;


-- ------------------------------------------------------------
-- 表: environment  [模块: case-service]
-- 说明: 环境信息表 — 存储前端采集的 HCI 现场环境数据，JSONB 全量存储
-- 用途: 存储前端采集的 HCI 现场环境数据（如集群版本、主机配置、网络拓扑等），JSONB 全量存储
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS environment (
    environment_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    env_type varchar(50) NOT NULL,
    env_data jsonb NOT NULL,
    collected_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT fk_environment_case_id FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE,
    CONSTRAINT environment_pkey PRIMARY KEY (environment_id)
);

COMMENT ON TABLE environment IS '环境信息表 — 存储前端采集的 HCI 现场环境数据，JSONB 全量存储';
COMMENT ON COLUMN environment.environment_id IS '环境信息主键，全局唯一';
COMMENT ON COLUMN environment.case_id IS '关联工单，ON DELETE CASCADE';
COMMENT ON COLUMN environment.env_type IS '环境类型（如 cluster/host/vm/network）';
COMMENT ON COLUMN environment.env_data IS '环境数据，JSONB 全量存储，结构根据 env_type 不同而变化';
COMMENT ON COLUMN environment.collected_at IS '数据采集时间';
COMMENT ON COLUMN environment.trace_id IS '采集请求 trace ID';

-- 索引: environment
-- 工单环境查询
CREATE INDEX IF NOT EXISTS idx_environment_case_id ON environment (case_id);
-- 类型过滤
CREATE INDEX IF NOT EXISTS idx_environment_type ON environment (env_type);
-- 时间排序
CREATE INDEX IF NOT EXISTS idx_environment_collected_at ON environment (collected_at DESC);
-- JSONB 内容检索
CREATE INDEX IF NOT EXISTS idx_environment_data_gin ON environment USING GIN (env_data);
-- O-001: idx_environment_trace_id 已移除（环境表链路追踪通过日志/Tempo 查找，不走 DB）

-- ------------------------------------------------------------
-- 表: assistant_evaluation  [模块: case-service]
-- 说明: AI 助手评估表 — 以工单为粒度的双轨评分，归属 case-service
-- 用途: 用户对 AI 助手的服务质量进行评分，包含分数、反馈文本、处理时长等指标
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assistant_evaluation (
    evaluation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    conversation_id uuid,
    assistant_type varchar(50) NOT NULL,
    score smallint,
    feedback text,
    resolution_time_seconds integer,
    message_count integer,
    metadata jsonb DEFAULT '{}'::jsonb,
    close_reason varchar(20),
    session_duration_sec integer,
    repeat_question_count integer,
    composite_score smallint,
    score_breakdown jsonb,
    calculated_at timestamptz,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT fk_assistant_evaluation_case_id FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE,
    -- D-006: 评分范围约束，防止 ORM/前端写入非法值
    CONSTRAINT chk_assistant_evaluation_score CHECK (score IS NULL OR (score >= 1 AND score <= 5)),
    CONSTRAINT chk_assistant_evaluation_composite_score CHECK (composite_score IS NULL OR (composite_score >= 0 AND composite_score <= 100)),
    CONSTRAINT assistant_evaluation_pkey PRIMARY KEY (evaluation_id)
);

COMMENT ON TABLE assistant_evaluation IS 'AI 助手评估表 — 以工单为粒度的双轨评分，归属 case-service';
COMMENT ON COLUMN assistant_evaluation.evaluation_id IS '评估主键，全局唯一';
COMMENT ON COLUMN assistant_evaluation.case_id IS '关联工单，ON DELETE CASCADE';
COMMENT ON COLUMN assistant_evaluation.conversation_id IS '关联会话，ON DELETE SET NULL';
COMMENT ON COLUMN assistant_evaluation.assistant_type IS '被评估的 AI 助手类型';
COMMENT ON COLUMN assistant_evaluation.score IS '评分，1-5 分';
COMMENT ON COLUMN assistant_evaluation.feedback IS '用户反馈文本';
COMMENT ON COLUMN assistant_evaluation.resolution_time_seconds IS '问题解决耗时（秒）';
COMMENT ON COLUMN assistant_evaluation.message_count IS '对话轮次';
COMMENT ON COLUMN assistant_evaluation.metadata IS '扩展字段';
COMMENT ON COLUMN assistant_evaluation.close_reason IS '关闭原因冗余字段（复制自 case.close_reason），避免质量评分时 JOIN case 表。值域与 case.close_reason 保持一致：user_command / timeout / abandon / admin_close。被动信号轨道权重 20%';
COMMENT ON COLUMN assistant_evaluation.session_duration_sec IS '会话总时长（秒），= conversation.ended_at - conversation.started_at。用于解决效率维度计算，关单时由 QualityScoreService 填入。10 分钟内解决得分高';
COMMENT ON COLUMN assistant_evaluation.repeat_question_count IS '用户重复提问次数（冗余自 conversation.repeat_question_count），避免 JOIN。0 次=满分，>3 次=严重扣分。质量评分效率维度重要权重';
COMMENT ON COLUMN assistant_evaluation.composite_score IS '综合质量分 0-100，由 QualityScoreService 在关单时计算并写入。双轨制：有用户主动评分时占 20% 权重；无评分时降级为三维模型（close_reason/efficiency/repeat_question）';
COMMENT ON COLUMN assistant_evaluation.score_breakdown IS '各维度详细分解，格式：{"close_intent":90,"efficiency":70,"user_rating":80,"ai_quality":65}。close_intent=关闭意图信号分，efficiency=解决效率分，user_rating=主动评分，ai_quality=AI 质量分';
COMMENT ON COLUMN assistant_evaluation.calculated_at IS '综合质量分计算时间，由 QualityScoreService 在写入 composite_score 时同步写入';
COMMENT ON COLUMN assistant_evaluation.created_at IS '评估创建时间';
COMMENT ON COLUMN assistant_evaluation.trace_id IS '创建评估的请求 trace ID';

-- 索引: assistant_evaluation
-- 工单评估查询
CREATE INDEX IF NOT EXISTS idx_eval_case_id ON assistant_evaluation (case_id);
-- 助手类型统计
CREATE INDEX IF NOT EXISTS idx_eval_assistant_type ON assistant_evaluation (assistant_type);
-- 评分统计
CREATE INDEX IF NOT EXISTS idx_eval_score ON assistant_evaluation (score);
-- 时间排序
CREATE INDEX IF NOT EXISTS idx_eval_created_at ON assistant_evaluation (created_at DESC);
-- O-001: idx_eval_trace_id 已移除（评估表不在请求热路径，链路追踪不需要 DB 索引）
-- 综合质量分排名统计（仅已计算）
CREATE INDEX IF NOT EXISTS idx_eval_composite_score ON assistant_evaluation (composite_score) WHERE composite_score IS NOT NULL;

-- ------------------------------------------------------------
-- 表: conversation  [模块: conversation-service]
-- 说明: 对话会话表 — 记录一次与 AI 助手的对话会话。一个工单可以有多个 conversation（用户断开后重连会创建新的 conversation）
-- 用途: 用户首次发消息时创建 conversation，并获取 Scheduler 分配的 AI Pod ID。SSE 流式对话期间，AI 推理完成后异步更新 message_count（由 DB 触发器维护）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation (
    conversation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    pod_id varchar(100),
    assistant_type varchar(50) NOT NULL DEFAULT 'openclaw',
    diagnostic_stage varchar(5) NOT NULL DEFAULT 'S0',
    category_id varchar(64),
    category_l1 varchar(100),
    category_l2 varchar(200),
    started_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    ended_at timestamptz,
    message_count integer DEFAULT 0,
    metadata jsonb DEFAULT '{}'::jsonb,
    pending_confirm jsonb,
    pending_resolution jsonb,
    repeat_question_count integer NOT NULL DEFAULT 0,
    trace_id varchar(64),
    CONSTRAINT fk_conversation_case_id FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE,
    -- D-006: 防止非法阶段值写入（状态机只允许 S0-S6）
    CONSTRAINT chk_conversation_diagnostic_stage CHECK (diagnostic_stage IN ('S0','S1','S2','S3','S4','S5','S6')),
    CONSTRAINT conversation_pkey PRIMARY KEY (conversation_id)
);

COMMENT ON TABLE conversation IS '对话会话表 — 记录一次与 AI 助手的对话会话。一个工单可以有多个 conversation（用户断开后重连会创建新的 conversation）';
COMMENT ON COLUMN conversation.conversation_id IS '会话主键，全局唯一';
COMMENT ON COLUMN conversation.case_id IS '关联工单，ON DELETE CASCADE';
COMMENT ON COLUMN conversation.pod_id IS 'Scheduler Service 分配的 AI 助手 Pod 标识（如 openclaw-pod-abc123），用于追踪本次对话由哪个 Pod 服务';
COMMENT ON COLUMN conversation.assistant_type IS 'AI 助手类型，与 case.assistant_type 保持一致，冗余存储便于直接在 conversation 维度统计';
COMMENT ON COLUMN conversation.diagnostic_stage IS '当前诊断阶段（状态机标记），初始为 S0：S0=意图识别 / S1=SOP 关联 / S2=假设生成 / S3=验证步骤 / S4=根因分析 / S5=解决方案 / S6=完结。由 conversation-service AI 响应解析后更新，禁止前端直接写入';
COMMENT ON COLUMN conversation.category_id IS 'S0 意图识别确认的故障分类编码，关联 kb_category.code；S0 完成后写入，后续阶段用于 SOP 检索和工具定义过滤';
COMMENT ON COLUMN conversation.category_l1 IS '冗余字段：确认分类的 L1 技术域名称（如''虚拟机''），避免前端展示时 JOIN kb_category';
COMMENT ON COLUMN conversation.category_l2 IS '冗余字段：确认分类的 L2 问题名称（如''虚拟机开机失败''），前端展示当前问题类别';
COMMENT ON COLUMN conversation.started_at IS '会话开始时间';
COMMENT ON COLUMN conversation.ended_at IS '会话结束时间（用户关闭工单或 Pod 回收时写入）';
COMMENT ON COLUMN conversation.message_count IS '只读字段，由触发器 update_conversation_message_count 在 message 表 INSERT/DELETE 时自动维护；代码层只读，禁止手动修改';
COMMENT ON COLUMN conversation.metadata IS '扩展字段，存储 case_title/case_description（供 Pod 分配时注入环境变量）、context_info（S0 意图识别环境上下文）等';
COMMENT ON COLUMN conversation.pending_confirm IS '待用户确认的工具调用快照，格式：{"audit_id":"...","tool_name":"vm_migrate","risk":"medium","cmd":"..."}。仅 risk_level>=2 且等待确认期间有值，用户确认/拒绝/超时后清空（NULL）。前端 SSE 事件驱动，此字段作为断线重连时的恢复锚点';
COMMENT ON COLUMN conversation.pending_resolution IS 'S6 验证闭环后等待用户选择的快照（A/B/C 选项），格式：{"resolution":"...","choices":["A:...","B:...","C:..."]}。用户选择后清空（NULL），A 选项触发关闭工单';
COMMENT ON COLUMN conversation.repeat_question_count IS '用户重复提问次数，由 conversation-service 实时统计（Jaccard 相似度 >= 0.6 判定为重复）。质量评分效率维度的核心输入，关单时复制到 assistant_evaluation.repeat_question_count';
COMMENT ON COLUMN conversation.trace_id IS '创建会话的请求 trace ID';

-- 索引: conversation
-- 工单会话查询
CREATE INDEX IF NOT EXISTS idx_conversation_case_id ON conversation (case_id);
-- Pod 维度统计
CREATE INDEX IF NOT EXISTS idx_conversation_pod_id ON conversation (pod_id);
-- 助手类型统计
CREATE INDEX IF NOT EXISTS idx_conversation_assistant_type ON conversation (assistant_type);
-- 时间排序
CREATE INDEX IF NOT EXISTS idx_conversation_started_at ON conversation (started_at DESC);
-- 链路追踪
CREATE INDEX IF NOT EXISTS idx_conversation_trace_id ON conversation (trace_id);
-- 复合索引：按工单查会话
CREATE INDEX IF NOT EXISTS idx_conversation_case_started ON conversation (case_id, started_at DESC);
-- 诊断阶段过滤（统计各阶段卡点比例）
CREATE INDEX IF NOT EXISTS idx_conversation_diagnostic_stage ON conversation (diagnostic_stage) WHERE diagnostic_stage IS NOT NULL;
-- 故障分类维度统计
CREATE INDEX IF NOT EXISTS idx_conversation_category_id ON conversation (category_id) WHERE category_id IS NOT NULL;
-- 有重复提问的会话过滤（质量分析）
CREATE INDEX IF NOT EXISTS idx_conversation_repeat_question ON conversation (repeat_question_count) WHERE repeat_question_count > 0;

-- conversation 表已建立，现在添加 assistant_evaluation 的 conversation 外键
ALTER TABLE assistant_evaluation
    ADD CONSTRAINT fk_assistant_evaluation_conversation_id
    FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE SET NULL;

-- 注意事项:
--   * message_count 禁止代码层手动递增（会导致双重计数）
--   * 禁止在 SSE 流程中使用请求作用域 Session（idle in transaction 期间持锁）
--   * diagnostic_stage 由 AI 响应解析后更新，初始为 NULL（S0 前）
--   * category_id 在 S0 确认后写入且不再修改，是后续 SOP 检索和工具注入的锚点
--   * conversation 行只保留轻量元数据：阶段标记、分类冗余、待确认快照
--   * 诊断结论（假设/验证步骤/根因/方案）持久化在 diagnostic_item 表，Prompt 构建时按 conversation_id + stage 查询
--   * ReAct 推理草稿（思考链、中间观察）存活在内存 AgentState 中，不持久化，重连时从 diagnostic_item 恢复

-- ------------------------------------------------------------
-- 表: message  [模块: conversation-service]
-- 说明: 消息表 — 存储对话中所有消息，是多轮对话上下文的持久化来源。每轮对话前 AI 从此表读取完整历史，因为 AI 模型本身无状态
-- 用途: 用户发送消息 → 先 INSERT role=user 消息（同步，独立 session）。读取历史 → SELECT 当前 conversation 的所有消息，按 created_at ASC 排序组装 messages[] 传给 AI。AI 回复结束 → BackgroundTask 异步 INSERT role=assistant 消息
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message (
    message_id uuid NOT NULL DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    case_id varchar(20) NOT NULL,
    "role" message_role NOT NULL,
    content text NOT NULL,
    command text,
    command_warning text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT fk_message_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    CONSTRAINT message_pkey PRIMARY KEY (message_id)
);

COMMENT ON TABLE message IS '消息表 — 存储对话中所有消息，是多轮对话上下文的持久化来源。每轮对话前 AI 从此表读取完整历史，因为 AI 模型本身无状态';
COMMENT ON COLUMN message.message_id IS '消息主键，全局唯一';
COMMENT ON COLUMN message.conversation_id IS '关联会话，ON DELETE CASCADE';
COMMENT ON COLUMN message.case_id IS '冗余字段，复制自 conversation.case_id，用于无 JOIN 快速查询''某工单的所有消息''';
COMMENT ON COLUMN message."role" IS '消息角色枚举：user（用户消息）/ assistant（AI 回复）/ system（系统提示）/ command（命令建议）';
COMMENT ON COLUMN message.content IS '消息内容，纯文本或 Markdown 格式';
COMMENT ON COLUMN message.command IS 'AI 建议执行的命令（如 acli vm.start --vm-id xxx），仅 role=command 时填写';
COMMENT ON COLUMN message.command_warning IS '命令执行风险提示，仅 role=command 时填写';
COMMENT ON COLUMN message.metadata IS '扩展字段，如消息来源、token 统计等';
COMMENT ON COLUMN message.created_at IS '消息创建时间';
COMMENT ON COLUMN message.trace_id IS '创建消息的请求 trace ID';

-- 索引: message
-- 会话消息查询
CREATE INDEX IF NOT EXISTS idx_message_conversation_id ON message (conversation_id);
-- 工单消息查询
CREATE INDEX IF NOT EXISTS idx_message_case_id ON message (case_id);
-- 时间排序
CREATE INDEX IF NOT EXISTS idx_message_created_at ON message (created_at DESC);
-- 角色过滤
CREATE INDEX IF NOT EXISTS idx_message_role ON message ("role");
-- 链路追踪
CREATE INDEX IF NOT EXISTS idx_message_trace_id ON message (trace_id);
-- 复合索引：按工单查消息
CREATE INDEX IF NOT EXISTS idx_message_case_created ON message (case_id, created_at DESC);
-- 全文检索
-- D-003: 修复全文检索语言配置，content 为中文对话内容，english 分词无效
-- 使用 simple（按标点/空格分词，无需扩展），如生产环境安装了 zhparser 可改为 'chinese'
CREATE INDEX IF NOT EXISTS idx_message_content_search ON message USING GIN (to_tsvector('simple', content));

-- 触发器: 自动维护 conversation.message_count（message 表已建立，此处注册触发器）

-- ------------------------------------------------------------
-- 表: diagnostic_item  [模块: conversation-service]
-- 说明: 诊断条目表 — conversation 的子实体，与 message 同构（1 个 conversation → N 个诊断条目）。存储 S2-S5 各阶段产生的结构化结论：假设（hypothesis）/ 验证步骤（verification_step）/ 根因（root_cause）/ 解决方案（solution）。解决 BUG-06：原 conversation.hypothesis JSONB blob 导致 Pod 重启假设全丢、并发更新竞态、无法独立查询等问题
-- 用途: S2 生成假设时 INSERT 多条 type=hypothesis 记录；S3 生成验证步骤时 INSERT 多条 type=verification_step 记录；S4 确认根因时 INSERT 1 条 type=root_cause 记录；S5 生成方案时 INSERT 1-2 条 type=solution 记录。Prompt 构建时：SELECT * FROM diagnostic_item WHERE conversation_id=X AND type='hypothesis' ORDER BY seq 替代 conversation.hypothesis JSONB 解析
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diagnostic_item (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    stage varchar(5) NOT NULL,
    "type" varchar(30) NOT NULL,
    seq smallint NOT NULL DEFAULT 0,
    content jsonb NOT NULL DEFAULT '{}'::jsonb,
    probability real,
    status varchar(20) NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT fk_diagnostic_item_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    -- D-004: 防止重复 (conversation_id, type, seq) 组合导致 Prompt 构建混乱
    CONSTRAINT uq_diagnostic_item_conv_type_seq UNIQUE (conversation_id, "type", seq),
    -- D-006: 数据有效性约束
    CONSTRAINT chk_diagnostic_item_probability CHECK (probability IS NULL OR (probability >= 0 AND probability <= 1)),
    CONSTRAINT chk_diagnostic_item_stage CHECK (stage IN ('S2','S3','S4','S5')),
    CONSTRAINT diagnostic_item_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE diagnostic_item IS '诊断条目表 — conversation 的子实体，与 message 同构（1 个 conversation → N 个诊断条目）。存储 S2-S5 各阶段产生的结构化结论：假设（hypothesis）/ 验证步骤（verification_step）/ 根因（root_cause）/ 解决方案（solution）。解决 BUG-06：原 conversation.hypothesis JSONB blob 导致 Pod 重启假设全丢、并发更新竞态、无法独立查询等问题';
COMMENT ON COLUMN diagnostic_item.id IS '诊断条目主键，全局唯一';
COMMENT ON COLUMN diagnostic_item.conversation_id IS '关联会话，ON DELETE CASCADE';
COMMENT ON COLUMN diagnostic_item.stage IS '生成阶段：S2=假设生成 / S3=验证步骤 / S4=根因分析 / S5=解决方案';
COMMENT ON COLUMN diagnostic_item."type" IS '条目类型：hypothesis（根因假设，S2）/ verification_step（验证步骤，S3）/ root_cause（根因结论，S4）/ solution（解决方案，S5）';
COMMENT ON COLUMN diagnostic_item.seq IS '同会话同类型内的排序序号（从 1 开始）。hypothesis 按概率降序排列；verification_step 按执行顺序排列';
COMMENT ON COLUMN diagnostic_item.content IS '结构化内容，按 type 不同格式不同：hypothesis: {description, probability, evidence_needed}；verification_step: {action, expected_result, tool_hint}；root_cause: {description, confidence, evidence}；solution: {steps[], commands[]}';
COMMENT ON COLUMN diagnostic_item.probability IS '假设概率（0.0-1.0），仅 type=hypothesis 使用；NULL 表示不适用';
COMMENT ON COLUMN diagnostic_item.status IS '条目执行状态：pending（待处理）/ in_progress（验证中）/ confirmed（已确认/验证通过）/ rejected（已排除/验证失败）/ skipped（跳过）';
COMMENT ON COLUMN diagnostic_item.created_at IS '条目创建时间（AI 生成该假设/步骤的时间）';
COMMENT ON COLUMN diagnostic_item.updated_at IS '最后更新时间（状态变更时刷新），触发器自动维护';
COMMENT ON COLUMN diagnostic_item.trace_id IS '写入该条目的请求 trace ID';

-- 索引: diagnostic_item
-- 核心查询：按会话 + 类型 + 序号获取诊断条目（Prompt 构建路径）
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_conv_type ON diagnostic_item (conversation_id, "type", seq);
-- 按阶段汇总：查某会话 S2 阶段所有内容
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_conv_stage ON diagnostic_item (conversation_id, stage);
-- 状态统计：如'所有会话中被排除的假设比例'
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_status ON diagnostic_item ("type", status);

-- 触发器: 自动刷新 updated_at

-- 注意事项:
--   * 不存储 AI 的推理过程（思考链、中间观察），那些留在 AgentState 内存中
--   * 只存储阶段性最终结论：假设列表、验证步骤、根因、方案
--   * Pod 重启时，conversation-service 从此表恢复 DiagnosticSession.hypotheses
--   * 与 message 表完全同构的子实体模式：INSERT-only 用于生成，UPDATE 用于状态变更

-- ------------------------------------------------------------
-- 表: tool_result  [模块: conversation-service]
-- 说明: 工具执行结果表 — 记录每次 AI 调用工具（acli/scp_api）的请求参数、执行结果、风险等级和授权信息。从旧 audit_log.audit_type='tool_call' 分离，修复 BUG-03（step_no 字段缺失）
-- 用途: AI 调用工具时写入一条记录，包含工具名、参数、执行结果、耗时、风险等级；用于 CP-02 工具审计验证、高危操作追溯、工具性能 SLA 统计
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_result (
    id varchar(36) NOT NULL,
    conversation_id uuid NOT NULL,
    tool_name varchar(100) NOT NULL,
    tool_type varchar(20),
    step_no smallint,
    risk_level smallint NOT NULL DEFAULT 1,
    policy varchar(20),
    authorized_by varchar(100),
    input_json jsonb DEFAULT '{}'::jsonb,
    output_json jsonb,
    error text,
    duration_ms integer,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    trace_id varchar(64),
    CONSTRAINT fk_tool_result_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    -- D-006: 风险等级只允许 1（只读）/ 2（需确认）/ 3（高危），NOT NULL DEFAULT 1
    CONSTRAINT chk_tool_result_risk_level CHECK (risk_level >= 1 AND risk_level <= 3),
    CONSTRAINT tool_result_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE tool_result IS '工具执行结果表 — 记录每次 AI 调用工具（acli/scp_api）的请求参数、执行结果、风险等级和授权信息。从旧 audit_log.audit_type=''tool_call'' 分离，修复 BUG-03（step_no 字段缺失）';
COMMENT ON COLUMN tool_result.id IS '工具执行记录 ID，UUID 格式';
COMMENT ON COLUMN tool_result.conversation_id IS '关联会话，ON DELETE CASCADE';
COMMENT ON COLUMN tool_result.tool_name IS '工具标识名称（如 acli_vm_list / scp_get_servers），对应 tool_definition.tool_name';
COMMENT ON COLUMN tool_result.tool_type IS '工具类型：acli（CLI 命令行）/ scp_api（SCP REST 接口）';
COMMENT ON COLUMN tool_result.step_no IS '执行步骤编号（BUG-03 修复：原 tool_audit_log 缺失此字段），对应 task_plan.seq（S3 验证步骤序号）';
COMMENT ON COLUMN tool_result.risk_level IS '风险等级：1=只读查询 / 2=写操作 / 3=高危（对应 tool_definition.risk_level）';
COMMENT ON COLUMN tool_result.policy IS '执行策略：auto（自动执行）/ notify（执行并通知）/ confirm（需人工确认后执行）/ block（已拦截）';
COMMENT ON COLUMN tool_result.authorized_by IS '高危操作确认用户标识（policy=confirm 时必填，记录是谁授权了该操作）';
COMMENT ON COLUMN tool_result.input_json IS '工具调用输入参数（acli 类型为 CLI args；scp_api 类型为 HTTP 请求 body/params）';
COMMENT ON COLUMN tool_result.output_json IS '工具执行结果（acli 类型为命令输出；scp_api 类型为 HTTP 响应 body）';
COMMENT ON COLUMN tool_result.error IS '错误信息（工具调用失败时写入，成功时为 NULL）';
COMMENT ON COLUMN tool_result.duration_ms IS '执行耗时（毫秒），completed_at - started_at';
COMMENT ON COLUMN tool_result.started_at IS '工具调用开始时间';
COMMENT ON COLUMN tool_result.completed_at IS '工具调用完成时间（含失败场景）';
COMMENT ON COLUMN tool_result.trace_id IS '请求 trace ID';

-- 索引: tool_result
-- 会话工具调用查询（CP-02 验证）
CREATE INDEX IF NOT EXISTS idx_tool_result_conversation ON tool_result (conversation_id, started_at DESC);
-- 工具名称统计与性能分析
CREATE INDEX IF NOT EXISTS idx_tool_result_tool_name ON tool_result (tool_name, started_at DESC);
-- 高危操作审计
CREATE INDEX IF NOT EXISTS idx_tool_result_risk_level ON tool_result (risk_level) WHERE risk_level >= 2;
-- 链路追踪
CREATE INDEX IF NOT EXISTS idx_tool_result_trace_id ON tool_result (trace_id) WHERE trace_id IS NOT NULL;

-- ------------------------------------------------------------
-- 表: audit_log  [模块: conversation-service]
-- 说明: System Instructions 审计表 — 记录每轮对话的 Prompt 构建过程（含使用的模板版本、注入的工具列表片段、最终 Prompt token 数）。工具执行审计已移入 tool_result 表
-- 用途: 每次构建 System Instructions 时写入一条记录；payload 字段存储 {system_prompt_id, tool_count, rendered_token_count, model, case_id} 等信息，用于追踪 AI 行为来源和 Prompt 版本迭代效果
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_prompt (
    id serial NOT NULL,
    stage varchar(5) NOT NULL,
    name varchar(100) NOT NULL,
    description text,
    content_template text NOT NULL,
    version varchar(20) NOT NULL DEFAULT '1.0',
    is_active boolean DEFAULT true,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT system_prompt_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE system_prompt IS 'System Instructions 模板表 — 存储 S0-S6 各诊断阶段的 Prompt 模板，支持版本管理和阶段级 A/B 测试';
COMMENT ON COLUMN system_prompt.id IS '模板主键，自增';
COMMENT ON COLUMN system_prompt.stage IS '适用诊断阶段：S0/S1/S2/S3/S4/S5/S6 或 BASE（全局基础 Prompt，各阶段共用）';
COMMENT ON COLUMN system_prompt.name IS '模板唯一名称（如 s0_intent_recognition_v2），建议格式为 {stage}_{purpose}_{version}';
COMMENT ON COLUMN system_prompt.description IS '模板说明，描述该 Prompt 的用途、设计思路和与前版本的区别';
COMMENT ON COLUMN system_prompt.content_template IS 'Prompt 模板内容，使用 {placeholder} 占位符（如 {tool_list}、{category_name}、{sop_content}、{hypothesis_list}）';
COMMENT ON COLUMN system_prompt.version IS '版本号（如 1.0 / 1.1 / 2.0），配合 is_active 实现版本管理';
COMMENT ON COLUMN system_prompt.is_active IS '是否为当前激活版本；同一 stage 同时只有一个 is_active=true 的版本被注入 Prompt';
COMMENT ON COLUMN system_prompt.created_at IS '创建时间';
COMMENT ON COLUMN system_prompt.updated_at IS '最后更新时间';

-- 索引: system_prompt
-- 按阶段查当前激活模板（核心查询）
CREATE INDEX IF NOT EXISTS idx_system_prompt_stage_active ON system_prompt (stage, is_active);
-- 版本历史查询
CREATE INDEX IF NOT EXISTS idx_system_prompt_stage_version ON system_prompt (stage, version);

-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id varchar(36) NOT NULL,
    audit_type varchar(20) NOT NULL,
    conversation_id uuid NOT NULL,
    turn_index smallint,
    system_prompt_id integer,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    duration_ms integer,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    trace_id varchar(64),
    CONSTRAINT fk_audit_log_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    CONSTRAINT fk_audit_log_system_prompt_id FOREIGN KEY (system_prompt_id) REFERENCES system_prompt (id) ON DELETE SET NULL,
    CONSTRAINT audit_log_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE audit_log IS 'System Instructions 审计表 — 记录每轮对话的 Prompt 构建过程（含使用的模板版本、注入的工具列表片段、最终 Prompt token 数）。工具执行审计已移入 tool_result 表';
COMMENT ON COLUMN audit_log.id IS '审计记录 ID，UUID 格式';
COMMENT ON COLUMN audit_log.audit_type IS '审计类型，当前固定为 prompt（System Instructions 构建）；tool_call 类型已迁移至 tool_result 表';
COMMENT ON COLUMN audit_log.conversation_id IS '关联会话，ON DELETE CASCADE';
COMMENT ON COLUMN audit_log.turn_index IS '对话轮次索引（prompt 类型使用）';
COMMENT ON COLUMN audit_log.system_prompt_id IS '本次 Prompt 构建使用的模板 ID，关联 system_prompt.id。ON DELETE SET NULL，删除 Prompt 模板时不影响历史审计记录。NULL 表示使用硬编码 Prompt（迁移前历史数据）。按此字段聚合可分析不同 Prompt 版本的对话效果（平均 composite_score、session_duration_sec）';
COMMENT ON COLUMN audit_log.payload IS '类型专属字段（prompt 类型：case_id/assistant_type/model/messages 等；tool_call 类型：tool_args/result）';
COMMENT ON COLUMN audit_log.error IS '错误信息（如有）';
COMMENT ON COLUMN audit_log.duration_ms IS '执行耗时（毫秒）';
COMMENT ON COLUMN audit_log.started_at IS '开始时间';
COMMENT ON COLUMN audit_log.completed_at IS '完成时间';
COMMENT ON COLUMN audit_log.trace_id IS '请求 trace ID';

-- 索引: audit_log
-- 会话审计查询
CREATE INDEX IF NOT EXISTS idx_audit_log_conversation ON audit_log (conversation_id, started_at DESC);
-- 类型审计查询
CREATE INDEX IF NOT EXISTS idx_audit_log_type ON audit_log (audit_type, started_at DESC);
-- 链路追踪
CREATE INDEX IF NOT EXISTS idx_audit_log_trace_id ON audit_log (trace_id) WHERE trace_id IS NOT NULL;
-- Prompt 模板效果追踪
CREATE INDEX IF NOT EXISTS idx_audit_log_system_prompt_id ON audit_log (system_prompt_id) WHERE system_prompt_id IS NOT NULL;

-- ------------------------------------------------------------
-- 表: session  [模块: conversation-service]
-- 说明: 用户会话 Token 表 — 存储 SSE 长连接会话凭证，关联工单与用户
-- 用途: 用户打开工单时创建 session，SSE 连接建立时校验有效性；expires_at 控制超时
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session (
    session_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    user_id uuid NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    expires_at timestamptz,
    trace_id varchar(64),
    CONSTRAINT fk_session_case_id FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE,
    -- D-005: 补全外键约束，防止孤儿 session 记录
    CONSTRAINT fk_session_user_id FOREIGN KEY (user_id) REFERENCES "user" (user_id) ON DELETE CASCADE,
    CONSTRAINT session_pkey PRIMARY KEY (session_id)
);

COMMENT ON TABLE session IS '用户会话 Token 表 — 存储 SSE 长连接会话凭证，关联工单与用户';
COMMENT ON COLUMN session.session_id IS '会话 Token 主键，全局唯一';
COMMENT ON COLUMN session.case_id IS '关联工单，ON DELETE CASCADE';
COMMENT ON COLUMN session.user_id IS '关联用户';
COMMENT ON COLUMN session.metadata IS '扩展字段';
COMMENT ON COLUMN session.created_at IS '会话创建时间';
COMMENT ON COLUMN session.expires_at IS '会话过期时间，NULL 表示不过期';
COMMENT ON COLUMN session.trace_id IS '创建会话的请求 trace ID';

-- 索引: session
-- 工单会话查询
CREATE INDEX IF NOT EXISTS idx_session_case_id ON session (case_id);
-- 用户会话查询
CREATE INDEX IF NOT EXISTS idx_session_user_id ON session (user_id);

-- ------------------------------------------------------------
-- 表: system_prompt  [模块: conversation-service]
-- 说明: System Instructions 模板表 — 存储 S0-S6 各诊断阶段的 Prompt 模板，支持版本管理和阶段级 A/B 测试
-- 用途: Prompt 版本化管理：每个阶段可维护多个版本，is_active=true 的版本被激活；audit_log 记录每次使用的 system_prompt.id，用于效果追踪和快速回滚
-- ------------------------------------------------------------
-- 表: tool_definition  [模块: conversation-service]
-- 说明: 工具定义表 — AI 工具知识库，存储 LLM 可调用工具的完整描述（acli 命令 / SCP API）。Prompt 构建时动态注入，让 LLM 知道何时调用哪个工具以及如何传参
-- 用途: 解决'AI 不知道如何调用工具'的根本问题：Prompt 构建时 SELECT * FROM tool_definition WHERE is_active=true AND (category='{当前故障域}' OR category IS NULL)，格式化后追加到 System Instructions。新增工具时只需插入记录，无需改代码
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_definition (
    id serial NOT NULL,
    tool_name varchar(100) NOT NULL UNIQUE,
    display_name varchar(200),
    tool_type varchar(20) NOT NULL,
    category varchar(50),
    description text NOT NULL,
    usage_template text,
    parameters_schema jsonb,
    examples jsonb,
    risk_level smallint NOT NULL DEFAULT 1,
    is_active boolean DEFAULT true,
    version varchar(20) DEFAULT '1.0',
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    -- D-006: 工具风险等级约束：1=只读安全 / 2=需用户确认 / 3=高危
    CONSTRAINT chk_tool_definition_risk_level CHECK (risk_level >= 1 AND risk_level <= 3),
    CONSTRAINT tool_definition_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE tool_definition IS '工具定义表 — AI 工具知识库，存储 LLM 可调用工具的完整描述（acli 命令 / SCP API）。Prompt 构建时动态注入，让 LLM 知道何时调用哪个工具以及如何传参';
COMMENT ON COLUMN tool_definition.id IS '工具定义主键，自增';
COMMENT ON COLUMN tool_definition.tool_name IS '工具唯一标识（如 acli_vm_list / scp_get_servers），tool_result.tool_name 引用此字段；命名规则：{tool_type}_{资源}_{动作}';
COMMENT ON COLUMN tool_definition.display_name IS '工具展示名（如''获取虚拟机列表''），用于前端审计日志展示，比 tool_name 更易读';
COMMENT ON COLUMN tool_definition.tool_type IS '工具类型：acli（Sangfor HCI CLI 工具）/ scp_api（SCP REST API 接口）';
COMMENT ON COLUMN tool_definition.category IS '所属故障域（vm / storage / network / cluster / platform）。NULL 表示通用工具（所有故障域均注入）；非 NULL 则只在对应 category_id 的会话中注入，减少 Prompt token';
COMMENT ON COLUMN tool_definition.description IS '工具功能描述（直接注入 Prompt，LLM 读取后知道何时应该调用此工具）。示例：''获取 HCI 集群内所有虚拟机列表，可按名称、状态、宿主机过滤''';
COMMENT ON COLUMN tool_definition.usage_template IS '调用模板。acli 类型示例：''acli vm list [--filter <key>=<value>]''；scp_api 类型示例：''GET https://{SCP_IP}/janus/{version}/servers?page={page}&limit={limit}''';
COMMENT ON COLUMN tool_definition.parameters_schema IS '参数 JSON Schema（OpenAPI 3.0 格式），AI 按此 Schema 输出结构化参数对象，后端按此 Schema 校验后生成实际命令/请求';
COMMENT ON COLUMN tool_definition.examples IS '调用示例数组。acli 示例：[{"cmd": "acli vm list", "desc": "列出全部虚拟机"}, {"cmd": "acli vm list --filter name=test-vm", "desc": "按名称过滤"}]；scp_api 示例：[{"method": "GET", "path": "/janus/20240725/servers", "desc": "获取服务器列表"}]';
COMMENT ON COLUMN tool_definition.risk_level IS '风险等级：1=只读查询（不影响生产）/ 2=写操作（修改状态/配置）/ 3=高危（删除/重启/格式化）；影响 tool_result.policy 的默认策略';
COMMENT ON COLUMN tool_definition.is_active IS '是否启用；is_active=false 的工具不会注入 Prompt 也不会被 AI 调用，用于临时下线某工具';
COMMENT ON COLUMN tool_definition.version IS '工具接口版本（对应 CLI 版本或 API path 中的日期版本如 20240725）';
COMMENT ON COLUMN tool_definition.created_at IS '创建时间';
COMMENT ON COLUMN tool_definition.updated_at IS '最后更新时间';

-- 索引: tool_definition
-- 按类型查活跃工具
CREATE INDEX IF NOT EXISTS idx_tool_definition_type_active ON tool_definition (tool_type, is_active);
-- 按故障域 + 风险等级过滤注入（Prompt 构建核心查询）
CREATE INDEX IF NOT EXISTS idx_tool_definition_category_risk ON tool_definition (category, risk_level);
-- 风险等级统计
CREATE INDEX IF NOT EXISTS idx_tool_definition_risk_level ON tool_definition (risk_level);

-- ------------------------------------------------------------
-- 表: kb_category  [模块: kb-service]
-- 说明: 分类树表 — 全局分类枢纽（198 节点），意图识别锚点
-- 用途: 存储 198 个 HCI 故障分类，覆盖虚拟机/网络/存储/硬件/平台五个域。S0 意图识别阶段注入分类列表，引导 LLM 输出确认的故障分类
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kb_category (
    id serial NOT NULL,
    parent_id integer,
    name varchar(100) NOT NULL,
    level smallint NOT NULL,
    keywords text[],
    source varchar(50) DEFAULT 'manual',
    version varchar(20) DEFAULT '1.0',
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    code varchar(64) UNIQUE,
    domain varchar(100),
    path_labels jsonb DEFAULT '[]'::jsonb,
    embedding vector(1536),
    hit_count integer DEFAULT 0,
    is_active boolean DEFAULT true,
    CONSTRAINT fk_kb_category_parent_id FOREIGN KEY (parent_id) REFERENCES kb_category (id) ON DELETE NO ACTION,
    CONSTRAINT kb_category_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE kb_category IS '分类树表 — 全局分类枢纽（198 节点），意图识别锚点';
COMMENT ON COLUMN kb_category.id IS '分类主键，自增';
COMMENT ON COLUMN kb_category.parent_id IS '父分类 ID，自引用外键，L1 节点为 NULL';
COMMENT ON COLUMN kb_category.name IS '分类名称（如"虚拟机开机失败"），最长 100 字符；ORM 字段 String(100)，DB DDL VARCHAR(100)';
COMMENT ON COLUMN kb_category.level IS '层级：1=域节点（虚拟机/网络/存储/硬件/平台）/ 2=分组节点 / 3=叶节点 / 4=细分节点';
COMMENT ON COLUMN kb_category.keywords IS '关键词数组，用于关键词匹配（已废弃，改用语义匹配）';
COMMENT ON COLUMN kb_category.source IS '数据来源：manual（手动录入）/ baseline_yaml（YAML 导入）/ baseline_yaml_intermediate（YAML 导入中间节点）';
COMMENT ON COLUMN kb_category.version IS '数据版本';
COMMENT ON COLUMN kb_category.created_at IS '创建时间';
COMMENT ON COLUMN kb_category.code IS '分类编码（UNIQUE），对应 YAML id，如"虚拟机-003"。L1 域节点格式为"{domain}-L1"，中间节点格式为"{domain}-L{level}-{name}"。注意：ORM 字段 String(32) 是历史 Bug，目标 VARCHAR(64)，迁移时需 ALTER';
COMMENT ON COLUMN kb_category.domain IS '一级技术域：虚拟机 / 网络 / 存储 / 硬件 / 平台';
COMMENT ON COLUMN kb_category.path_labels IS '完整路径数组，如 ["虚拟机", "虚拟机创建", "虚拟机创建失败"]';
COMMENT ON COLUMN kb_category.embedding IS '意图识别向量（1536 维），用于 S0 阶段语义匹配';
COMMENT ON COLUMN kb_category.hit_count IS '命中计数，S0 确认分类后 +1';
COMMENT ON COLUMN kb_category.is_active IS '是否启用，可用于禁用某些分类';

-- 索引: kb_category
-- 分类编码查询
CREATE INDEX IF NOT EXISTS idx_kb_category_code ON kb_category (code);
-- 父子关系查询
CREATE INDEX IF NOT EXISTS idx_kb_category_parent ON kb_category (parent_id);
-- 层级过滤
CREATE INDEX IF NOT EXISTS idx_kb_category_level ON kb_category (level);
-- 关键词检索
CREATE INDEX IF NOT EXISTS idx_kb_category_keywords ON kb_category USING GIN (keywords);
-- D-002: 向量相似度检索索引（S0 意图识别阶段语义匹配）
-- IVFFlat lists=100：适合 ~200 个分类节点；当数据量超过 10000 时切换 HNSW
-- ⚠️  注意：IVFFlat 索引需在数据量 > 1000 行且执行 ANALYZE 后才能正常发挥效果。
--    全新部署建库后如数据量不足，查询会自动退化为顺序扫描（不影响正确性，仅影响性能）。
--    建议在批量导入知识库数据后执行：ANALYZE kb_category;
CREATE INDEX IF NOT EXISTS idx_kb_category_embedding ON kb_category
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- kb_category 数据统计:
--   total: 235
--   by_level: {'L1': 5, 'L2': 138, 'L3': 89, 'L4': 3}
--   by_domain: {'虚拟机': 54, '网络': 23, '存储': 42, '硬件': 75, '平台': 41}

-- ------------------------------------------------------------
-- 表: kbd_entry  [模块: kb-service]
-- 说明: KBD 知识条目表 — KBD 知识条目（~600 字/条），整条 embedding，无分块
-- 用途: 存储从深信服支持案例导入的知识条目，全生命周期管理：生产 → 审核 → 消费 → 归档
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kbd_entry (
    id bigserial NOT NULL,
    support_id varchar(20) NOT NULL UNIQUE,
    support_url text,
    title text NOT NULL,
    content_md text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    category_id varchar(32),
    ai_category_id varchar(32),
    ai_category_conf double precision,
    ai_category_reason text,
    embedding vector(1536),
    tsv tsvector,
    status varchar(20) NOT NULL DEFAULT 'draft',
    reviewer_id integer,
    reviewed_at timestamptz,
    review_note text,
    published_at timestamptz,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_kbd_entry_category_id FOREIGN KEY (category_id) REFERENCES kb_category (code) ON DELETE NO ACTION,
    CONSTRAINT kbd_entry_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE kbd_entry IS 'KBD 知识条目表 — KBD 知识条目（~600 字/条），整条 embedding，无分块';
COMMENT ON COLUMN kbd_entry.id IS '知识条目主键，自增';
COMMENT ON COLUMN kbd_entry.support_id IS '深信服案例 ID（幂等键）';
COMMENT ON COLUMN kbd_entry.support_url IS '深信服案例 URL';
COMMENT ON COLUMN kbd_entry.title IS '知识条目标题';
COMMENT ON COLUMN kbd_entry.content_md IS '全段结构化 Markdown 内容';
COMMENT ON COLUMN kbd_entry.metadata IS '扩展元数据（如案例类型、适用版本、标签等）';
COMMENT ON COLUMN kbd_entry.category_id IS '人工确认的分类编码，关联 kb_category.code';
COMMENT ON COLUMN kbd_entry.ai_category_id IS 'AI 分类建议编码';
COMMENT ON COLUMN kbd_entry.ai_category_conf IS 'AI 分类置信度（0-1）';
COMMENT ON COLUMN kbd_entry.ai_category_reason IS 'AI 分类理由';
COMMENT ON COLUMN kbd_entry.embedding IS '全文语义向量（1536 维），published 时生成';
COMMENT ON COLUMN kbd_entry.tsv IS 'BM25 全文检索向量，published 时生成';
COMMENT ON COLUMN kbd_entry.status IS '状态机：draft（草稿）/ published（已发布）/ archived（已归档）/ rejected（已拒绝）';
COMMENT ON COLUMN kbd_entry.reviewer_id IS '审核人 ID';
COMMENT ON COLUMN kbd_entry.reviewed_at IS '审核时间';
COMMENT ON COLUMN kbd_entry.review_note IS '审核备注';
COMMENT ON COLUMN kbd_entry.published_at IS '发布时间';
COMMENT ON COLUMN kbd_entry.archived_at IS '归档时间';
COMMENT ON COLUMN kbd_entry.created_at IS '创建时间';
COMMENT ON COLUMN kbd_entry.updated_at IS '最后更新时间';

-- 索引: kbd_entry
-- 状态过滤
CREATE INDEX IF NOT EXISTS idx_kbd_entry_status ON kbd_entry (status);
-- 分类过滤（仅已发布）
CREATE INDEX IF NOT EXISTS idx_kbd_entry_category ON kbd_entry (category_id) WHERE status = 'published';
-- AI 分类建议查询
CREATE INDEX IF NOT EXISTS idx_kbd_entry_ai_category ON kbd_entry (ai_category_id);
-- 发布时间排序（仅已发布）
CREATE INDEX IF NOT EXISTS idx_kbd_entry_published ON kbd_entry (published_at DESC) WHERE status = 'published';
-- 全文检索
CREATE INDEX IF NOT EXISTS idx_kbd_entry_tsv ON kbd_entry USING GIN (tsv);
-- JSONB 内容检索
CREATE INDEX IF NOT EXISTS idx_kbd_entry_metadata ON kbd_entry USING GIN (metadata);
-- D-002: 向量相似度检索索引（知识库语义检索，仅已发布条目）
-- 部分索引：只对 status='published' 的条目建索引，减少写入/存储开销，与业务查询路径吻合。
-- ⚠️  同 kb_category：数据量不足 1000 时效果有限，建议批量导入后执行：ANALYZE kbd_entry;
CREATE INDEX IF NOT EXISTS idx_kbd_entry_embedding ON kbd_entry
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
    WHERE status = 'published';

-- P2-2: 补全 updated_at 触发器（schema 注释中要求由触发器维护，但原先缺失）

-- kbd_entry 状态流转:
--   draft → published: 审核通过
--   draft → rejected: 审核拒绝
--   published → archived: 归档

-- ------------------------------------------------------------
-- 表: sop_document  [模块: kb-service]
-- 说明: SOP 文档表 — SOP 排障手册文档存储，完整 Markdown，按章节拆分为 sop_chunk
-- 用途: 存储 SOP 排障手册文档（~20,000 字/个），按章节拆分为 sop_chunk 进行检索
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sop_document (
    id serial NOT NULL,
    source_id varchar(100) UNIQUE,
    category_id varchar(32),
    title varchar(500),
    content_md text,
    docx_hash varchar(64),
    status varchar(20) DEFAULT 'draft',
    reviewer_id integer,
    reviewed_at timestamptz,
    review_note text,
    published_at timestamptz,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    CONSTRAINT fk_sop_document_category_id FOREIGN KEY (category_id) REFERENCES kb_category (code) ON DELETE NO ACTION,
    CONSTRAINT sop_document_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE sop_document IS 'SOP 文档表 — SOP 排障手册文档存储，完整 Markdown，按章节拆分为 sop_chunk';
COMMENT ON COLUMN sop_document.id IS '文档主键，自增';
COMMENT ON COLUMN sop_document.source_id IS '来源标识（如 sop-vm-start-failure），幂等键';
COMMENT ON COLUMN sop_document.category_id IS '关联分类编码，用于 S1+ 阶段按 category_id 直接查 SOP';
COMMENT ON COLUMN sop_document.title IS '文档标题';
COMMENT ON COLUMN sop_document.content_md IS '完整 SOP Markdown 内容';
COMMENT ON COLUMN sop_document.docx_hash IS '原文档哈希，用于幂等去重';
COMMENT ON COLUMN sop_document.status IS '状态：draft / published / archived';
COMMENT ON COLUMN sop_document.reviewer_id IS '审核人 ID';
COMMENT ON COLUMN sop_document.reviewed_at IS '审核时间';
COMMENT ON COLUMN sop_document.review_note IS '审核备注';
COMMENT ON COLUMN sop_document.published_at IS '发布时间';
COMMENT ON COLUMN sop_document.created_at IS '创建时间';
COMMENT ON COLUMN sop_document.updated_at IS '最后更新时间';

-- ------------------------------------------------------------
-- 表: sop_chunk  [模块: kb-service]
-- 说明: SOP 分块检索表 — SOP 分块 + 向量检索
-- 用途: SOP 按章节拆分，每个章节生成一个 chunk，支持 BM25(tsv) + 向量(embedding) RRF 融合检索
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sop_chunk (
    id serial NOT NULL,
    document_id integer NOT NULL,
    chunk_index smallint NOT NULL,
    chapter_title varchar(200),
    content text,
    embedding vector(1536),
    tsv tsvector,
    created_at timestamptz DEFAULT now(),
    CONSTRAINT fk_sop_chunk_document_id FOREIGN KEY (document_id) REFERENCES sop_document (id) ON DELETE CASCADE,
    CONSTRAINT sop_chunk_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE sop_chunk IS 'SOP 分块检索表 — SOP 分块 + 向量检索';
COMMENT ON COLUMN sop_chunk.id IS '分块主键，自增';
COMMENT ON COLUMN sop_chunk.document_id IS '关联文档 ID，ON DELETE CASCADE';
COMMENT ON COLUMN sop_chunk.chunk_index IS '分块索引（从 0 开始）';
COMMENT ON COLUMN sop_chunk.chapter_title IS '章节标题';
COMMENT ON COLUMN sop_chunk.content IS '章节内容';
COMMENT ON COLUMN sop_chunk.embedding IS '章节语义向量（1536 维）';
COMMENT ON COLUMN sop_chunk.tsv IS 'BM25 全文检索向量';
COMMENT ON COLUMN sop_chunk.created_at IS '创建时间';

-- 索引: sop_document
-- D-002: SOP 文档向量索引（通过 sop_chunk 实现，见下方）
-- P2-2: 补全 updated_at 触发器（字段有 updated_at 但原先无触发器）
-- 分类统计查询索引：支持 WHERE status = 'published' AND category_id = ?
CREATE INDEX IF NOT EXISTS idx_sop_document_category_status ON sop_document (category_id, status);

-- 索引: sop_chunk
-- chunk 顺序检索（按文档 + 分块序号）
CREATE INDEX IF NOT EXISTS idx_sop_chunk_document ON sop_chunk (document_id, chunk_index);
-- 全文检索
CREATE INDEX IF NOT EXISTS idx_sop_chunk_tsv ON sop_chunk USING GIN (tsv);
-- D-002: 向量相似度检索索引（SOP 章节语义检索，S1+ 阶段使用）
-- ⚠️  注意：IVFFlat 需数据量 > 1000 行且执行 ANALYZE 后才能正常发挥效果。
--    建议在批量导入 SOP 内容后执行：ANALYZE sop_chunk;
CREATE INDEX IF NOT EXISTS idx_sop_chunk_embedding ON sop_chunk
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
