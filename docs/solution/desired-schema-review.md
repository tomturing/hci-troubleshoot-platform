# desired_schema.sql 设计审核报告

> **审核日期**: 2026-04-08
> **审核文件**: `database/desired_schema.sql` (v6.2)
> **审核目的**: 发现设计缺陷、过度设计、类型选择问题，为后续修复提供依据

---

## 一、缺陷（必须修复）

### D-001: generate_case_id() 并发竞态风险 ⚠️ 高危

| 属性 | 值 |
|------|-----|
| **问题类型** | 缺陷 |
| **优先级** | P0 - 紧急 |
| **涉及对象** | 函数 `generate_case_id()` |
| **影响范围** | 工单创建 |

**问题描述**：
```sql
SELECT COUNT(*) + 1 INTO v_seq FROM "case"
    WHERE case_id LIKE 'Q' || v_today || '%';
```
在高并发场景下，两个请求可能同时读到相同 `COUNT(*)`，导致生成重复 `case_id`。

**修复建议**：
```sql
-- 方案 A：使用日期分区序列（推荐）
CREATE SEQUENCE case_id_seq DEFAULT 1;

CREATE OR REPLACE FUNCTION generate_case_id()
RETURNS VARCHAR(20) AS $$
DECLARE
    v_today VARCHAR(8);
    v_seq   INTEGER;
BEGIN
    v_today := TO_CHAR(CURRENT_DATE, 'YYYYMMDD');
    -- 使用 Advisory Lock 保证原子性
    SELECT pg_advisory_lock(v_today::bigint::int);
    SELECT COALESCE(MAX(CAST(SUBSTRING(case_id FROM 10 FOR 5) AS INTEGER)), 0) + 1
        INTO v_seq FROM "case"
        WHERE case_id LIKE 'Q' || v_today || '%';
    SELECT pg_advisory_unlock(v_today::bigint::int);
    RETURN 'Q' || v_today || LPAD(v_seq::TEXT, 5, '0');
END;
$$ LANGUAGE plpgsql;

-- 方案 B：在服务层使用 Redis 分布式锁生成序号
-- 方案 C：使用 PostgreSQL 事务级 SELECT ... FOR UPDATE 锁定当天最大 case_id
```

**状态**: 待修复

---

### D-002: 向量索引缺失 ⚠️ 性能隐患

| 属性 | 值 |
|------|-----|
| **问题类型** | 缺陷 |
| **优先级** | P0 - 紧急 |
| **涉及对象** | `kb_category.embedding`, `kbd_entry.embedding`, `sop_chunk.embedding` |
| **影响范围** | 知识库语义检索性能 |

**问题描述**：
三个表都定义了 `embedding vector(1536)` 字段，但未创建向量索引。无索引时向量检索是全表扫描，性能极差。

**修复建议**：
```sql
-- 使用 IVFFlat 索引（适合中等规模数据）
CREATE INDEX idx_kb_category_embedding ON kb_category
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX idx_kbd_entry_embedding ON kbd_entry
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX idx_sop_chunk_embedding ON sop_chunk
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- 或使用 HNSW（精度更高但内存开销更大，适合大规模数据）
-- CREATE INDEX idx_kb_category_embedding ON kb_category
--     USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

**注意**：
- IVFFlat 需要数据量 > `lists * 10` 才能有效工作（建议至少 1000 条记录）
- HNSW 对数据量无限制，但内存开销更大
- 生产环境应根据数据规模和查询频率选择合适的索引类型

**状态**: 待修复

---

### D-003: 全文检索语言配置错误

| 属性 | 值 |
|------|-----|
| **问题类型** | 缺陷 |
| **优先级** | P1 - 高 |
| **涉及对象** | `idx_message_content_search` |
| **影响范围** | 消息内容搜索 |

**问题描述**：
```sql
CREATE INDEX idx_message_content_search ON message
    USING GIN (to_tsvector('english', content));
```
`content` 字段存储中文排障对话内容，使用 `english` 配置无法正确分词。

**修复建议**：
```sql
-- 方案 A：使用 simple 配置（按空格/标点分词，无需额外扩展）
DROP INDEX IF EXISTS idx_message_content_search;
CREATE INDEX idx_message_content_search ON message
    USING GIN (to_tsvector('simple', content));

-- 方案 B：安装 zhparser 扩展（专业中文分词，推荐生产环境）
-- 1. 安装 zhparser 扩展（需要 superuser 权限）
CREATE EXTENSION IF NOT EXISTS zhparser;
CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l WITH simple;

-- 2. 创建索引
DROP INDEX IF EXISTS idx_message_content_search;
CREATE INDEX idx_message_content_search ON message
    USING GIN (to_tsvector('chinese', content));

-- 3. 同时更新 tsv 字段触发器（如果存在）
```

**状态**: 待修复

---

### D-004: diagnostic_item 缺少复合唯一约束

| 属性 | 值 |
|------|-----|
| **问题类型** | 缺陷 |
| **优先级** | P1 - 高 |
| **涉及对象** | `diagnostic_item` 表 |
| **影响范围** | 诊断条目数据一致性 |

**问题描述**：
当前无唯一约束，可能出现重复 `(conversation_id, type, seq)` 组合，导致 Prompt 构建时数据混乱。

**修复建议**：
```sql
ALTER TABLE diagnostic_item
    ADD CONSTRAINT uq_diagnostic_item_conv_type_seq
    UNIQUE (conversation_id, "type", seq);
```

**状态**: 待修复

---

### D-005: session 表 user_id 缺少外键约束

| 属性 | 值 |
|------|-----|
| **问题类型** | 缺陷 |
| **优先级** | P1 - 高 |
| **涉及对象** | `session` 表 |
| **影响范围** | 数据完整性 |

**问题描述**：
```sql
user_id uuid NOT NULL,  -- 没有 REFERENCES "user"
```
缺少外键约束可能导致孤儿记录。

**修复建议**：
```sql
ALTER TABLE session
    ADD CONSTRAINT fk_session_user_id
    FOREIGN KEY (user_id) REFERENCES "user" (user_id) ON DELETE CASCADE;
```

**状态**: 待修复

---

### D-006: CHECK 约束缺失

| 属性 | 值 |
|------|-----|
| **问题类型** | 缺陷 |
| **优先级** | P1 - 高 |
| **涉及对象** | 多表多字段 |
| **影响范围** | 数据有效性校验 |

**问题描述**：
以下字段缺少 CHECK 约束，可能写入非法值：

| 表 | 字段 | 需添加的 CHECK |
|---|------|---------------|
| `diagnostic_item` | `probability` | `CHECK (probability >= 0 AND probability <= 1)` |
| `diagnostic_item` | `stage` | `CHECK (stage IN ('S2','S3','S4','S5'))` |
| `conversation` | `diagnostic_stage` | `CHECK (diagnostic_stage IN ('S0','S1','S2','S3','S4','S5','S6'))` |
| `tool_result` | `risk_level` | `CHECK (risk_level >= 1 AND risk_level <= 3)` |
| `tool_definition` | `risk_level` | `CHECK (risk_level >= 1 AND risk_level <= 3)` |
| `assistant_evaluation` | `score` | `CHECK (score >= 1 AND score <= 5)` |
| `assistant_evaluation` | `composite_score` | `CHECK (composite_score >= 0 AND composite_score <= 100)` |

**修复建议**：
```sql
-- diagnostic_item
ALTER TABLE diagnostic_item ADD CONSTRAINT chk_diagnostic_item_probability
    CHECK (probability IS NULL OR (probability >= 0 AND probability <= 1));
ALTER TABLE diagnostic_item ADD CONSTRAINT chk_diagnostic_item_stage
    CHECK (stage IN ('S2','S3','S4','S5'));

-- conversation
ALTER TABLE conversation ADD CONSTRAINT chk_conversation_diagnostic_stage
    CHECK (diagnostic_stage IN ('S0','S1','S2','S3','S4','S5','S6'));

-- tool_result
ALTER TABLE tool_result ADD CONSTRAINT chk_tool_result_risk_level
    CHECK (risk_level IS NULL OR (risk_level >= 1 AND risk_level <= 3));

-- tool_definition
ALTER TABLE tool_definition ADD CONSTRAINT chk_tool_definition_risk_level
    CHECK (risk_level >= 1 AND risk_level <= 3);

-- assistant_evaluation
ALTER TABLE assistant_evaluation ADD CONSTRAINT chk_assistant_evaluation_score
    CHECK (score IS NULL OR (score >= 1 AND score <= 5));
ALTER TABLE assistant_evaluation ADD CONSTRAINT chk_assistant_evaluation_composite_score
    CHECK (composite_score IS NULL OR (composite_score >= 0 AND composite_score <= 100));
```

**状态**: 待修复

---

## 二、过度设计（建议精简）

### O-001: trace_id 索引泛滥

| 属性 | 值 |
|------|-----|
| **问题类型** | 过度设计 |
| **优先级** | P2 - 中 |
| **涉及对象** | 多表 trace_id 索引 |
| **影响范围** | 写入性能、存储空间 |

**问题描述**：
几乎所有表都有 `idx_xxx_trace_id` 索引，但：
- 链路追踪主要通过日志/Tempo 进行，数据库查询频率极低
- 大部分表的 trace_id 查询场景罕见
- 索引会增加写入开销和存储空间

**建议保留索引的表**：
- `case` - 核心业务表，可能需要按 trace_id 查工单
- `conversation` - 核心业务表
- `message` - 核心业务表

**建议移除索引的表**：
- `user`、`customer`、`environment`、`assistant_evaluation`、`session`、`tool_result`、`audit_log`、`diagnostic_item`

**修复建议**：
```sql
-- 移除非核心表的 trace_id 索引
DROP INDEX IF EXISTS idx_user_trace_id;
DROP INDEX IF EXISTS idx_customer_trace_id;
DROP INDEX IF EXISTS idx_environment_trace_id;
DROP INDEX IF EXISTS idx_eval_trace_id;
DROP INDEX IF EXISTS idx_session_trace_id;
-- DROP INDEX IF EXISTS idx_tool_result_trace_id;  -- 保留（有 WHERE 条件）
-- DROP INDEX IF EXISTS idx_audit_log_trace_id;   -- 保留（有 WHERE 条件）
```

**状态**: 待审核

---

### O-002: environment.env_data GIN 索引

| 属性 | 值 |
|------|-----|
| **问题类型** | 过度设计 |
| **优先级** | P2 - 中 |
| **涉及对象** | `idx_environment_data_gin` |
| **影响范围** | 写入性能、存储空间 |

**问题描述**：
```sql
CREATE INDEX idx_environment_data_gin ON environment USING GIN (env_data);
```
GIN 索引写入开销大、占用空间多。如果实际业务不查询 JSONB 内容，此索引是浪费。

**修复建议**：
```sql
-- 确认业务需求后决定是否移除
-- 如果不需要查询 env_data 内容，移除索引：
DROP INDEX IF EXISTS idx_environment_data_gin;
```

**状态**: 待审核（需确认业务需求）

---

### O-003: assistant_evaluation 冗余字段

| 属性 | 值 |
|------|-----|
| **问题类型** | 过度设计 |
| **优先级** | P2 - 中 |
| **涉及对象** | `assistant_evaluation` 表 |
| **影响范围** | 数据一致性 |

**问题描述**：
以下字段是冗余复制，存在数据一致性风险：
- `close_reason` ← 冗余自 `case.close_reason`
- `session_duration_sec` ← 冗余自 `conversation.ended_at - conversation.started_at`
- `repeat_question_count` ← 冗余自 `conversation.repeat_question_count`

**风险**：
- 源表更新时冗余字段可能不一致
- 需要额外代码维护同步逻辑

**建议**：
1. **保留现状**：如果业务确实需要避免 JOIN（如高频查询场景），确认同步逻辑完善
2. **移除冗余**：通过视图或 JOIN 查询获取
3. **使用触发器同步**：在源表变更时自动更新冗余字段

**状态**: 待审核（需确认业务需求）

---

### O-004: 单字段索引可合并

| 属性 | 值 |
|------|-----|
| **问题类型** | 过度设计 |
| **优先级** | P3 - 低 |
| **涉及对象** | 多表单字段索引 |
| **影响范围** | 索引维护开销 |

**问题描述**：
部分表的单字段索引可合并为复合索引以减少开销：

| 表 | 可合并的索引 | 合并后 |
|---|-------------|--------|
| `case` | `idx_case_status` + `idx_case_created_at` | `idx_case_status_created_at (status, created_at DESC)` |
| `conversation` | `idx_conversation_started_at` + `idx_conversation_case_id` | 已有 `idx_conversation_case_started`，可移除单字段索引 |

**修复建议**：
```sql
-- case 表：创建复合索引后移除单字段索引
CREATE INDEX idx_case_status_created_at ON "case" (status, created_at DESC);
DROP INDEX IF EXISTS idx_case_status;
-- idx_case_created_at 保留（可能用于全表时间排序）

-- conversation 表：已有复合索引，移除冗余单字段索引
DROP INDEX IF EXISTS idx_conversation_started_at;
```

**状态**: 待审核

---

## 三、类型选择建议

### T-001: 应使用 ENUM 的字段

| 属性 | 值 |
|------|-----|
| **问题类型** | 类型选择 |
| **优先级** | P3 - 低 |
| **涉及对象** | 多表多字段 |
| **影响范围** | 数据一致性、存储效率 |

**问题描述**：
以下字段使用 VARCHAR 存储枚举值，建议改为 ENUM 类型：

| 表 | 字段 | 当前类型 | 建议改为 |
|---|------|---------|----------|
| `case` | `priority` | VARCHAR(20) | `case_priority ENUM ('low','medium','high','urgent')` |
| `diagnostic_item` | `type` | VARCHAR(30) | `diagnostic_item_type ENUM ('hypothesis','verification_step','root_cause','solution')` |
| `diagnostic_item` | `status` | VARCHAR(20) | `diagnostic_item_status ENUM ('pending','in_progress','confirmed','rejected','skipped')` |
| `audit_log` | `audit_type` | VARCHAR(20) | ENUM 类型 |

**注意**：ENUM 类型变更需要特殊处理（ALTER TYPE ... ADD VALUE），迁移成本较高。

**建议**：当前版本可暂不修改，记录为技术债，后续版本统一整改。

**状态**: 待审核（技术债记录）

---

### T-002: 字段长度不合理

| 属性 | 值 |
|------|-----|
| **问题类型** | 类型选择 |
| **优先级** | P3 - 低 |
| **涉及对象** | 多表多字段 |
| **影响范围** | 存储效率 |

**问题描述**：

| 表 | 字段 | 当前长度 | 建议调整 | 原因 |
|---|------|---------|---------|------|
| `user` | `client_id` | VARCHAR(255) | VARCHAR(36) | UUID v4 格式固定 36 字符 |
| `tool_result` | `id` | VARCHAR(36) | UUID 类型 | 应使用 gen_random_uuid() |
| `audit_log` | `id` | VARCHAR(36) | UUID 类型 | 同上 |

**修复建议**：
```sql
-- 此修改涉及现有数据和 ORM 模型，需配合迁移脚本
-- 建议记录为技术债，后续版本整改
```

**状态**: 待审核（技术债记录）

---

## 四、设计考虑（需讨论）

### C-001: 级联删除策略

| 属性 | 值 |
|------|-----|
| **问题类型** | 设计考虑 |
| **优先级** | P2 - 中 |
| **涉及对象** | 外键约束 ON DELETE CASCADE |
| **影响范围** | 数据安全、合规 |

**问题描述**：
当前设计：`user → case → conversation → message/tool_result/diagnostic_item` 全部 `ON DELETE CASCADE`。

**风险**：删除用户会永久删除所有工单数据，可能不符合审计/合规需求。

**建议讨论**：
1. **软删除方案**：添加 `is_deleted` 标记，不物理删除
2. **归档方案**：改为 `ON DELETE SET NULL` + 定期归档到历史表
3. **保留现状**：如果业务确认不需要审计追溯

**状态**: 待讨论

---

### C-002: message.command / command_warning 设计

| 属性 | 值 |
|------|-----|
| **问题类型** | 设计考虑 |
| **优先级** | P3 - 低 |
| **涉及对象** | `message` 表 |
| **影响范围** | 数据模型清晰度 |

**问题描述**：
```sql
command text,          -- 仅 role='command' 时有值
command_warning text,  -- 仅 role='command' 时有值
```
这两个字段只在 `role='command'` 时有值，其他角色为 NULL。

**建议讨论**：
1. **保留现状**：如果业务场景简单，当前设计可接受
2. **拆分子表**：创建 `command_message` 子表，存储命令专属字段
3. **使用 JSONB**：将 command 相关信息合并到 `metadata` 字段

**状态**: 待讨论

---

### C-003: updated_at 触发器的必要性

| 属性 | 值 |
|------|-----|
| **问题类型** | 设计考虑 |
| **优先级** | P3 - 低 |
| **涉及对象** | 多表 updated_at 触发器 |
| **影响范围** | 性能开销 |

**问题描述**：
当前所有表都有 `updated_at` 触发器，但：
- 部分表更新频率极低（如 `kb_category`、`kbd_entry`）
- 触发器有性能开销

**建议讨论**：
1. **保留现状**：统一管理，代码简洁
2. **按需移除**：对低频更新表移除触发器，在服务层手动维护

**状态**: 待讨论

---

## 五、修复执行建议

### 修复顺序

| 优先级 | 问题编号 | 建议执行顺序 |
|--------|----------|-------------|
| P0 | D-001, D-002 | 1-2（紧急） |
| P1 | D-003, D-004, D-005, D-006 | 3-6（高） |
| P2 | O-001, O-002, O-003, O-004, C-001 | 7-11（中） |
| P3 | T-001, T-002, C-002, C-003 | 12-15（低，技术债） |

### 迁移注意事项

1. **P0/P1 缺陷修复**需要生成 Atlas 迁移文件
2. **索引修改**应在低负载时段执行（GIN/向量索引创建耗时较长）
3. **ENUM 类型变更**需要特别处理，建议独立迁移
4. **外键约束添加**需先清理孤儿数据

---

## 六、状态跟踪

| 编号 | 类型 | 优先级 | 状态 | 审核人 | 修复人 | 备注 |
|------|------|--------|------|--------|--------|------|
| D-001 | 缺陷 | P0 | 待修复 | - | - | 并发竞态 |
| D-002 | 缺陷 | P0 | 待修复 | - | - | 向量索引 |
| D-003 | 缺陷 | P1 | 待修复 | - | - | 全文检索 |
| D-004 | 缺陷 | P1 | 待修复 | - | - | 唯一约束 |
| D-005 | 缺陷 | P1 | 待修复 | - | - | 外键约束 |
| D-006 | 缺陷 | P1 | 待修复 | - | - | CHECK 约束 |
| O-001 | 过度设计 | P2 | 待审核 | - | - | trace_id 索引 |
| O-002 | 过度设计 | P2 | 待审核 | - | - | GIN 索引 |
| O-003 | 过度设计 | P2 | 待审核 | - | - | 冗余字段 |
| O-004 | 过度设计 | P3 | 待审核 | - | - | 索引合并 |
| T-001 | 类型选择 | P3 | 待审核 | - | - | ENUM |
| T-002 | 类型选择 | P3 | 待审核 | - | - | 字段长度 |
| C-001 | 设计考虑 | P2 | 待讨论 | - | - | 级联删除 |
| C-002 | 设计考虑 | P3 | 待讨论 | - | - | command 字段 |
| C-003 | 设计考虑 | P3 | 待讨论 | - | - | 触发器 |

---

## 附录：问题编号规则

- **D-xxx**: 缺陷（Defect）- 必须修复
- **O-xxx**: 过度设计（Over-design）- 建议精简
- **T-xxx**: 类型选择（Type）- 建议改进
- **C-xxx**: 设计考虑（Consideration）- 需讨论决策