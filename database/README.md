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
  desired_extras.sql            ← 函数/触发器（psql 幂等执行）
  data-migrations/              ← 版本化数据迁移（新增，详见下文）
    001_xxx.sql
    002_yyy.sql
  atlas-migrations/             ← Atlas 迁移文件目录（仅 Schema）
    20260408000000_baseline.sql ← baseline（接管时快照）
    atlas.sum                   ← 完整性校验文件（禁止手动修改）
  seeds/                        ← 业务种子数据（与迁移工具无关）
    01_tool_definitions.sql     ← 初始化 tool_definition 表
    02_system_prompts.sql       ← 初始化 system_prompt 表
    03_skill_definitions.sql    ← 初始化 skill_definition 表

docs/archive/db-migrations-history/
  migrations/                   ← 历史 dbmate 迁移文件（只读归档，v6.3 前）
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

1. **平台库 desired_schema.sql 是唯一权威** — `hci_troubleshoot` 表结构以此为准；`hci_sim` 只认 `hci-sim-migrations/`，两者不可交叉扫描
2. **已提交的 Atlas 迁移文件永远不修改** — 如需修订，新建迁移文件
3. **atlas.sum 禁止手动修改** — 由 `atlas migrate hash` 自动生成
4. **migrations/ 目录只读归档** — 禁止新增 dbmate 文件
5. **desired_schema.sql + atlas-migrations/ 必须同一 commit 提交**

## 多环境说明

- **全新 DB**（测试/本地）：`atlas migrate apply --env local`（从 baseline 开始全量执行）
- **已有 DB**（存量 dev/staging/prod）：`atlas migrate apply --env prod --baseline 20260408000000`（baseline 跳过，从后续迁移开始）
- **CI 环境**：`atlas migrate apply --env ci`（全量执行，每次 PR 验证）

## hci-sim 控制面 Schema（阶段 C/D）

`hci_sim` 数据库的控制面 metadata 按 `control_plane`、`fixture`、`artifact`、`audit` schema 管理：Scenario、不可变 Fixture Bundle、依赖、provenance、审批、审计、TestRun、Attempt、Event、Result 和 Runtime capability。它们只保存精确 revision、受控对象 URI、digest/哈希、状态与审计关联；**禁止**保存原始客户 Artifact、任意外部 URL 或可重放的 Lease 明文。真实 Artifact 进入具备审批、版本与保留策略的对象存储，Runtime 只能读取 `published` Bundle。

当前主库中的 `public.agent_test_*` 是迁移前存量兼容源，复制脚本默认只 inventory；完成 copy/verify/switch 和观察窗口前不得 DROP。独立迁移入口为 `database/hci-sim-migrations/000001_control_plane.sql`，不由平台 Atlas Job 执行。

> **历史说明**：`schema_migrations` 表为旧 dbmate 工具表（已废弃）。Atlas 使用 `atlas_schema_revisions` 表跟踪版本。

---

## 业务种子数据说明（Seeds）

业务种子数据存放在 `database/seeds/` 目录中，用于在 Admin UI 中初始化工具管理、Prompt管理和技能管理页面。通过 Helm 部署时，`db-seed-job` 会作为 PostSync Hook 在数据库迁移完成后自动加载这些 SQL 文件。

### 1. 幂等性与覆盖策略

为保护不同环境及用户在管理页面的自定义配置，种子文件遵循以下差异化幂等设计：

| 种子数据文件 | 表名称 | 冲突处理策略 | 覆盖行为说明 |
| :--- | :--- | :--- | :--- |
| `01_tool_definitions.sql` | `tool_definition` | `ON CONFLICT (tool_name) DO UPDATE` | **会被强行覆盖**。工具定义参数与后端 Python 代码严格绑定，必须强制保持一致。 |
| `02_system_prompts.sql` | `system_prompt` | `ON CONFLICT (name) DO NOTHING` | **不会覆盖已有修改**。保护用户在界面微调或自定义的 Prompt 不被冲掉。 |
| `03_skill_definitions.sql` | `skill_definition` | `ON CONFLICT (name) DO NOTHING` | **不会覆盖已有修改**。保护用户自定义技能规则不被重置。 |

### 2. 手动全量强制更新方法

如果需要丢弃本地或 Staging 环境的已有自定义数据，强制将数据库中的工具、技能和 Prompt 刷新为与最新代码种子文件一致的版本，可使用以下步骤：

1. **清空旧数据**（注意：这会删除所有自定义修改及审计日志）：
   ```bash
   # 在 Kubernetes 中执行
   kubectl exec -i -n hci-dev postgres-0 -- psql -U hci_admin -d hci_troubleshoot -c "TRUNCATE TABLE tool_definition, skill_definition, system_prompt CASCADE;"
   ```
2. **重新导入种子数据**：
   ```bash
   kubectl exec -i -n hci-dev postgres-0 -- psql -U hci_admin -d hci_troubleshoot < database/seeds/01_tool_definitions.sql
   kubectl exec -i -n hci-dev postgres-0 -- psql -U hci_admin -d hci_troubleshoot < database/seeds/02_system_prompts.sql
   kubectl exec -i -n hci-dev postgres-0 -- psql -U hci_admin -d hci_troubleshoot < database/seeds/03_skill_definitions.sql
   ```

---

## 数据迁移（Data Migration）

> 设计文档：`docs/solution/database/数据迁移设计方案.md`

### 背景

业务演进过程中需要执行数据层面的变更，如：
- 初始化数据补充
- 历史数据修复
- 字段数据回填
- 数据格式转换

这类变更属于 **Data Migration（DML）**，与 Schema Migration（DDL）分离管理。

### 目录结构

```
database/data-migrations/
  001_update_signals_prompt_stage.sql
  002_xxx.sql
  ...
```

### 命名规范

格式：`{version}_{description}.sql`

- `version`：三位数字，永远递增（001, 002, 003...）
- `description`：简短描述，使用下划线分隔

### 幂等性规范（强制）

所有数据迁移脚本**必须可安全重复执行**：

```sql
-- ❌ 错误：第二次执行失败
INSERT INTO config VALUES ('feature_x', 'true');

-- ✅ 正确：幂等
INSERT INTO config (key, value)
VALUES ('feature_x', 'true')
ON CONFLICT(key) DO NOTHING;
```

```sql
-- ✅ UPDATE 必须有 WHERE 条件
UPDATE system_prompt
SET stage = 'KEY'
WHERE name = 'kbd_extract_signals_v1'
  AND stage = 'KBD';
```

### 执行机制

数据迁移通过 `migration-runner.sh` 在 db-migrate Job 中执行：

1. 启动时检查 `migration_history` 表
2. 扫描 `data-migrations/` 目录
3. 按版本号顺序执行未执行的迁移
4. 记录执行历史（version, checksum, executed_at）

### migration_history 表

```sql
CREATE TABLE IF NOT EXISTS migration_history (
    version VARCHAR(100) PRIMARY KEY,
    checksum VARCHAR(64),
    description VARCHAR(255),
    executed_at TIMESTAMP DEFAULT NOW(),
    execution_time_ms INTEGER
);
```

### 开发流程

1. **新增数据迁移**：在 `database/data-migrations/` 下新建文件
2. **本地测试**：`psql -f database/data-migrations/xxx.sql`
3. **提交 PR**：CI 自动验证
4. **合并后自动执行**：ArgoCD PreSync Hook

### 注意事项

- 禁止修改已执行的迁移文件
- 新需求必须新增文件
- 大数据量迁移需分批处理
- 生产问题采用 Forward Fix，不回滚
