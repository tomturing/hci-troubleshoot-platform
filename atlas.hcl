# Atlas 项目配置 — 声明式 Schema 管理
# 工具模式：atlas schema apply（声明式，非增量迁移）
# 单一真实来源：database/desired_schema.sql
#
# 开发者常用命令：
#   查看与当前 DB 的差异：atlas schema diff --env local
#   应用到本地 DB：       atlas schema apply --env local --auto-approve
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
#   DEV_URL      → 同实例内的临时 schema（如 ...?search_path=atlas_schema_dev）
env "local" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  dev = var.dev_url
}

# ── CI 环境（用于本地复现 CI 行为）──────────────────────────────────────────
# 依赖：
#   DATABASE_URL → postgres://hci_test:ci_test_pass@localhost:5432/hci_test?sslmode=disable
#   DEV_URL      → postgres://hci_test:ci_test_pass@localhost:5432/hci_test?sslmode=disable&search_path=atlas_schema_dev
env "ci" {
  src = "file://database/desired_schema.sql"
  url = var.db_url
  dev = var.dev_url
}
