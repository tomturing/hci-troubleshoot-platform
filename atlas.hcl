# Atlas 项目配置
# 参考文档：https://atlasgo.io/atlas-schema/projects
#
# 工作流说明：
#   1. 修改 database/desired_schema.sql（期望状态）
#   2. 运行 atlas migrate diff --env local <migration-name>  生成迁移文件
#   3. 审查 database/atlas-migrations/ 下生成的 SQL 文件
#   4. 提交 PR → CI 自动执行 atlas migrate lint + 全量执行验证
#   5. 合并后 CI 构建 db-migrate 镜像 → ArgoCD PreSync 自动执行

variable "db_url" {
  type    = string
  default = getenv("DATABASE_URL")
}

# ── 本地开发环境 ──────────────────────────────────────────────────────────────
# 用途: 开发者生成新迁移文件（atlas migrate diff --env local <name>）
# 依赖: 需要本地运行 postgres 容器，DATABASE_URL 指向本地数据库
env "local" {
  # 声明式期望 schema 定义（17 张业务表）
  src = "file://database/desired_schema.sql"
  # 当前实际数据库（用于 diff 计算）
  url = var.db_url
  # 临时 dev 库：Atlas 内部用于解析 schema DDL，容器自动创建销毁
  dev = "docker://postgres/15/dev?search_path=public"
  migration {
    dir    = "file://database/atlas-migrations"
    format = atlas
  }
  exclude = [
    # 排除 Atlas 自身版本跟踪表，避免被算入 diff
    "atlas_schema_revisions",
    # 排除已废弃的 dbmate 版本表（已有 DB 上可能还存在）
    "schema_migrations",
  ]
}

# ── CI 环境 ───────────────────────────────────────────────────────────────────
# 用途: GitHub Actions PR 验证（atlas migrate lint + atlas migrate apply）
# 依赖: DATABASE_URL 指向 CI postgres service container
env "ci" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  dev = "docker://postgres/15/dev?search_path=public"
  migration {
    dir    = "file://database/atlas-migrations"
    format = atlas
  }
  exclude = [
    "atlas_schema_revisions",
    "schema_migrations",
  ]
}

# ── 生产/部署环境 ─────────────────────────────────────────────────────────────
# 用途: K8s Job 中执行迁移（atlas migrate apply --env prod）
# 依赖: DATABASE_URL 由 K8s Job 环境变量注入
env "prod" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  migration {
    dir    = "file://database/atlas-migrations"
    format = atlas
  }
  exclude = [
    "atlas_schema_revisions",
    "schema_migrations",
  ]
}
