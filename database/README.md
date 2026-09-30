# 数据库迁移管理

## 唯一权威工具：Ptah Compat（0.11.2）

本项目使用 [Ptah Compat](https://github.com/stokaro/ptah/releases/tag/v0.11.2) 声明式管理数据库 Schema。
扩展、函数、触发器与表、索引统一声明在 `desired_schema.sql`，不再前后两次执行 `desired_extras.sql`。
接管验证与对照实验见[Ptah 团队的文章](https://blog.ptah.run/posts/hci-postgresql-desired-state/)。

> ⚠️ **dbmate 迁移链已于 2026-04-08 彻底废弃。**
> `database/migrations/` 下的文件仅作历史档案，不再被任何 K8s Job 执行。
> 所有 schema 变更必须：
> 1. 修改 `database/desired_schema.sql`（期望状态，唯一权威）
> 2. 运行 `ptah-compat schema diff` 审查差异
> 3. 提交 PR，CI 自动验证

## 目录结构

```
database/
  desired_schema.sql           ← 期望 Schema（唯一权威，Ptah Compat 声明式管理）
  data-migrations/              ← 版本化数据迁移（详见下文；040 保留原 extras 数据修复）
    001_xxx.sql
    002_yyy.sql
  atlas-migrations/             ← 历史 Atlas 迁移文件（只读保留，不被迁移镜像执行）
    20260408000000_baseline.sql ← baseline（接管时快照）
    atlas.sum                   ← 完整性校验文件（禁止手动修改）
  seeds/                        ← 业务种子数据（与迁移工具无关）
    01_tool_definitions.sql     ← 初始化 tool_definition 表
    02_system_prompts.sql       ← 初始化 system_prompt 表
    03_skill_definitions.sql    ← 初始化 skill_definition 表

docs/archive/db-migrations-history/
  migrations/                   ← 历史 dbmate 迁移文件（只读归档，v6.3 前）
```

## Ptah Compat 工作流

### 修改 Schema（新增表/字段/索引/扩展/函数/触发器）

```bash
# 1. 修改期望 schema（唯一权威入口）
vim database/desired_schema.sql

# 2. 审查 Schema 差异（需目标库及独立临时数据库就绪）
export DATABASE_URL="postgres://hci_admin:dev_password_123@localhost:15432/hci_troubleshoot?sslmode=disable"
export DEV_URL="postgres://hci_admin:dev_password_123@localhost:15432/atlas_dev?sslmode=disable"
export PTAH_POSTGRES_INDEX_STORAGE_PARAMS=1
export PTAH_ATLAS_ALLOW_UNMATCHED_EXCLUDE=1
ptah-compat schema diff --env local

# 3. 提交（期望状态与迁移说明必须同一 commit）
git add database/desired_schema.sql
```

### 本地应用迁移

更新工具版本后先重建迁移镜像。上述 URL 对应本地 Compose 默认配置；若修改 `.env` 或使用远程 Docker，需同步调整连接地址，确保审查与迁移指向同一数据库。

```bash
# 执行与部署相同的 Schema 和版本化数据迁移
make db-sync

# 验证 schema 与 desired_schema.sql 一致
ptah-compat schema diff --env local
```

`DEV_URL` 必须指向与目标库同版本的独立临时数据库，不得指向目标库。
项目部署和 CI 使用 PostgreSQL 15；迁移 `035` 的 `CREATE OR REPLACE TRIGGER` 至少需要 PostgreSQL 14。
服务器须安装 pgvector 等扩展二进制。`vector` 不是 trusted 扩展，目标库和临时库的迁移用户均须有安装权限；
内置 Chart 的 `config.postgresUser` 同时作为 `POSTGRES_USER` 初始化超级用户。外部/托管数据库须由管理员授权或预装扩展。
Ptah Compat 从期望状态管理扩展，无需手动重置临时库。现有 PostgreSQL init ConfigMap 保留原样；后续变更由完整期望状态收敛。

镜像统一设置 `PTAH_POSTGRES_INDEX_STORAGE_PARAMS=1`（保留 `lists=100`）和
`PTAH_ATLAS_ALLOW_UNMATCHED_EXCLUDE=1`（新库可能尚无历史工具表）；直接运行本地 CLI 时按上例设置。
迁移镜像直接从 Docker Hub 的 `stokaro/ptah:0.11.2` 复制 `ptah-compat`，并固定包含 amd64/arm64 的镜像索引摘要，无需下载 GitHub Release 压缩包。
`database/licenses/ptah-LICENSE` 是 [Ptah v0.11.2 的许可证原文](https://github.com/stokaro/ptah/blob/v0.11.2/LICENSE)，随二进制复制到镜像内。
`MIRROR_MODE=on` 保留原有 Alpine 包镜像设置。

### CI 自动验证

CI 流程自动执行：
1. 构建与部署相同的迁移镜像，以 UID 65534 完成新建库及版本化数据迁移
2. 保留核心表/废弃表检查，验证函数、触发器及 pgvector 行为
3. 重复部署，读取 `schema diff --format '{{ len .Changes }}'`，要求待变更数量为零
4. 用 PR 基线的迁移镜像建立存量库，再运行当前镜像；验证数据、`035` 历史 checksum、业务行为及零 diff。手动触发时以 `origin/main` 为基线。

## 幂等性规范（强制）

所有数据迁移脚本**必须可安全重复执行**：

| 操作类型 | 要求 |
|----------|------|
| `CREATE TABLE` | 必须加 `IF NOT EXISTS` |
| `ALTER TABLE ADD COLUMN` | 必须加 `IF NOT EXISTS` |
| `CREATE INDEX` | 必须加 `IF NOT EXISTS` |
| `CREATE TRIGGER` | `CREATE OR REPLACE TRIGGER` 或 `DROP TRIGGER IF EXISTS` 后再创建 |
| `DROP TABLE` | 必须加 `IF EXISTS` |

## 铁律

1. **平台库 desired_schema.sql 是唯一权威** — `hci_troubleshoot` 表结构以此为准；`hci_sim` 只认 `hci-sim-migrations/`，两者不可交叉扫描
2. **已提交的 Atlas 迁移文件永远不修改** — 历史文件只读保留，新 Schema 变更修改期望状态
3. **atlas.sum 禁止手动修改** — 与历史 Atlas 迁移文件一同保留
4. **migrations/ 目录只读归档** — 禁止新增 dbmate 文件
5. **desired_schema.sql 与迁移说明必须同一 commit 提交**；数据修复单独新增 `data-migrations/` 文件

## 多环境说明

- **全新 DB**（测试/本地）：先创建完整 Schema，再执行版本化数据迁移，最后收敛 Schema
- **已有 DB**（存量 dev/staging/prod）：先执行数据迁移，再收敛 Schema，避免新约束先于数据修复
- **CI 环境**：运行同一迁移镜像的新建、业务行为及重复部署验证；生产变更仍通过现有 Helm Job

存量库先执行数据迁移，再应用新 Schema。若数据迁移需要新增扩展，必须先在该数据迁移中安装，或由管理员预装；只改期望 Schema 来不及满足该数据迁移的依赖。
原 extras 的遗留清理和中断任务转换移至 `040`；其 CHECK 修复仅用于尚缺新值的旧定义，不收窄完整 Schema。
`035` 仅将触发器创建改为 `CREATE OR REPLACE`，避免空库完整 Schema 已创建触发器后的重名冲突。
runner 按版本跳过已执行迁移，因此存量库保留旧 checksum、新库记录新 checksum；将来校验摘要时须处理这一已知差异。

## hci-sim 控制面 Schema（阶段 C/D）

`hci_sim` 数据库的控制面 metadata 按 `control_plane`、`fixture`、`artifact`、`audit` schema 管理：Scenario、不可变 Fixture Bundle、依赖、provenance、审批、审计、TestRun、Attempt、Event、Result 和 Runtime capability。它们只保存精确 revision、受控对象 URI、digest/哈希、状态与审计关联；**禁止**保存原始客户 Artifact、任意外部 URL 或可重放的 Lease 明文。真实 Artifact 进入具备审批、版本与保留策略的对象存储，Runtime 只能读取 `published` Bundle。

当前主库中的 `public.agent_test_*` 是迁移前存量兼容源，复制脚本默认只 inventory；完成 copy/verify/switch 和观察窗口前不得 DROP。独立迁移入口为 `database/hci-sim-migrations/000001_control_plane.sql`，不由平台 Ptah Compat Job 执行。

> **历史说明**：`schema_migrations` 和 `atlas_schema_revisions` 为历史工具表，继续保留。
> Ptah Compat 直接收敛期望 Schema；版本化数据迁移仍由 `migration_history` 跟踪。

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
