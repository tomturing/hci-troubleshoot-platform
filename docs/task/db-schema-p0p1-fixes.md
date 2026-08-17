# 数据库 Schema P0/P1 缺陷修复记录

## 变更概述

本文档记录 `database/atlas-migrations/20260817000000_fix_p0p1_schema_defects.sql`
和 `database/hci-sim-migrations/000002_fix_p0p1_defects.sql` 的设计修复内容。

修复方法论：**第一性原理 + 对抗性审查**（追溯到底层逻辑约束，像红队一样验证每个修复的边界条件）。

---

## 主库修复（`database/atlas-migrations/20260817000000_fix_p0p1_schema_defects.sql`）

### P0-1：`terminal_operation` / `bridge_execution_logs` `case_id` 格式无约束

| 项目 | 修复前 | 修复后 |
|------|--------|--------|
| `terminal_operation.case_id` 类型 | `varchar(32)`，无格式约束 | 添加 CHECK `^Q[0-9]{8}[0-9]{5}$` |
| `bridge_execution_logs.case_id` | `varchar(32)`，无格式约束 | 添加 CHECK（允许 NULL，有值须符合格式） |

**对抗性验证**：存量数据若含非标准格式的 `case_id` 会导致约束失败。迁移在 `DO $$ ... IF NOT EXISTS $$` 中添加，不清除数据，仅对新写入强制约束。

---

### P0-2：`authorization` 表缺失安全审计字段

| 字段/约束 | 修复前 | 修复后 |
|----------|--------|--------|
| `trace_id` | 缺失 | 添加 `varchar(64)`，允许 NULL（历史兼容） |
| `updated_at` | 缺失 | 添加 `timestamptz DEFAULT now()` |
| `decision` 约束 | 无约束 | 添加 `CHECK (decision IN ('approve', 'deny'))` |
| 索引 | 无 | 添加 `idx_authorization_exec_id` 和 `idx_authorization_trace_id` |

**风险说明**：高危授权表无 `trace_id` 意味着无法通过链路追踪定位授权操作的发起方，违反安全审计不可抵赖性要求。

---

### P0-3：`fact` 表缺失 `case` 外键

| 项目 | 修复前 | 修复后 |
|------|--------|--------|
| `fact.case_id` 约束 | 无外键 | `FOREIGN KEY → case(case_id) ON DELETE CASCADE` |
| 孤儿数据处理 | 无 | 迁移中先清理孤儿记录，再添加 FK |

---

### P0-4：`claim_evidence_link` 表缺失 `case` 外键

| 项目 | 修复前 | 修复后 |
|------|--------|--------|
| `claim_evidence_link.case_id` | 无外键 | `FOREIGN KEY → case(case_id) ON DELETE CASCADE` |

---

### P1-1：`kbd_entry.lock_version` 默认值不一致

| 项目 | 修复前 | 修复后 |
|------|--------|--------|
| `lock_version DEFAULT` | `0` | `1`（与 `collection_profile_definition` 等一致） |
| `lock_version CHECK` | 无 | `CHECK (lock_version >= 1)` |
| `ORM Model 默认值` | `default=0` | `default=1` (`backend/kb-service/app/models/kbd_entry.py`) |
| 存量数据 | `lock_version = 0` 的记录 | `UPDATE SET lock_version = 1 WHERE lock_version = 0` |
| 集成测试对齐 | 硬编码 `lock_version=0` | 同步更新为 `1` (`test_kbd_diagnosis_samples_postgres.py`、`test_kbd_sync.py`) |

---

### P1-2：`bridge_execution_logs` stdout/stderr 无大小上限

| 约束 | 内容 |
|------|------|
| `output_preview` | `length(output_preview) <= 2000` 字符 |
| `stdout` / `stderr` | `pg_column_size <= 512 KiB / 128 KiB` |

**k3s 资源保护**：k3s 受限节点上，无大小约束的 `text` 字段存储大量 SSH 命令输出会导致 PG OOM。全量输出应存 `bridge_execution_artifacts` 表。

---

### 冗余索引清理

| 索引 | 清理原因 |
|------|---------|
| `idx_case_client_id` | 被 `idx_case_client_status(client_id, status)` 前缀覆盖 |
| `idx_message_case_id` | 被 `idx_message_case_created(case_id, created_at)` 前缀覆盖 |
| `idx_kb_category_keywords` | `keywords` 字段已废弃，GIN 索引是纯写入开销 |

---

## hci-sim 库修复（`database/hci-sim-migrations/000002_fix_p0p1_defects.sql`）

### H-P0-3：`artifact.scan` CHECK 约束阻止记录扫描失败

| 项目 | 修复前 | 修复后 |
|------|--------|--------|
| `scan_passed` 约束 | `CHECK (secret_scan_passed AND pii_scan_passed AND ...)` | 移除约束 |
| 审计控制 | 数据库层阻止失败记录写入 | 改由 `artifact.metadata.status` 状态机控制 |

**对抗性分析**：原约束完全阻止写入失败的扫描记录，导致"为什么 artifact 被拒绝"无法从数据库中溯源，破坏审计不可变性原则。

---

### H-P0-4：`fixture.approval` 角色唯一约束阻止重审

| 项目 | 修复前 | 修复后 |
|------|--------|--------|
| `approval_role` 约束 | `UNIQUE (bundle_id, stage, actor_role)` | 移除唯一约束 |
| 最新审批查询 | 约束强制唯一 | 添加 `idx_fixture_approval_latest` 索引，应用层 `ORDER BY decided_at DESC LIMIT 1` |

---

### H-P1-1：`fixture.approval` 缺失 `trace_id`

- 补充 `trace_id varchar(64)` 字段，与 `artifact.approval.trace_id` 对齐
- 历史记录允许为 NULL

---

## 验证方法

```bash
# 主库迁移验证
make migrate-status  # 或 atlas migrate status --env local

# hci-sim 迁移验证
psql "$HCI_SIM_DATABASE_URL" -f database/hci-sim-migrations/000002_fix_p0p1_defects.sql

# 约束验证（主库）
psql "$DATABASE_URL" -c "\d terminal_operation"
psql "$DATABASE_URL" -c "\d authorization"
psql "$DATABASE_URL" -c "\d fact"
```
