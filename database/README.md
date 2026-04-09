# 数据库迁移管理

## 唯一权威工具：Atlas（自 v6.3 起）

本项目使用 [Atlas](https://atlasgo.io) 声明式管理数据库 Schema 版本。

> ⚠️ **dbmate 迁移链已于 2026-04-08 彻底废弃。**
> `database/migrations/` 下的文件仅作历史档案，不再被任何 K8s Job 执行。
> 所有 schema 变更必须：
> 1. 修改 `database/desired_schema.sql`（期望状态，唯一权威）
> 2. 运行 `atlas migrate diff` 生成迁移文件
> 3. 提交 PR，CI 自动验证

## 目录结构

```
database/
  desired_schema.sql           ← 期望 Schema（唯一权威，Atlas 声明式管理）
  atlas-migrations/            ← Atlas 迁移文件目录
    20260408000000_baseline.sql  ← baseline（接管时快照）
    atlas.sum                    ← 完整性校验文件（禁止手动修改）
  seeds/                       ← 业务种子数据（与迁移工具无关）
    01_tool_definitions.sql    ← 初始化 tool_definition 表
    02_system_prompts.sql      ← 初始化 system_prompt 表
  README.md

docs/archive/db-migrations-history/
  migrations/                  ← 历史 dbmate 迁移文件（只读归档，v6.3 前）
```

## Atlas 工作流

### 修改 Schema（新增表/字段/索引）

```bash
# 1. 修改期望 schema（唯一权威入口）
vim database/desired_schema.sql

# 2. 生成迁移文件（需本地 postgres 容器运行）
export DATABASE_URL="postgres://postgres:postgres@localhost:5432/hci_dev?sslmode=disable"
atlas migrate diff --env local <migration-name>

# 3. 审查生成的迁移文件
cat database/atlas-migrations/<新文件>.sql

# 4. 提交（迁移文件 + desired_schema.sql 必须同一 commit）
git add database/desired_schema.sql database/atlas-migrations/
```

### 本地应用迁移

```bash
# 应用所有待执行迁移
atlas migrate apply --env local

# 查看迁移状态
atlas migrate status --env local

# 验证 schema 与 desired_schema.sql 一致
atlas schema diff --env local
```

### CI 自动验证

CI 流程自动执行：
1. `atlas migrate lint` — 检测破坏性变更
2. 全量执行 baseline + 所有迁移文件
3. 验证最终 schema 与 `desired_schema.sql` 一致

## 幂等性规范（强制）

所有迁移脚本**必须可安全重复执行**：

| 操作类型 | 要求 |
|----------|------|
| `CREATE TABLE` | 必须加 `IF NOT EXISTS` |
| `ALTER TABLE ADD COLUMN` | 必须加 `IF NOT EXISTS` |
| `CREATE INDEX` | 必须加 `IF NOT EXISTS` |
| `CREATE TRIGGER` | `DROP TRIGGER IF EXISTS` 后再创建 |
| `DROP TABLE` | 必须加 `IF EXISTS` |

## 铁律

1. **desired_schema.sql 是唯一权威** — 所有表结构以此为准，文档与代码以此校对
2. **已提交的 Atlas 迁移文件永远不修改** — 如需修订，新建迁移文件
3. **atlas.sum 禁止手动修改** — 由 `atlas migrate hash` 自动生成
4. **migrations/ 目录只读归档** — 禁止新增 dbmate 文件
5. **desired_schema.sql + atlas-migrations/ 必须同一 commit 提交**

## 多环境说明

- **全新 DB**（测试/本地）：`atlas migrate apply --env local`（从 baseline 开始全量执行）
- **已有 DB**（存量 dev/staging/prod）：`atlas migrate apply --env prod --baseline 20260408000000`（baseline 跳过，从后续迁移开始）
- **CI 环境**：`atlas migrate apply --env ci`（全量执行，每次 PR 验证）

> **历史说明**：`schema_migrations` 表为旧 dbmate 工具表（已废弃）。Atlas 使用 `atlas_schema_revisions` 表跟踪版本。

