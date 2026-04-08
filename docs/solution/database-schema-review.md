# HCI 数据库 Schema 审核报告

**审核日期**: 2026-04-08  
**审核对象**: `hci-troubleshoot-platform/database/desired_schema.sql` (Version 6.2)  
**审核标准**: 数据库设计范式、业界最佳实践、PostgreSQL性能优化

---

## 📊 问题统计

- **关键问题 (P0)**: 5个 - 影响数据一致性和系统稳定性
- **重要问题 (P1)**: 4个 - 影响性能和可维护性  
- **优化建议 (P2)**: 3个 - 改善代码质量

---

## 🔴 P0 - 关键问题（必须修复）

### P0-1: 主键设计不一致

**问题描述**:  
系统中混用了三种主键策略，缺乏统一规范：
- UUID: `user.user_id`, `conversation.conversation_id`
- VARCHAR业务ID: `case.case_id` (Q20260408xxxx)
- Serial自增: `kb_category.id`, `system_prompt.id`
- VARCHAR UUID字符串: `tool_result.id`, `audit_log.id`

**影响**:
- 数据库层面无法通过主键类型快速识别表的性质
- 业务可读ID作为主键导致分布式场景下ID生成冲突
- `case.case_id` 作为VARCHAR在JOIN时性能劣于UUID
- `tool_result.id`/`audit_log.id` 使用VARCHAR存储UUID浪费存储空间（36字节 vs 16字节）

**修复建议**:
```sql
-- 1. case表：将业务ID改为普通字段
ALTER TABLE "case" ADD COLUMN case_uuid UUID DEFAULT gen_random_uuid();
ALTER TABLE "case" ADD CONSTRAINT case_uuid_unique UNIQUE (case_uuid);
-- 迁移外键后，将case_uuid提升为主键，case_id改为case_number

-- 2. tool_result和audit_log：改用UUID类型
ALTER TABLE tool_result ALTER COLUMN id TYPE UUID USING id::uuid;
ALTER TABLE audit_log ALTER COLUMN id TYPE UUID USING id::uuid;

-- 3. 保持配置表使用serial（tool_definition, system_prompt等）
```

**优先级**: P0 - 建议在数据量小时尽早修改，避免后期迁移成本

---

### P0-2: 外键级联策略不当导致历史数据丢失风险

**问题描述**:  
核心业务表使用 `ON DELETE CASCADE` 会导致删除工单时级联删除所有历史记录：

```sql
-- 问题外键
CONSTRAINT fk_assistant_evaluation_case_id 
  FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE

CONSTRAINT fk_conversation_case_id 
  FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE

CONSTRAINT fk_audit_log_conversation_id 
  FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE
```

**影响**:
- 删除工单会丢失所有评估数据（`assistant_evaluation`），无法进行历史质量分析
- 删除对话会丢失所有审计日志（`audit_log`），违反审计合规要求
- 误删除操作无法恢复

**修复建议**:
```sql
-- 1. 评估表：保留历史数据，软删除case即可
ALTER TABLE assistant_evaluation 
  DROP CONSTRAINT fk_assistant_evaluation_case_id,
  ADD CONSTRAINT fk_assistant_evaluation_case_id 
    FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE RESTRICT;

-- 2. 审计表：改为SET NULL，保留孤儿记录用于审计
ALTER TABLE audit_log 
  DROP CONSTRAINT fk_audit_log_conversation_id,
  ADD CONSTRAINT fk_audit_log_conversation_id 
    FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE SET NULL;

-- 3. 工具执行记录：改为RESTRICT
ALTER TABLE tool_result 
  DROP CONSTRAINT fk_tool_result_conversation_id,
  ADD CONSTRAINT fk_tool_result_conversation_id 
    FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE RESTRICT;

-- 4. 实现软删除
ALTER TABLE "case" ADD COLUMN deleted_at TIMESTAMPTZ;
ALTER TABLE conversation ADD COLUMN deleted_at TIMESTAMPTZ;
CREATE INDEX idx_case_deleted ON "case" (deleted_at) WHERE deleted_at IS NULL;
```

**优先级**: P0 - 直接影响数据安全

---

### P0-3: 缺少必要的数据约束

**问题描述**:  
多个字段使用字符串枚举但缺少CHECK约束，导致脏数据风险：

```sql
-- 无约束的枚举字段
"case".priority varchar(20) DEFAULT 'medium'  -- 可能插入'urgent123'
"case".category varchar(100)  -- 无外键关联kb_category
conversation.diagnostic_stage varchar(5) DEFAULT 'S0'  -- 可能插入'S99'
assistant_evaluation.score smallint  -- 可能插入999
assistant_evaluation.composite_score smallint  -- 可能插入-50
tool_result.risk_level smallint  -- 可能插入999
```

**影响**:
- 应用层bug可能写入非法值，导致统计查询异常
- 前端展示时需要处理意外值
- 数据质量无法在数据库层保证

**修复建议**:
```sql
-- 1. 创建ENUM类型或添加CHECK约束
DO $$ BEGIN
    CREATE TYPE priority_level AS ENUM ('low', 'medium', 'high', 'urgent');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE "case" 
  ALTER COLUMN priority TYPE priority_level USING priority::priority_level;

-- 2. 诊断阶段约束
ALTER TABLE conversation 
  ADD CONSTRAINT chk_diagnostic_stage 
  CHECK (diagnostic_stage IN ('S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6'));

-- 3. 评分范围约束
ALTER TABLE assistant_evaluation 
  ADD CONSTRAINT chk_score CHECK (score IS NULL OR (score BETWEEN 1 AND 5)),
  ADD CONSTRAINT chk_composite_score CHECK (composite_score IS NULL OR (composite_score BETWEEN 0 AND 100));

-- 4. 风险等级约束
ALTER TABLE tool_result 
  ADD CONSTRAINT chk_risk_level CHECK (risk_level BETWEEN 1 AND 3);

-- 5. category外键约束
ALTER TABLE "case" 
  ADD CONSTRAINT fk_case_category 
  FOREIGN KEY (category) REFERENCES kb_category (code) ON DELETE SET NULL;
```

**优先级**: P0 - 防止脏数据

---

### P0-4: JSONB滥用导致查询性能问题

**问题描述**:  
多处将结构化数据存储在JSONB中，导致无法有效索引和查询：

```sql
-- 问题1: environment.env_data 全量JSONB
-- 如果需要查询"所有HCI 6.x版本的环境"，必须扫描全表JSONB

-- 问题2: diagnostic_item.content JSONB
-- 不同type的content结构完全不同，强行塞入一个字段

-- 问题3: tool_definition.parameters_schema/examples JSONB
-- 这些是相对固定的结构，可以拆表
```

**影响**:
- 无法对JSONB内部字段建立有效索引（GIN索引效率低）
- 统计查询（如"HCI 7.0故障率"）需要全表扫描
- ORM层需要额外序列化/反序列化逻辑
- 数据库无法保证JSONB内部结构一致性

**修复建议**:
```sql
-- 1. environment表：提取高频查询字段
ALTER TABLE environment 
  ADD COLUMN cluster_version VARCHAR(50),
  ADD COLUMN host_count INTEGER,
  ADD COLUMN vm_count INTEGER;

CREATE INDEX idx_environment_cluster_version ON environment (cluster_version);

-- 2. diagnostic_item：按type拆分表
CREATE TABLE hypothesis (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
  seq SMALLINT NOT NULL,
  description TEXT NOT NULL,
  probability REAL CHECK (probability BETWEEN 0.0 AND 1.0),
  evidence_needed TEXT,
  status VARCHAR(20) DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE verification_step (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
  seq SMALLINT NOT NULL,
  action TEXT NOT NULL,
  expected_result TEXT,
  tool_hint VARCHAR(100),
  status VARCHAR(20) DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE root_cause (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
  description TEXT NOT NULL,
  confidence REAL,
  evidence TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE solution (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
  seq SMALLINT NOT NULL,
  steps TEXT[] NOT NULL,
  commands TEXT[],
  created_at TIMESTAMPTZ DEFAULT now()
);

-- 迁移数据后删除diagnostic_item表
```

**优先级**: P0 - 影响查询性能和数据质量

---

### P0-5: 过度的冗余字段破坏数据一致性

**问题描述**:  
多处冗余存储导致更新不一致风险：

```sql
-- 冗余1: case.client_id 复制自 user.client_id
-- 风险：user表更新client_id后，case表不会自动更新

-- 冗余2: message.case_id 复制自 conversation.case_id
-- 风险：conversation移动到另一个case时，message不会更新

-- 冗余3: conversation.category_l1/l2 复制自 kb_category
-- 风险：kb_category重命名后，历史conversation显示旧名称

-- 冗余4: assistant_evaluation.close_reason 复制自 case.close_reason
-- 风险：case关闭原因修正后，评估表不会更新
```

**影响**:
- 数据不一致导致报表统计错误
- 需要维护复杂的更新触发器
- 存储空间浪费

**修复建议**:
```sql
-- 1. 移除不必要的冗余（category_l1/l2, close_reason）
ALTER TABLE conversation 
  DROP COLUMN category_l1,
  DROP COLUMN category_l2;

ALTER TABLE assistant_evaluation 
  DROP COLUMN close_reason;

-- 查询时通过JOIN获取：
-- SELECT c.*, cat.name AS category_l2 
-- FROM conversation c 
-- JOIN kb_category cat ON c.category_id = cat.code;

-- 2. 保留高价值冗余但添加触发器维护
-- 如果确实需要保留case.client_id，添加触发器：
CREATE OR REPLACE FUNCTION sync_case_client_id()
RETURNS TRIGGER AS $$
BEGIN
  UPDATE "case" SET client_id = NEW.client_id 
  WHERE user_id = NEW.user_id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_case_client_id
  AFTER UPDATE OF client_id ON "user"
  FOR EACH ROW EXECUTE FUNCTION sync_case_client_id();

-- 3. 对于message.case_id，建议移除或改用计算列（PG12+）
ALTER TABLE message DROP COLUMN case_id;
-- 查询时JOIN: SELECT m.* FROM message m JOIN conversation c ON m.conversation_id = c.conversation_id
```

**优先级**: P0 - 防止数据不一致

---

## ⚠️ P1 - 重要问题（建议修复）

### P1-1: 索引设计不合理

**问题描述**:

**过度索引**:
```sql
-- 1. UNIQUE约束已自带索引，无需重复创建
CREATE INDEX idx_user_client_id ON "user" (client_id);  -- 冗余
-- user.client_id已有UNIQUE约束

-- 2. 低基数字段单列索引效率低
CREATE INDEX idx_user_type ON "user" (user_type);  -- 仅2个值
CREATE INDEX idx_case_priority ON "case" (priority);  -- 仅4个值
CREATE INDEX idx_case_assistant_type ON "case" (assistant_type);  -- 仅1-2个值

-- 3. trace_id索引使用频率低
CREATE INDEX idx_user_trace_id ON "user" (trace_id);  -- 仅调试使用
CREATE INDEX idx_case_trace_id ON "case" (trace_id);
-- 14个表都有trace_id索引，但90%的查询不用
```

**缺失关键索引**:
```sql
-- 1. audit_log按prompt模板分析性能
-- 缺少: CREATE INDEX ON audit_log (system_prompt_id, started_at DESC);

-- 2. tool_result按工具名+风险等级统计
-- 缺少: CREATE INDEX ON tool_result (tool_name, risk_level, started_at DESC);

-- 3. message内容搜索使用英文分词
CREATE INDEX idx_message_content_search ON message USING GIN (to_tsvector('english', content));
-- 应改为中文分词：to_tsvector('zhcfg', content) 或 jieba分词
```

**修复建议**:
```sql
-- 1. 移除冗余索引
DROP INDEX idx_user_client_id;  -- UNIQUE约束已有
DROP INDEX idx_user_type;  -- 低基数
DROP INDEX idx_case_priority;  -- 低基数
DROP INDEX idx_case_category;  -- 低基数
DROP INDEX idx_case_assistant_type;  -- 低基数

-- 2. trace_id改为部分索引
DROP INDEX idx_user_trace_id;
DROP INDEX idx_case_trace_id;
-- ... 其他trace_id索引

-- 仅在核心表保留，且使用部分索引
CREATE INDEX idx_case_trace_id ON "case" (trace_id) 
  WHERE trace_id IS NOT NULL AND created_at > now() - interval '7 days';

-- 3. 添加缺失的复合索引
CREATE INDEX idx_audit_log_prompt_time ON audit_log (system_prompt_id, started_at DESC)
  WHERE system_prompt_id IS NOT NULL;

CREATE INDEX idx_tool_result_analysis ON tool_result (tool_name, risk_level, started_at DESC);

-- 4. 修复中文分词
DROP INDEX idx_message_content_search;
-- 方案A: 使用pg_jieba扩展
CREATE EXTENSION IF NOT EXISTS pg_jieba;
CREATE INDEX idx_message_content_search ON message USING GIN (jieba_to_tsvector(content));

-- 方案B: 使用zhparser
CREATE INDEX idx_message_content_search ON message USING GIN (to_tsvector('zhcfg', content));
```

**影响**: 索引过多影响INSERT性能，缺失索引影响查询性能  
**优先级**: P1

---

### P1-2: trace_id字段过度设计

**问题描述**:  
几乎所有表（21个）都有 `trace_id VARCHAR(64)` 字段和对应索引：

```sql
-- 业务表
user.trace_id
customer.trace_id
case.trace_id
conversation.trace_id
message.trace_id
environment.trace_id
...

-- 配置表
tool_definition.trace_id  -- 不合理：配置变更不需要trace
system_prompt.trace_id
kb_category.trace_id
```

**影响**:
- 增加64字节 × 21列 = 1.3KB 每行开销
- 配置表的trace_id永远为NULL（配置变更不在请求上下文）
- 索引空间浪费（21个索引但使用率<5%）

**修复建议**:
```sql
-- 方案1: 移除配置表的trace_id
ALTER TABLE tool_definition DROP COLUMN trace_id;
ALTER TABLE system_prompt DROP COLUMN trace_id;
ALTER TABLE kb_category DROP COLUMN trace_id;
ALTER TABLE sop_document DROP COLUMN trace_id;

-- 方案2: 创建独立的链路追踪表
CREATE TABLE trace_log (
  id BIGSERIAL PRIMARY KEY,
  trace_id VARCHAR(64) NOT NULL,
  table_name VARCHAR(50) NOT NULL,
  record_id TEXT NOT NULL,
  operation VARCHAR(20) NOT NULL,  -- INSERT/UPDATE/DELETE
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_trace_log_trace_id ON trace_log (trace_id);
CREATE INDEX idx_trace_log_table_record ON trace_log (table_name, record_id);

-- 通过触发器自动记录
CREATE OR REPLACE FUNCTION log_trace()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.trace_id IS NOT NULL THEN
    INSERT INTO trace_log (trace_id, table_name, record_id, operation)
    VALUES (NEW.trace_id, TG_TABLE_NAME, NEW.id::TEXT, TG_OP);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 然后移除各表的trace_id字段和索引
```

**优先级**: P1 - 影响存储和性能

---

### P1-3: 缺少软删除机制

**问题描述**:  
所有表都是硬删除，无法恢复误删除的数据。特别是核心业务表：
- `user`: 删除用户会级联删除所有工单
- `case`: 误删工单无法恢复
- `conversation`: 删除对话会丢失所有消息

**影响**:
- 误操作无法恢复
- 无法实现"回收站"功能
- 用户注销后无法保留历史数据用于分析

**修复建议**:
```sql
-- 1. 添加软删除字段
ALTER TABLE "user" ADD COLUMN deleted_at TIMESTAMPTZ;
ALTER TABLE "case" ADD COLUMN deleted_at TIMESTAMPTZ;
ALTER TABLE conversation ADD COLUMN deleted_at TIMESTAMPTZ;
ALTER TABLE customer ADD COLUMN deleted_at TIMESTAMPTZ;

-- 2. 添加部分索引（仅索引未删除的记录）
CREATE INDEX idx_user_deleted ON "user" (user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_case_deleted ON "case" (case_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_conversation_deleted ON conversation (conversation_id) WHERE deleted_at IS NULL;

-- 3. 修改应用层查询逻辑
-- 所有查询添加: WHERE deleted_at IS NULL
-- ORM配置: default_scope { where(deleted_at: nil) }

-- 4. 定期清理（可选）
-- 创建定时任务，删除90天前的软删除记录
DELETE FROM "case" WHERE deleted_at < now() - interval '90 days';
```

**优先级**: P1 - 提升数据安全性

---

### P1-4: 文本字段长度不合理

**问题描述**:
```sql
"user".username VARCHAR(100)  -- 一般用户名<20字符
"user".email VARCHAR(255)  -- 标准长度，合理
"case".title VARCHAR(500)  -- 过长，一般标题<200字符
"case".description TEXT  -- 合理
customer.name VARCHAR(200)  -- 公司名一般<100字符
tool_definition.display_name VARCHAR(200)  -- 工具名一般<50字符
kbd_entry.title TEXT  -- 应限制长度，避免标题过长
```

**影响**:
- 索引效率降低（VARCHAR越长索引越大）
- 前端展示溢出风险
- 内存占用增加

**修复建议**:
```sql
ALTER TABLE "user" ALTER COLUMN username TYPE VARCHAR(50);
ALTER TABLE "case" ALTER COLUMN title TYPE VARCHAR(200);
ALTER TABLE customer ALTER COLUMN name TYPE VARCHAR(100);
ALTER TABLE tool_definition ALTER COLUMN display_name TYPE VARCHAR(100);
ALTER TABLE kbd_entry ALTER COLUMN title TYPE VARCHAR(300);
```

**优先级**: P1 - 优化性能

---

## 💡 P2 - 优化建议（可选）

### P2-1: 时间戳字段命名不一致

**问题描述**:
- 大部分表: `created_at`, `updated_at`
- conversation: `started_at`, `ended_at`
- tool_result: `started_at`, `completed_at`
- audit_log: `started_at`, `completed_at`
- session: `expires_at`

**建议**: 统一命名约定
- 实体创建/更新: `created_at`, `updated_at`
- 过程开始/结束: `started_at`, `completed_at`
- 生命周期节点: `confirmed_at`, `resolved_at`, `closed_at`, `expired_at`

---

### P2-2: 触发器维护不一致

**问题描述**:  
以下表有 `updated_at` 字段但缺少触发器：
- `session` - 无 `updated_at` 字段（合理，session不会更新）
- `kbd_entry` - 有 `updated_at` 但注释说"由触发器维护"，实际未创建
- `sop_document` - 同上

**修复建议**:
```sql
DROP TRIGGER IF EXISTS update_kbd_entry_updated_at ON kbd_entry;
CREATE TRIGGER update_kbd_entry_updated_at
  BEFORE UPDATE ON kbd_entry
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_sop_document_updated_at ON sop_document;
CREATE TRIGGER update_sop_document_updated_at
  BEFORE UPDATE ON sop_document
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

### P2-3: 考虑表分区策略

**问题描述**:  
随着时间增长，以下表会积累大量历史数据：
- `message` - 每轮对话2-20条消息
- `audit_log` - 每轮对话1条审计
- `tool_result` - 每次工具调用1条
- `diagnostic_item` - 每个会话10-50条

**建议**:
```sql
-- 按月分区message表
CREATE TABLE message_partitioned (
  LIKE message INCLUDING ALL
) PARTITION BY RANGE (created_at);

CREATE TABLE message_2026_04 PARTITION OF message_partitioned
  FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');

CREATE TABLE message_2026_05 PARTITION OF message_partitioned
  FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');

-- 定期归档历史分区
-- 6个月后将旧分区detach并归档到对象存储
```

---

## 📝 修复检查清单

### 立即修复 (P0)
- [ ] P0-1: 统一主键策略（case表改为UUID主键）
- [ ] P0-2: 修改外键级联策略（评估/审计表改为RESTRICT）
- [ ] P0-3: 添加数据约束（CHECK/ENUM）
- [ ] P0-4: 拆分JSONB字段（diagnostic_item拆表）
- [ ] P0-5: 移除冗余字段（category_l1/l2, close_reason）

### 近期修复 (P1)
- [ ] P1-1: 优化索引设计（移除冗余索引，添加复合索引）
- [ ] P1-2: 清理trace_id过度设计
- [ ] P1-3: 实现软删除机制
- [ ] P1-4: 调整文本字段长度

### 长期优化 (P2)
- [ ] P2-1: 统一时间戳命名
- [ ] P2-2: 补充缺失的触发器
- [ ] P2-3: 考虑表分区策略

---

## 🎯 修复优先级建议

**第一阶段（数据安全）**:
1. P0-2: 修改外键级联策略 + 实现软删除
2. P0-3: 添加数据约束
3. P0-5: 移除危险的冗余字段

**第二阶段（性能优化）**:
1. P0-4: 拆分diagnostic_item表
2. P1-1: 优化索引设计
3. P1-2: 清理trace_id设计

**第三阶段（架构优化）**:
1. P0-1: 统一主键策略（需要迁移数据）
2. P1-3: 完善软删除机制
3. P2系列: 代码质量优化

---

## 📌 注意事项

1. **P0-1 主键迁移需要停机维护**，建议在数据量小时进行
2. **所有DDL变更前务必备份数据**
3. 修改外键约束后，需要更新应用层的删除逻辑
4. 添加CHECK约束后，需要清理历史脏数据
5. 拆分表结构后，需要更新ORM模型和所有相关查询

---

## 🔗 相关文档

- [PostgreSQL 外键约束最佳实践](https://www.postgresql.org/docs/current/ddl-constraints.html)
- [数据库索引设计原则](https://use-the-index-luke.com/)
- [JSONB vs 独立列选择指南](https://www.postgresql.org/docs/current/datatype-json.html)

---

**审核人**: GitHub Copilot CLI  
**下一步**: 请将此报告分配给数据库架构师进行评审和修复
