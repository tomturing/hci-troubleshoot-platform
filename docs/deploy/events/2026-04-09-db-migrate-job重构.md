---
status: active
category: deploy
audience: operator
last_updated: 2026-04-09
owner: team
---

# db-migrate Job 重构：Atlas 声明式 + psql extras

## 背景

原 db-migrate Job（PR #124）实为增量迁移（`atlas migrate apply`），本次重构改为真正的声明式部署。

## 重构内容

### 1. Dockerfile.migrations（多阶段构建）

```dockerfile
# Stage 1: 获取 Atlas 二进制
FROM arigaio/atlas:latest AS atlas-bin
# Stage 2: postgres:15-alpine 自带 psql
FROM postgres:15-alpine
COPY --from=atlas-bin /atlas /usr/local/bin/atlas
COPY database/desired_schema.sql /desired_schema.sql
COPY database/desired_extras.sql /desired_extras.sql
COPY scripts/db-migrate.sh /db-migrate.sh
RUN chmod +x /db-migrate.sh
ENTRYPOINT ["/db-migrate.sh"]
```

### 2. db-migrate-job.yaml 关键变更

**新增 initContainer `init-atlas-dev`：**
```yaml
initContainers:
- name: init-atlas-dev
  image: postgres:15-alpine
  command: ["/bin/sh", "-c"]
  args:
  - |
    # 1. 确保目标库有所需 extensions（staging/prod 首次部署关键）
    psql "$DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS uuid-ossp; ..."
    # 2. 创建 atlas_dev 数据库（dev-url 用独立 DB，非 search_path）
    psql "$DATABASE_URL" -c "CREATE DATABASE atlas_dev;" 2>/dev/null || true
    psql "$ATLAS_DEV_URL" -c "CREATE EXTENSION IF NOT EXISTS uuid-ossp; ..."
```

**主容器改动：**
- 不再覆盖 ENTRYPOINT（由 `db-migrate.sh` 入口脚本处理）
- 注入 `DATABASE_URL`、`DEV_URL`、`HOME=/tmp`
- 无 `readOnlyRootFilesystem`（sh 脚本需要写权限）
- `/tmp` 挂载 `emptyDir`（Atlas 需要写 `.cache`）

### 3. init-configmap.yaml

补全 4 个 extensions（原只有 2 个）：
- uuid-ossp ✓（原有）
- pg_trgm ✓（原有）
- pgcrypto ← 新增
- vector ← 新增

## 多环境注意事项

staging/prod 环境首次执行时，initContainer 会先确保目标库有所有 extensions（`vector`、`pgcrypto` 等），然后再创建 `atlas_dev`。这解决了跨环境部署时"extension 不存在"的失败场景。

## 验证

本地 k3s (hci-dev namespace) 验证通过：
- 第一次：300 SQL，26 张表→17 张表（7 张废弃表 DROP）
- 第二次（幂等）：`Schema is synced, no changes to be made`
