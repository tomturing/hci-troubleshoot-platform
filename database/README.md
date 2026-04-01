# 数据库迁移管理

## 工具：dbmate

本项目使用 [dbmate](https://github.com/amacneil/dbmate) 管理数据库 Schema 版本。

## 目录结构

```
database/
  migrations/              ← 版本化迁移文件（只追加，禁止修改已提交文件）
    20260305_001_init_schema.sql
    20260312_001_kb_rag_v3.sql
    ...
  seeds/                   ← 初始种子数据（分类树、同义词等，可重复执行）
    01_kb_category.sql
  schema.sql               ← 由 dbmate dump 自动维护，代表当前最新 schema（勿手改）
  README.md
```

## 命名规范

```
YYYYMMDD_NNN_描述.sql
```

- `YYYYMMDD`：变更日期
- `NNN`：当天序号（001 起）
- 描述：简短英文，下划线分隔

## 操作命令

```bash
# 本地开发：执行所有未运行的迁移
dbmate --url "postgresql://hci_admin:dev_password_123@localhost:5432/hci_troubleshoot?sslmode=disable" up

# 查看迁移状态（哪些已执行、哪些未执行）
dbmate --url "..." status

# 创建新的迁移文件（自动生成带时间戳的文件名）
dbmate --migrations-dir database/migrations new "add_audit_log_table"

# 回滚最后一次迁移
dbmate --url "..." down

# 生成当前 schema 快照（提交前更新 schema.sql）
dbmate --url "..." dump
```

## 铁律

1. **已提交的 migration 文件永远不修改** ——如需修订，新建文件
2. **每个文件必须幂等**：使用 `IF NOT EXISTS` / `ON CONFLICT DO NOTHING`
3. **每次新建 migration 后，本地跑 `dbmate up` + `dbmate dump` 更新 schema.sql 一并提交**
4. `schema.sql` 不手动编辑

## 多环境说明

各环境通过 ArgoCD PreSync Hook 自动执行 dbmate：
- dbmate 内部维护 `schema_migrations` 表，记录每个文件是否已执行
- 同一文件在同一环境**只执行一次**
- 新环境从零开始则顺序全部执行

**已有数据库的环境（存量引导）**：
见 `database/seeds/` 目录下的 `00_baseline.sql`，需要在迁移工具接管前手动执行一次。
