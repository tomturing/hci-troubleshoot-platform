# ADR-006：引入 dbmate 数据库版本管理

- **状态**: 接受（Accepted）
- **日期**: 2026-04-15
- **关联 PR**: feat/kbd-pipeline-v1

---

## 背景

项目历史上数据库变更通过手动执行 SQL 文件完成。随着环境增多（dev / staging / prod），出现以下问题：

1. 各环境执行了哪些 SQL 没有记录，无法知道"当前环境缺哪些 migration"
2. 新成员加入时不知道应该按什么顺序初始化数据库
3. ArgoCD 同步代码后没有自动机制触发数据库迁移

---

## 决策

引入 [dbmate](https://github.com/amacneil/dbmate)（Go 单二进制，无额外依赖）管理数据库版本，通过 ArgoCD **PreSync Hook** 在每次部署前自动执行。

---

## 方案要点

### 目录结构

```
database/
  migrations/           ← 所有迁移文件（命名格式：YYYYMMDD_NNN_描述.sql）
    20260305_001_init_schema.sql
    ...
  seeds/
    00_baseline.sql     ← 存量环境一次性引导脚本（历史环境首次接入时用）
  README.md             ← 操作规范
```

### 文件命名规则

`YYYYMMDD_NNN_简短描述.sql`

- `YYYYMMDD`：创建日期
- `NNN`：当天序号（001 起）
- 描述：用英文小写+下划线，不超过 20 字符

### dbmate SQL 文件格式

```sql
-- migrate:up
-- 在此写 DDL/DML
CREATE TABLE ...;

-- migrate:down
-- 不强制提供降级 SQL，保留注释占位
```

### ArgoCD 集成

两个 PreSync Hook（按 `hook-weight` 顺序执行）：

| Hook | weight | 功能 |
|------|--------|------|
| `db-migrations-configmap.yaml` | -10 | 将 `database/migrations/*.sql` 文件挂载为 ConfigMap |
| `db-migrate-job.yaml` | -5 | 运行 `dbmate up` 执行所有未执行的迁移文件 |

两个 Hook 均使用 `BeforeHookCreation` 删除策略，保证每次 Sync 重新创建。

Job 名称包含 `{{ .Release.Revision }}` 确保每次唯一。

### 已有数据库的引导（一次性操作）

见 [存量环境引导操作指南](#存量环境引导操作指南)。

---

## 好处

- `schema_migrations` 表追踪版本状态，**每个文件只执行一次**，幂等安全
- `dbmate status` 可随时查看各环境缺哪些迁移
- 新环境 `dbmate up` 一键从零搭建完整 Schema
- 与 ArgoCD 原生集成，代码 push → 自动迁移，无需人工介入

## 代价

- 新增学习成本（dbmate CLI 用法，但极简）
- ConfigMap 嵌入 SQL 内容，Helm 渲染稍慢（可忽略）

---

## 存量环境引导操作指南

仅在**首次引入 dbmate 时对已有数据库执行一次**。

```bash
# 1. 设置目标环境连接串
export DATABASE_URL="postgres://user:password@host:5432/dbname?sslmode=disable"

# 2. 执行 baseline 标记（告诉 dbmate 历史文件已执行）
psql "$DATABASE_URL" -f database/seeds/00_baseline.sql

# 3. 确认结果（应显示 7 条记录）
psql "$DATABASE_URL" -c "SELECT * FROM schema_migrations ORDER BY version;"

# 4. 验证 dbmate 不会重复执行历史文件
# 安装 dbmate（本地验证用）
# curl -fsSL -o /usr/local/bin/dbmate https://github.com/amacneil/dbmate/releases/latest/download/dbmate-linux-amd64
# chmod +x /usr/local/bin/dbmate
dbmate --migrations-dir database/migrations status
# 期望所有 7 个文件显示 [x]（已应用）
```

> **注意**：如果某个环境确实没有执行某个历史 migration，
> 请在 `00_baseline.sql` 中删除对应行，让 dbmate 正常执行该文件。

---

## 日常使用

```bash
# 查看状态
dbmate --url $DATABASE_URL --migrations-dir database/migrations status

# 执行所有待执行迁移
dbmate --url $DATABASE_URL --migrations-dir database/migrations up

# 添加新 migration（自动生成带时间戳的文件）
dbmate --migrations-dir database/migrations new "描述"
```

---

## 备选方案

| 工具 | 原因未选 |
|------|---------|
| Alembic | 已用于 ORM，但仅限 Python，且需在应用容器内执行 |
| Flyway | JVM 依赖，镜像体积大 |
| Liquibase | XML/YAML 格式，复杂度高 |
| 手动 SQL | 当前痛点根源，放弃 |
