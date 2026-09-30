# Ptah Compat 项目配置 — 兼容 Atlas 的声明式 Schema 管理
# 工具模式：ptah-compat schema apply（声明式，非增量迁移）
# 单一真实来源：database/desired_schema.sql
#
# 开发者常用命令：
#   查看与当前 DB 的差异：ptah-compat schema diff --env local
#   应用到本地 DB：       ptah-compat schema apply --env local --auto-approve
#
# CI/K8s 不使用此配置文件，直接在命令行传递 --url / --to / --dev-url 参数
# （见 .github/workflows/db-migration-test.yml 和 db-migrate-job.yaml）

variable "db_url" {
  type    = string
  default = getenv("DATABASE_URL")
}

variable "dev_url" {
  type    = string
  default = getenv("DEV_URL")
}

# ── 本地开发环境 ──────────────────────────────────────────────────────────────
# 依赖：
#   DATABASE_URL → 指向本地 Postgres（如 postgres://hci_admin:xxx@localhost:5432/hci_troubleshoot?sslmode=disable）
#   DEV_URL      → 同实例内独立的临时数据库（不得指向目标库）
env "local" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  dev = var.dev_url
}

# ── CI 环境（用于本地复现 CI 行为）──────────────────────────────────────────
# 依赖：
#   DATABASE_URL → postgres://hci_test:ci_test_pass@localhost:5432/hci_test?sslmode=disable
#   DEV_URL      → postgres://hci_test:ci_test_pass@localhost:5432/atlas_dev?sslmode=disable
env "ci" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  dev = var.dev_url
}

# ── 生产环境（只读 diff，禁止直接 apply）────────────────────────────────────
# 依赖：
#   DATABASE_URL → postgres://<user>:<pass>@<prod-host>:5432/hci_troubleshoot?sslmode=require
#   DEV_URL      → 同实例内独立的临时数据库（不得指向目标库）
# 用法（仅限 diff，不可 apply）：
#   ptah-compat schema diff --env prod
# 注意：生产变更必须通过 db-migrate Job（K8s），禁止本地 apply
env "prod" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  dev = var.dev_url
}
