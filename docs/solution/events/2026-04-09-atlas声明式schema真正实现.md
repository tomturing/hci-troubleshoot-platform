---
status: active
category: solution
audience: developer
last_updated: 2026-04-09
owner: team
---

# Atlas 声明式 Schema 管理——真正实现

## 背景与问题

PR #124（"feat(db): 引入 Atlas 声明式 Schema 管理"）标题声称使用声明式模式，但实际代码审查发现：

- `Dockerfile.migrations` 使用 `ghcr.io/ariga/atlas:latest`（不存在的镜像）
- 复制了 `atlas-migrations/` 目录（增量文件）
- Job YAML 调用的是 `atlas migrate apply`（增量模式），带 `--baseline` 和 `--allow-dirty` 标志

**核心差异：**

| | PR #124（伪声明式） | 本次（真声明式） |
|---|---|---|
| 命令 | `atlas migrate apply` | `atlas schema apply --to desired_schema.sql` |
| 模式 | 增量迁移 | 声明式 diff+apply |
| 会删除废弃表吗？ | 不会 | 会 |
| 新增该文件后生效 | 需要手写迁移文件 | 自动计算 diff |

## 技术方案

### Atlas Community 版限制

经实测，Atlas Community 版不支持：
- `CREATE EXTENSION`（Pro 专属）→ 移至 `postgres/init-configmap.yaml`（DB 初始化时由 psql 执行）
- `CREATE OR REPLACE FUNCTION`（Pro 专属）→ 移至 `desired_extras.sql`
- `CREATE TRIGGER`（Pro 专属）→ 移至 `desired_extras.sql`

### 拆分方案

```
database/desired_schema.sql   ← Atlas 管理：ENUM/表/索引/FK
database/desired_extras.sql   ← psql 管理：函数（CREATE OR REPLACE）+ 触发器（DROP IF EXISTS + CREATE）
```

### 3 步串行迁移（`scripts/db-migrate.sh`）

```
Step 1: psql -f /desired_extras.sql  (只创建函数，表不存在时 trigger 会报错)
Step 2: atlas schema apply --to file:///desired_schema.sql \
          --url $DATABASE_URL --dev-url $DEV_URL \
          --exclude "schema_migrations,alembic_version,atlas_schema_revisions" \
          --auto-approve
Step 3: psql -f /desired_extras.sql  (完整创建触发器)
```

### initContainer 职责

```yaml
# 先确保目标库有所需 extensions（解决 staging 部署问题）
psql "$DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS uuid-ossp; ..."
# 再创建 atlas_dev 数据库 + extensions（Atlas dev-url 使用独立 DB）
psql "$DATABASE_URL" -c "CREATE DATABASE atlas_dev;"
psql "$ATLAS_DEV_URL" -c "CREATE EXTENSION IF NOT EXISTS uuid-ossp; ..."
```

### dev-url 方案选择

| 方案 | 结果 |
|---|---|
| `search_path=atlas_schema_dev` | ❌ schema not found |
| 独立 `atlas_dev` 数据库 | ✅ 成功（`hci_admin` 有 CREATEDB 权限） |

### 多阶段 Dockerfile

```dockerfile
FROM arigaio/atlas:latest AS atlas-bin
FROM postgres:15-alpine          # 自带 psql，不需要额外安装
COPY --from=atlas-bin /atlas /usr/local/bin/atlas
```

## k3s 本地验证结果

- **第一次运行**：执行 300 条 SQL，26 张旧表 → 17 张期望表（7 张废弃表被 DROP）
- **第二次运行（幂等验证）**：`Schema is synced, no changes to be made` ✅

## 多环境部署说明

| 环境 | 触发方式 | 说明 |
|---|---|---|
| dev | PR 合并后 CI 自动构建 + ArgoCD 自动执行 | 全自动，无需操作 |
| staging | 手动触发 `env-repo-sync.yml` (target_env=staging) | 设计如此；Atlas 自动计算 staging 与期望状态的 diff |

## 影响文件

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `database/desired_schema.sql` | 修改 | 移除 extension/function/trigger；修复 FK 顺序 |
| `database/desired_extras.sql` | 新增 | 3 函数 + 7 触发器，psql 幂等管理 |
| `Dockerfile.migrations` | 修改 | 多阶段构建（atlas-bin + postgres:15-alpine） |
| `scripts/db-migrate.sh` | 新增 | 3 步串行入口脚本 |
| `deploy/helm/.../db-migrate-job.yaml` | 修改 | initContainer 双库初始化 + 新容器规格 |
| `deploy/helm/.../init-configmap.yaml` | 修改 | 补全 pgcrypto + vector extensions |
| `.github/workflows/db-migration-test.yml` | 修改 | 全量重写为声明式验证流程 |
| `atlas.hcl` | 修改 | 移除 `migration {}` 块，改为声明式 env |
| `deploy/helm/.../values.yaml` | 修改 | 注释修正 |
