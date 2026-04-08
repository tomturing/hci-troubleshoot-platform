# 数据库迁移管理

## 唯一权威工具：dbmate

本项目使用 [dbmate](https://github.com/amacneil/dbmate) 管理数据库 Schema 版本。

> ⚠️ **Alembic 迁移链已于 2026-04-08 彻底废弃。**
> `backend/shared/migrations/versions/` 下的文件仅作历史档案，不再被任何 K8s Job 执行。
> 所有 schema 变更必须通过 `database/migrations/` 下的 dbmate 文件进行。

## 目录结构

```
database/
  migrations/              ← 版本化迁移文件（只追加，禁止修改已提交文件）
    20260305001_init_schema.sql
    20260312001_kb_rag_v3.sql
    ...
  seeds/                   ← 存量环境引导脚本（仅首次接入 dbmate 时执行一次）
    00_baseline.sql        ← 将历史迁移标记为已执行
    01_tool_definitions.sql
    02_system_prompts.sql
  README.md
```

## 命名规范（强制）

```
YYYYMMDDNNN_描述.sql
```

- `YYYYMMDD`：变更日期（8位）
- `NNN`：当天序号（001 起，**与日期之间无下划线**）
- 描述：简短英文，下划线分隔

**⚠️ 禁止在 YYYYMMDD 和 NNN 之间插入下划线**。dbmate 以文件名开头纯数字为 version 主键，
`20260312_001_xxx.sql` 和 `20260312_002_xxx.sql` 会产生相同 version `20260312`，
导致第二个文件被静默跳过（相关避坑索引见 [`docs/deploy/pitfalls/_index.md`](../docs/deploy/pitfalls/_index.md)）。

**⚠️ 同一天的序号必须唯一**。两个文件共享 version 时 dbmate 只执行字典序更早的文件，
另一个文件的 DDL 永远不会执行，且不会有任何错误提示。

## 迁移脚本幂等性规范（强制）

所有迁移脚本**必须可安全重复执行**：

| 操作类型 | 要求 |
|----------|------|
| `CREATE TABLE` | 必须加 `IF NOT EXISTS` |
| `ALTER TABLE ADD COLUMN` | 必须加 `IF NOT EXISTS` |
| `CREATE INDEX` | 必须加 `IF NOT EXISTS` |
| `CREATE TRIGGER` | 必须用 `DO $$ IF NOT EXISTS` 块包裹 |
| `INSERT ... SELECT FROM 其他表` | **必须**用 `DO $$ IF EXISTS (SELECT ... tablename=xxx)` 包裹后再执行 |
| `DROP TABLE` | 必须加 `IF EXISTS` |

**错误示例（会导致迁移链断裂）：**
```sql
-- ❌ 直接引用，没有防御性检查
INSERT INTO audit_log SELECT * FROM tool_audit_log;
```

**正确示例（幂等，表不存在时静默跳过）：**
```sql
-- ✅ 先检查源表是否存在
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='tool_audit_log') THEN
        INSERT INTO audit_log SELECT ... FROM tool_audit_log ON CONFLICT (id) DO NOTHING;
    END IF;
END $$;
```

## 日常工作流

### 新增迁移文件

```bash
# 1. 创建迁移文件（手动命名，遵循规范）
# 文件名格式：YYYYMMDDNNN_描述.sql

# 2. 编写 SQL（migrate:up + migrate:down 两个块）

# 3. 同步到 ConfigMap（必须！否则 K8s 不会执行）
make db-sync

# 4. 检查同步状态 + version 唯一性
make db-check

# 5. 将迁移文件 + ConfigMap 纳入同一 commit 提交
git add database/migrations/ deploy/helm/hci-platform/templates/hooks/db-migrations-configmap.yaml
```

### 本地验证

```bash
# 执行所有未运行的迁移
dbmate --url "postgresql://hci_admin:dev_password_123@localhost:5432/hci_troubleshoot?sslmode=disable" up

# 查看迁移状态
dbmate --url "..." status
```

## 铁律

1. **已提交的 migration 文件永远不修改** — 如需修订，新建文件
2. **每个文件必须幂等** — 参考上方"幂等性规范"
3. **新增迁移后必须运行 `make db-sync`** — ConfigMap 和迁移文件必须在同一 commit 提交
4. **version 号必须唯一** — CI `check-db-migrations-sync` job 会自动检测重复 version
5. **Alembic 迁移链已废弃** — 禁止在 `backend/shared/migrations/versions/` 下新增文件

## 多环境说明

各环境通过 ArgoCD PreSync Hook 自动执行 dbmate：
- dbmate 内部维护 `schema_migrations` 表，记录每个文件是否已执行
- 同一文件在同一环境**只执行一次**
- 新环境从零开始则顺序全部执行

**已有数据库的环境（存量引导）**：
执行 `database/seeds/00_baseline.sql` 一次，将历史迁移标记为已执行。

> **注意**：`schema_migrations` 表只有 `version` 列（dbmate 原生结构，无 `ts` 列）。
