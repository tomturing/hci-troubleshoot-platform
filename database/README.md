# 数据库迁移管理

## 管理模式：Atlas 声明式（无迁移版本链）

本项目使用 [Atlas](https://atlasgo.io) **声明式**管理数据库 Schema：
`database/desired_schema.sql` 描述期望状态，`atlas schema apply` 计算差量并收敛。

> ⚠️ **本项目不存在 Schema 迁移版本链。**
> `atlas.hcl` 的三个 env 均为 `src = "file://database/desired_schema.sql"`，
> 未配置 `migration_dir`，因此 `atlas migrate diff/apply/lint/status` 等增量模式命令
> **对本项目无效**。`database/atlas-migrations/` 已于 2026-10-24 整体删除（历史见下）。
>
> 所有 Schema 变更只需两步：
> 1. 修改 `database/desired_schema.sql`（期望状态，唯一权威）
> 2. 提交 PR，CI 自动验证（`db-migration-test.yml`）

### 变更历史

| 日期 | 事件 |
|---|---|
| 2026-04-08 | dbmate 增量迁移链废弃，切换 Atlas 声明式 |
| 2026-04-09 | 首次删除 `atlas-migrations/`（增量模式遗留死代码） |
| 2026-05-25 起 | 该目录按本文档旧版「Atlas 工作流」说明被重新创建并累积 46 个文件 |
| 2026-10-24 | 二次删除 `atlas-migrations/`（47 文件），并重写本文档以根断再生 |

删除依据：`Dockerfile.migrations`、`scripts/db-migrate.sh`、`db-migration-test.yml`、
`atlas.hcl` 四处均**不执行**该目录内 SQL；其唯一效果是触发镜像构建与 CI 验证的
**虚假信号**（改了会触发、构建了却不生效）。变更记录已由 Git 历史承载，无需另存副本。

## 目录结构

```
database/
  desired_schema.sql           ← 期望 Schema（唯一权威，Atlas 声明式管理）
  desired_extras.sql           ← 函数/触发器（psql 幂等执行）
  data-migrations/             ← 版本化数据迁移（DML，详见下文）
    001_xxx.sql
    002_yyy.sql
    review_required/           ← 需专家签字后手工应用的迁移（runner 不扫描）
  hci-sim-migrations/          ← hci_sim 独立库 Schema 入口（与主库互不扫描）
  seeds/                       ← 业务种子数据（与迁移工具无关）
    01_tool_definitions.sql    ← 初始化 tool_definition 表
    02_system_prompts.sql      ← 初始化 system_prompt 表
    03_skill_definitions.sql   ← 初始化 skill_definition 表
    04_kbd_diagnosis_samples.sql ← 显式标记的诊断测试 KBD 草稿

docs/archive/db-migrations-history/
  migrations/                  ← 历史 dbmate 迁移文件（只读归档，v6.3 前）
```

> 无 `atlas-migrations/`：本项目不维护 Schema 迁移版本链，见上文「变更历史」。

## Atlas 工作流（声明式）

### 修改 Schema（新增表/字段/索引）

```bash
# 1. 修改期望 schema（唯一权威入口）
vim database/desired_schema.sql

# 2. 本地预览与当前 DB 的差量（不写入）
export DATABASE_URL="postgres://postgres:postgres@localhost:5432/hci_dev?sslmode=disable"
export DEV_URL="postgres://postgres:postgres@localhost:5432/atlas_dev?sslmode=disable"
atlas schema diff --env local

# 3. 应用到本地 DB
atlas schema apply --env local --auto-approve

# 4. 提交（只改 desired_schema.sql，无需生成迁移文件）
git add database/desired_schema.sql
```

> `desired_schema.sql` 定义表/列/索引/约束；`desired_extras.sql` 定义函数与触发器。
> **两者必须同步修改** —— db-migrate 链路是 `extras → schema apply → extras`，
> 只在其中一处修改会导致 Atlas 幂等验证报「apply 后仍存在 diff」。

### 部署链路实际执行顺序

`scripts/db-migrate.sh`（`Dockerfile.migrations` 打包为迁移镜像）：

| 步骤 | 动作 | 输入 |
|---|---|---|
| Step 0 | 空库引导（仅全新库执行） | `desired_schema.sql` |
| Step 1 | 数据迁移（migration-runner，版本化幂等） | `data-migrations/*.sql` |
| Step 2 | 应用函数 | `desired_extras.sql` |
| Step 3 | 声明式 Schema 收敛 | `desired_schema.sql` |
| Step 4 | 重建触发器 | `desired_extras.sql`（再次全量） |

`Dockerfile.migrations` 只 COPY 上述四个输入 + 两个脚本，**不打包任何迁移版本目录**。

> **`data-migrations/review_required/` 不会被自动执行**：`migration-runner.sh` 只匹配
> `/data-migrations/*.sql` 顶层 glob，子目录不在扫描范围内。该目录存放**不可自动回退**
> 的业务数据重分类/重挂载迁移，必须由领域专家逐条核对后手工执行：
> `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f <file>.sql`。
>
> 当前待签字项：`042`（分组节点上的 KBD 按关键词逐条重挂到叶子，并修正「虚拟机-049」
> 命名错配）、`043`（vm009 分组折叠为叶子）、`044`（已发布 KBD 重新归类）。
> 未执行不影响部署链路，但这些 KBD 的分类归属会保持现状。

### CI 自动验证（`db-migration-test.yml`）

1. 空库 `atlas schema apply` 全量建表
2. 按序执行 `data-migrations/*.sql`（验证 SQL 正确性）
3. 断言核心业务表存在、废弃表不存在
4. `atlas schema diff` 输出为空 → 幂等性通过

## 幂等性规范（强制）

`desired_extras.sql` 与 `data-migrations/*.sql` **必须可安全重复执行**：

| 操作类型 | 要求 |
|----------|------|
| `CREATE TABLE` | 必须加 `IF NOT EXISTS` |
| `ALTER TABLE ADD COLUMN` | 必须加 `IF NOT EXISTS` |
| `CREATE INDEX` | 必须加 `IF NOT EXISTS` |
| `CREATE FUNCTION` | 使用 `CREATE OR REPLACE` |
| `CREATE TRIGGER` | `DROP TRIGGER IF EXISTS` 后再创建 |
| `DROP TABLE` | 必须加 `IF EXISTS` |

## 铁律

1. **desired_schema.sql 是唯一权威** — `hci_troubleshoot` 表结构以此为准；`hci_sim` 只认 `hci-sim-migrations/`，两者不可交叉扫描
2. **desired_schema.sql 与 desired_extras.sql 必须同步修改** — 否则 CI 幂等验证失败
3. **data-migrations/ 已执行文件永不修改** — 变更走 Forward Fix，新增文件递增版本号
4. **禁止新增 Schema 迁移版本目录** — 变更历史由 Git 承载，不再维护 `atlas-migrations/` 副本
5. **数据库 migrations/ 目录只读归档** — 禁止新增 dbmate 文件

## 多环境说明

- **全新 DB**（测试/本地）：`atlas schema apply --env local`（全量建表）
- **已有 DB**（存量 dev/staging/prod）：同一命令，Atlas 自动计算差量收敛，无需 baseline
- **生产变更**：只能通过 K8s `db-migrate` Job（ArgoCD PreSync Hook），禁止本地 apply
- **CI 环境**：`db-migration-test.yml` 在空库上全量 apply 并验证幂等

## hci-sim 控制面 Schema（阶段 C/D）

`hci_sim` 数据库的控制面 metadata 按 `control_plane`、`fixture`、`artifact`、`audit` schema 管理：Scenario、不可变 Fixture Bundle、依赖、provenance、审批、审计、TestRun、Attempt、Event、Result 和 Runtime capability。它们只保存精确 revision、受控对象 URI、digest/哈希、状态与审计关联；**禁止**保存原始客户 Artifact、任意外部 URL 或可重放的 Lease 明文。真实 Artifact 进入具备审批、版本与保留策略的对象存储，Runtime 只能读取 `published` Bundle。

主库 `public.agent_test_*` 已从 `desired_schema.sql` 移除，且 `db-migration-test.yml` 将其中 15 张表列为 DEPRECATED 断言（不存在才通过）；backend 代码零引用。独立迁移入口为 `database/hci-sim-migrations/000001_control_plane.sql`，不由平台 Atlas Job 执行。

> **历史说明**：`schema_migrations` 表为旧 dbmate 工具表（已废弃）。Atlas 使用 `atlas_schema_revisions` 表跟踪版本。

---

## 业务种子数据说明（Seeds）

业务种子数据存放在 `database/seeds/` 目录中，用于在 Admin UI 中初始化工具管理、Prompt管理和技能管理页面，并提供显式标记的诊断测试 KBD 草稿。通过 Helm 部署时，`db-seed-job` 会作为 PostSync Hook 在数据库迁移完成后自动加载这些 SQL 文件。

### 1. 幂等性与覆盖策略

为保护不同环境及用户在管理页面的自定义配置，种子文件遵循以下差异化幂等设计：

| 种子数据文件 | 表名称 | 冲突处理策略 | 覆盖行为说明 |
| :--- | :--- | :--- | :--- |
| `01_tool_definitions.sql` | `tool_definition` | `ON CONFLICT (tool_name) DO UPDATE` | **会被强行覆盖**。工具定义参数与后端 Python 代码严格绑定，必须强制保持一致。 |
| `02_system_prompts.sql` | `system_prompt` | `ON CONFLICT (name) DO NOTHING` | **不会覆盖已有修改**。保护用户在界面微调或自定义的 Prompt 不被冲掉。 |
| `03_skill_definitions.sql` | `skill_definition` | `ON CONFLICT (name) DO NOTHING` | **不会覆盖已有修改**。保护用户自定义技能规则不被重置。 |
| `04_kbd_diagnosis_samples.sql` | `kbd_entry` | 仅升级同样例集且仍为 `draft` 的旧版本 | **只创建待审核草稿，不覆盖已发布、已拒绝或人工维护后的生命周期状态**。样例通过 `metadata.sample_suite` 检索，人工发布后才进入在线诊断，并在 KBD 同步后进入离线诊断。 |

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

### 3. 诊断 KBD 样例集使用方式

`04_kbd_diagnosis_samples.sql` 默认创建 5 篇 `draft`（待审核）KBD，样例集标识为
`diagnosis-signal-matrix-v1`。在 KBD 列表的“样例集标识”中输入该值即可筛出全部样例。
审核发布后的样例会直接用于在线诊断；执行一次离线诊断“KBD 同步与版本”的增量检测并审核派生资源后，
同一批样例即可用于离线诊断。再次加载 Seed 不会覆盖已经人工编辑或发布的样例。

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
