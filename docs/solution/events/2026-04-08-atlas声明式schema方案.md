---
status: active
category: solution
audience: developer
last_updated: 2026-04-08
owner: team
---

# 方案：Atlas 声明式数据库发布改造

> 对应需求：[2026-04-08-atlas数据库改造需求](../../../requirement/events/2026-04-08-atlas数据库改造需求.md)

---

## 一、第一性原理分析

**数据库发布的本质需求是**：在任意环境（全新/已有）中，安全、可重复地将数据库结构从任意历史状态收敛到目标状态，同时保证数据不丢失。

从这个本质出发：

| 层次 | 理想状态 | 现有问题 |
|------|---------|---------|
| **什么是 schema** | 单一事实来源：`desired_schema.sql` | 散落在 16 个手写 SQL 文件里，没有全局视图 |
| **如何产生变更** | 声明式 diff：期望状态 vs 当前状态 → 自动生成 delta | 手写 SQL 迁移，依赖人脑记住依赖顺序 |
| **如何验证变更** | PR 阶段在真实 DB 执行，失败即阻断 | 生产才发现；没有 lint；没有执行验证 |
| **如何部署变更** | 版本化迁移文件打包进不可变镜像 | 静态嵌入 Helm ConfigMap，手动同步 |
| **如何跟踪状态** | 迁移版本表自动维护 | `schema_migrations`（dbmate 私有格式）手动插入 |

**结论**：根本问题是**没有 schema 的单一事实来源**，导致所有下游步骤都是手动补丁。

---

## 二、选型决策：Atlas

### 2.1 方案对比

| 工具 | 优点 | 缺点 | 适用场景 |
|------|------|------|---------|
| **dbmate（现有）** | 轻量、Go 单二进制 | 纯版本化（无声明式 diff）、无 lint | 小型项目快速迁移 |
| **Flyway** | 企业级、成熟 | JVM 依赖重、声明式支持弱、镜像大 | Java 生态 |
| **Liquibase** | 声明式强、多格式 | XML/YAML 配置复杂、学习曲线高 | 需要 changelog 的企业项目 |
| **Atlas** | 声明式+版本化、Go 单二进制、原生 lint、开源 | 相对新（2022 年）、部分高级能力需企业版 | 现代 DevOps 流水线 |
| **Alembic** | Python 生态原生 | Python 依赖、非声明式 diff | Python ORM（SQLAlchemy）项目 |

**选择 Atlas 的理由**：
1. 唯一同时支持**声明式 schema diff**（从期望状态生成迁移）+ **版本化迁移执行**（可审计历史）的开源工具
2. Go 单二进制，与 dbmate 同等轻量，镜像无额外依赖
3. 内置 `atlas migrate lint`，可在 CI 阶段检测危险操作
4. PostgreSQL 支持最完整（含 ENUM、触发器、函数、pgvector）

### 2.2 为什么不选其他方案

- **继续使用 dbmate + 镜像化（放弃声明式）**：只解决了手动同步问题，不解决 PR 阶段验证和手写 SQL 错误问题
- **纯声明式（不保留版本化迁移文件）**：无法审计"每次部署做了什么变更"，无法定向回滚，不适合生产环境
- **Alembic**：项目已迁移出 Alembic（`alembic_version` 表已废弃），不应引入回调

---

## 三、架构设计

### 3.1 核心工作流

```
开发者修改 database/desired_schema.sql
              ↓
atlas migrate diff --env local <migration-name>
     ↓ Atlas 自动 diff 并生成迁移文件
database/atlas-migrations/YYYYMMDDHHMMSS_<name>.sql
              ↓
开发者审查生成的迁移文件（确认 SQL 符合预期）
              ↓
提交 PR → CI 触发
    ├── atlas migrate lint --env ci
    │    └── 检测：危险操作 / 无默认值 NOT NULL / 数据依赖删除
    └── db-migration-test job（postgres service container）
         └── atlas migrate apply（全量执行，SQL 错误即 CI 失败）
              ↓
PR 合并 → CI 构建 db-migrate 镜像
    └── docker build -f Dockerfile.migrations → ghcr.io/org/hci-db-migrate:<sha>
              ↓
ArgoCD Sync → PreSync Hook → db-migrate-job.yaml
    └── atlas migrate apply（增量执行，仅未执行的迁移）
```

### 3.2 文件结构变化

**新增**：
```
atlas.hcl                                        # Atlas 项目配置（多环境 env 定义）
database/
├── desired_schema.sql                           # 声明式期望 schema（17 张业务表）
└── atlas-migrations/                            # Atlas 管理的迁移目录
    ├── atlas.sum                                # 完整性校验文件（atlas 自动维护）
    └── 20260408000000_baseline.sql              # Baseline：全量建表 SQL

Dockerfile.migrations                            # db-migrate 镜像构建文件
.github/workflows/db-migration-test.yml          # PR 阶段 DB 全量验证 workflow
scripts/dev/atlas-diff.sh                        # 开发者生成迁移的便捷脚本
```

**删除**：
```
deploy/helm/hci-platform/templates/hooks/db-migrations-configmap.yaml
deploy/helm/hci-platform/templates/hooks/db-migrations-configmap.yaml.bak
scripts/ci/sync-db-migrations.sh
scripts/ci/check-db-migrations-sync.sh
```

**修改**：
```
deploy/helm/hci-platform/templates/hooks/db-migrate-job.yaml  # 改用 Atlas 镜像
deploy/helm/hci-platform/values.yaml                          # 更新 dbMigrate 配置
.github/workflows/ci.yml                                       # 删除旧 job，无需新增
```

### 3.3 Atlas 迁移目录与旧迁移目录的关系

| 目录 | 管理工具 | 状态 | 说明 |
|------|---------|------|------|
| `database/migrations/` | dbmate（废弃） | **只读存档** | 历史文件保留用于审计，不再执行 |
| `database/atlas-migrations/` | Atlas | **活跃** | 所有新迁移在此目录 |

`database/migrations/` 不删除，作为历史变更记录。未来若无审计需求可另行清理。

### 3.4 Baseline 机制（已有 DB 平滑切换）

**问题**：dev/staging/prod 数据库已通过 dbmate 执行了 16 个迁移文件。Atlas 不知道这些历史，若不处理，首次运行时会试图执行 `20260408000000_baseline.sql`（全量建表），因表已存在而失败。

**解决方案**：Atlas `--baseline` 标志

```bash
# 已有 DB 首次切换时，跳过 baseline 迁移，从其后的新迁移开始
atlas migrate apply \
  --url "$DATABASE_URL" \
  --dir "file:///atlas-migrations" \
  --baseline "20260408000000"
```

- **全新 DB**（测试/新环境）：正常执行所有迁移（含 baseline 全量建表）
- **已有 DB**（dev/staging/prod）：Helm values 中设置 `dbMigrate.atlasBaseline`，跳过 baseline

```yaml
# values.yaml（已有 DB 环境）
dbMigrate:
  atlasBaseline: "20260408000000"   # 首次切换时配置；所有环境迁移完成后可移除此字段
```

### 3.5 版本跟踪表变更

| 旧版 | 新版 |
|------|------|
| `schema_migrations`（dbmate） | `atlas_schema_revisions`（Atlas 自动创建） |

`schema_migrations` 表在 `desired_schema.sql` 中移除（不再是业务表，是工具内部表）。`atlas_schema_revisions` 由 Atlas 自动创建管理，无需手动维护。

---

## 四、Atlas 项目配置（atlas.hcl）

```hcl
# Atlas 项目配置
# 工作流说明：
#   新增/修改 schema → atlas migrate diff --env local <name>
#   → 审查 database/atlas-migrations/ 生成文件 → 提交 PR
#   → CI: atlas migrate lint + db-migration-test job
#   → 合并 → 构建 db-migrate 镜像 → ArgoCD PreSync 自动执行

variable "db_url" {
  type    = string
  default = getenv("DATABASE_URL")
}

env "local" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  dev = "docker://postgres/15/dev?search_path=public"
  migration {
    dir = "file://database/atlas-migrations"
  }
}

env "ci" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  dev = "docker://postgres/15/dev?search_path=public"
  migration {
    dir    = "file://database/atlas-migrations"
    format = atlas
  }
}

env "prod" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  migration {
    dir    = "file://database/atlas-migrations"
    format = atlas
  }
}
```

---

## 五、CI 变更设计

### 5.1 删除的 CI job

- `check-db-migrations-sync`：检查 ConfigMap 同步状态（ConfigMap 废弃后无意义）

### 5.2 新增的 CI workflow（PR 阶段）

`.github/workflows/db-migration-test.yml`：

```yaml
services:
  postgres:
    image: postgres:15
    env: { POSTGRES_PASSWORD: ci, POSTGRES_DB: hci_test }

steps:
  - atlas migrate lint      # 检测危险操作
  - atlas migrate apply     # 全量执行验证
  - 验证关键表存在          # schema 完整性检查
  - atlas migrate apply (2) # 幂等性验证（第二次执行应 0 变更）
```

---

## 六、风险评估

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|---------|
| Atlas baseline 配置遗漏 | 低 | 高（已有 DB 建表失败） | Helm PreUpgrade Hook 检查 `atlas_schema_revisions` 是否存在，存在则强制设置 baseline |
| `desired_schema.sql` 与实际 DB 漂移 | 中 | 中（生成错误迁移文件） | 定期运行 `atlas schema diff` 漂移检测（可加入定时 CI） |
| Atlas 无法解析 PL/pgSQL 触发器函数 | 低 | 低（触发器仍可在迁移文件中手写） | `desired_schema.sql` 只含表/索引/枚举定义，触发器/函数在迁移文件手写 |
| 迁移镜像构建失败阻断部署 | 低 | 中 | 镜像构建在 CI 中验证，ArgoCD 保留上一版本镜像可回滚 |

---

## 七、开发者工作手册

### 新增数据库字段/表（声明式工作流）

```bash
# 1. 修改期望 schema
vim database/desired_schema.sql

# 2. 启动本地 dev DB（Atlas 需要 dev DB 运行 schema 生成 diff）
docker compose -f deploy/docker/docker-compose.yml up -d postgres

# 3. 生成迁移文件
export DATABASE_URL="postgres://postgres:postgres@localhost:5432/hci_dev?sslmode=disable"
atlas migrate diff --env local add_new_column

# 4. 审查生成的迁移文件
cat database/atlas-migrations/$(ls -t database/atlas-migrations/*.sql | head -1)

# 5. 提交 PR（CI 自动验证）
git add database/desired_schema.sql database/atlas-migrations/
git commit -m "feat(db): 添加新字段"
```

### 在已有 DB 上首次应用 Atlas

```bash
# 在 HCI 环境库（hci-platform-env）中，首次切換时设置 baseline
# values.yaml 对应环境:
dbMigrate:
  atlasBaseline: "20260408000000"
```

切换完成后所有环境均已初始化 `atlas_schema_revisions` 表后，可从 values 中移除 `atlasBaseline` 字段。
