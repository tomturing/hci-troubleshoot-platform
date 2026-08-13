# HCI智能排障平台 - Makefile
# 依赖管理: uv (https://docs.astral.sh/uv/)

.PHONY: help install compose-check dev-up dev-down db-sync diagnosis-dev-keys diagnosis-sample-preflight diagnosis-sample-postgres-preflight diagnosis-lab-list diagnosis-lab-check diagnosis-lab-sync diagnosis-lab-up diagnosis-lab-status diagnosis-lab-connection diagnosis-lab-renew diagnosis-lab-online-smoke diagnosis-lab-offline-run diagnosis-lab-reset diagnosis-lab-down diagnosis-sample-e2e test lint clean quality-gate conflict-check post-merge k3s-release k3s-deploy-prod release-observe rollback-drill local-deploy local-deploy-import gen-schemas schema-check build-offline-collector test-offline-collector

# Docker Compose v2 是 Docker 官方当前发行形态，也是 GitHub Runner 提供的命令。
# 仍可通过 `make COMPOSE=docker-compose ...` 兼容仅安装 v1 的旧环境。
COMPOSE ?= docker compose

help:
	@echo "HCI智能排障平台 - 可用命令:"
	@echo ""
	@echo "  基础命令:"
	@echo "  make install        - 安装所有依赖 (uv sync + pnpm install)"
	@echo "  make dev-up         - 启动开发环境(Docker Compose)"
	@echo "  make dev-down       - 停止开发环境"
	@echo "  make diagnosis-dev-keys - 生成/校验本地离线诊断 RSA-3072 加密密钥"
	@echo "  make diagnosis-sample-preflight - 启动前验证 5 篇在线/离线诊断 KBD 样例"
	@echo "  make diagnosis-sample-postgres-preflight - 数据迁移后、服务启动前验证 5 篇 KBD 数据库闭环"
	@echo "  make diagnosis-lab-list - 查看可按需启动的在线/离线诊断样例场景"
	@echo "  make diagnosis-lab-up SCENARIO=... [VARIANT=positive] - 启动长期手工测试实例"
	@echo "  make diagnosis-lab-offline-run INSTANCE=... BUNDLE=... FINGERPRINT=... - 无网络执行离线采集"
	@echo "  make test           - 运行测试 (uv run pytest)"
	@echo "  make lint           - 代码检查 (uv run ruff)"
	@echo "  make clean          - 清理临时文件"
	@echo ""
	@echo "  多Agent工作流命令:"
	@echo "  make quality-gate   - 运行质量门禁（lint + test）"
	@echo "  make conflict-check - Worktree 冲突预检"
	@echo "  make post-merge     - 合并后集成验证"
	@echo "  make k3s-release    - 应急发布到 K3s（本地构建+导入+升级+校验）"
	@echo "  make k3s-deploy-prod- 🔴 生产 Helm 升级（会弹出 5 秒确认，需集群权限）"
	@echo "  make release-observe- 发布后观察（默认30分钟采样）"
	@echo "  make rollback-drill - 回滚演练（默认演练模式，不执行真实回滚）"
	@echo ""
	@echo "  CI 替代命令（GitHub Actions 不可用时）:"
	@echo "  make local-deploy            - 构建并 push 到 ghcr.io → ArgoCD 同步（全量）"
	@echo "  SERVICES=kb-service make local-deploy       - 仅构建指定服务"
	@echo "  SERVICES=kb-service make local-deploy-import - 构建并直接导入 K3s（离线）"
	@echo "  DRY_RUN=1 SERVICES=kb-service make local-deploy - 预览模式"
	@echo ""
	@echo "  数据库迁移命令（Atlas v6.3+）:"
	@echo "  atlas migrate diff --env local <name>  - 生成迁移文件"
	@echo "  atlas migrate apply --env local        - 本地应用迁移"
	@echo "  atlas migrate status --env local       - 查看迁移状态"
	@echo "  make db-sync        - 手动执行数据库 Schema 迁移（修改 desired_schema.sql 后使用）"
	@echo ""
	@echo "  信号数据模型契约（RFC §6.1）:"
	@echo "  make gen-schemas    - 从 ACQUIRER_ARGS_SCHEMA 导出 v2 JSON Schema 契约文件"
	@echo "  make schema-check   - CI 契约校验：schema 合法 + fixtures + 漂移检测"
	@echo "  make build-offline-collector - 构建 Linux x86_64 静态离线采集运行时"
	@echo "  make test-offline-collector  - 运行 Go 离线采集运行时测试"

install:
	@echo "安装Python依赖 (uv sync)..."
	uv sync
	@echo "安装前端依赖..."
	cd frontend && pnpm install

compose-check:
	@$(COMPOSE) version >/dev/null || { \
		echo "Docker Compose 不可用，请安装 Compose v2（docker compose）或设置 COMPOSE=docker-compose"; \
		exit 1; \
	}

dev-up: compose-check diagnosis-dev-keys diagnosis-sample-preflight
	@echo "Starting PostgreSQL & Redis..."
	$(COMPOSE) --env-file .env -f deploy/docker/docker-compose.yml up -d postgres redis
	@echo "Waiting for PostgreSQL to be ready..."
	@until $(COMPOSE) -f deploy/docker/docker-compose.yml exec -T postgres pg_isready -U $${POSTGRES_USER:-hci_admin}; do sleep 1; done
	@echo "Running database migrations..."
	$(COMPOSE) --env-file .env -f deploy/docker/docker-compose.yml up --force-recreate db-migrate
	@test "$$(docker inspect hci-db-migrate --format '{{.State.ExitCode}}')" = "0" || { \
		echo "Database migration failed; inspect logs with: docker logs hci-db-migrate"; \
		exit 1; \
	}
	$(MAKE) diagnosis-sample-postgres-preflight
	@echo "Starting all services..."
	$(COMPOSE) --env-file .env -f deploy/docker/docker-compose.yml up -d
	@echo ""
	@echo "服务已启动:"
	@echo "  - API Gateway: http://localhost:8000"
	@echo "  - Case Service: http://localhost:8001"
	@echo "  - Conversation Service: http://localhost:8002"
	@echo "  - Scheduler Service: http://localhost:8003"
	@echo "  - Admin UI: http://localhost:3002"

diagnosis-dev-keys:
	@echo "检查本地离线诊断加密密钥..."
	UV_CACHE_DIR=$${TMPDIR:-/tmp}/hci-uv-cache uv run --frozen python scripts/dev/ensure-diagnosis-dev-keys.py --env-file .env

diagnosis-sample-preflight:
	@echo "验证 5 篇 KBD 样例的发布、在线 Agent 与离线同步/诊断契约..."
	.venv/bin/python scripts/hci-sim/diagnosis-lab.py contract-smoke
	PYTHONPATH=backend/kb-service:backend .venv/bin/pytest -q backend/kb-service/tests/test_kbd_diagnosis_sample_seed.py
	PYTHONPATH=backend/agent-service:backend .venv/bin/pytest -q backend/agent-service/tests/unit/test_diagnosis_sample_contracts.py
	PYTHONPATH=backend/diagnosis-service:backend .venv/bin/pytest -q backend/diagnosis-service/tests/unit/test_diagnosis_sample_contracts.py
	PYTHONPATH=backend/diagnosis-service:backend .venv/bin/pytest -q backend/diagnosis-service/tests/unit/test_collector_artifact_service.py

diagnosis-sample-postgres-preflight:
	@echo "在 PostgreSQL 事务中验证 5 篇 KBD 的批量发布、离线同步、资源生成与诊断（结束后回滚）..."
	RUN_KB_POSTGRES_INTEGRATION=1 TEST_DATABASE_URL=$${DIAGNOSIS_PREFLIGHT_DATABASE_URL:-postgresql+asyncpg://$${POSTGRES_USER:-hci_admin}:$${POSTGRES_PASSWORD:-dev_password_123}@localhost:15432/$${POSTGRES_DB:-hci_troubleshoot}} PYTHONPATH=backend/kb-service:backend .venv/bin/pytest -q backend/kb-service/tests/integration/test_kbd_diagnosis_samples_postgres.py
	RUN_DIAGNOSIS_POSTGRES_INTEGRATION=1 TEST_DATABASE_URL=$${DIAGNOSIS_PREFLIGHT_DATABASE_URL:-postgresql+asyncpg://$${POSTGRES_USER:-hci_admin}:$${POSTGRES_PASSWORD:-dev_password_123}@localhost:15432/$${POSTGRES_DB:-hci_troubleshoot}} PYTHONPATH=backend/diagnosis-service:backend .venv/bin/pytest -q backend/diagnosis-service/tests/integration/test_diagnosis_samples_postgres.py

DIAGNOSIS_LAB := .venv/bin/python scripts/hci-sim/diagnosis-lab.py
LAB_INSTANCE := $(if $(INSTANCE),$(INSTANCE),$(shell printf '%s' '$(SCENARIO)' | tr '[:upper:]' '[:lower:]'))

diagnosis-lab-list:
	$(DIAGNOSIS_LAB) list

diagnosis-lab-check:
	$(DIAGNOSIS_LAB) check $(if $(SCENARIO),--scenario $(SCENARIO),)

diagnosis-lab-sync:
	$(DIAGNOSIS_LAB) sync-resources --mode $(if $(MODE),$(MODE),incremental)

diagnosis-lab-up:
	@test -n "$(SCENARIO)" || { echo "缺少 SCENARIO"; exit 2; }
	$(DIAGNOSIS_LAB) up --scenario $(SCENARIO) --instance $(LAB_INSTANCE) --variant $(if $(VARIANT),$(VARIANT),positive) --ttl $(if $(TTL),$(TTL),2h)

diagnosis-lab-status:
	$(DIAGNOSIS_LAB) status $(if $(INSTANCE),--instance $(INSTANCE),)

diagnosis-lab-connection:
	@test -n "$(INSTANCE)" || { echo "缺少 INSTANCE"; exit 2; }
	$(DIAGNOSIS_LAB) connection --instance $(INSTANCE)

diagnosis-lab-renew:
	@test -n "$(INSTANCE)" || { echo "缺少 INSTANCE"; exit 2; }
	$(DIAGNOSIS_LAB) renew --instance $(INSTANCE) --ttl $(if $(TTL),$(TTL),2h)

diagnosis-lab-online-smoke:
	@test -n "$(INSTANCE)" || { echo "缺少 INSTANCE"; exit 2; }
	$(DIAGNOSIS_LAB) online-smoke --instance $(INSTANCE)

diagnosis-lab-offline-run:
	@test -n "$(INSTANCE)" -a -n "$(BUNDLE)" -a -n "$(FINGERPRINT)" || { echo "缺少 INSTANCE、BUNDLE 或 FINGERPRINT"; exit 2; }
	$(DIAGNOSIS_LAB) offline-run --instance $(INSTANCE) --bundle $(BUNDLE) --fingerprint $(FINGERPRINT)

diagnosis-lab-reset:
	@test -n "$(INSTANCE)" || { echo "缺少 INSTANCE"; exit 2; }
	$(DIAGNOSIS_LAB) reset --instance $(INSTANCE)

diagnosis-lab-down:
	@test -n "$(INSTANCE)" || { echo "缺少 INSTANCE"; exit 2; }
	$(DIAGNOSIS_LAB) down --instance $(INSTANCE)

diagnosis-sample-e2e: diagnosis-dev-keys diagnosis-sample-preflight diagnosis-sample-postgres-preflight
	@echo "静态与 PostgreSQL 生命周期回归通过。运行层 E2E 请在平台/Terminal Bridge 启动后按需执行 diagnosis-lab-up、online-smoke 和 offline-run。"

db-sync: compose-check
	@echo "Running database schema migration..."
	@until $(COMPOSE) -f deploy/docker/docker-compose.yml exec -T postgres pg_isready -U $${POSTGRES_USER:-hci_admin}; do sleep 1; done
	$(COMPOSE) --env-file .env -f deploy/docker/docker-compose.yml up --force-recreate db-migrate
	@test "$$(docker inspect hci-db-migrate --format '{{.State.ExitCode}}')" = "0" || { \
		echo "Database migration failed; inspect logs with: docker logs hci-db-migrate"; \
		exit 1; \
	}
	@echo "Migration complete."

dev-down: compose-check
	@echo "停止开发环境..."
	$(COMPOSE) --env-file .env -f deploy/docker/docker-compose.yml down

test:
	@echo "运行测试 (按服务隔离，避免 app/ 命名空间冲突)..."
	uv run pytest tests/ -q
	uv run pytest backend/api-gateway/tests/ -q
	uv run pytest backend/case-service/tests/ -q
	uv run pytest backend/conversation-service/tests/ -q
	uv run pytest backend/scheduler-service/tests/ -q
	uv run pytest backend/kb-service/tests/ -q
	uv run pytest backend/diagnosis-service/tests/unit/ -q
	$(MAKE) test-offline-collector
	@echo "全部测试完成 ✓"

lint:
	@echo "运行代码检查..."
	uv run ruff check backend/ tests/

clean:
	@echo "清理临时文件..."
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +

# ============================================================================
# 多Agent工作流命令
# ============================================================================

quality-gate:
	@echo "运行质量门禁..."
	bash scripts/ci/agent-quality-gate.sh

conflict-check:
	@echo "运行 Worktree 冲突预检..."
	bash scripts/ci/check-worktree-conflicts.sh

post-merge:
	@echo "运行合并后集成验证..."
	bash scripts/ci/post-merge-verify.sh

k3s-release:
	@echo "⚠️  应急发布路径（正常发布请走 GitOps：环境仓库 PR → ArgoCD 同步）"
	@echo "执行 K3s 一键发布流程..."
	bash scripts/ops/k3s-release.sh

local-deploy:
	@echo "🔧 本地构建部署（CI 替代）: push 模式（构建 → 推送 ghcr.io → ArgoCD 同步）"
	@echo "SERVICES=$(SERVICES)  IMAGE_TAG=$(IMAGE_TAG)"
	SERVICES="$(SERVICES)" IMAGE_TAG="$(IMAGE_TAG)" DRY_RUN="$(or $(DRY_RUN),0)" \
		bash scripts/ops/local-deploy.sh push

local-deploy-import:
	@echo "🔧 本地构建部署（CI 替代）: import 模式（构建 → 导入 K3s → ArgoCD 同步）"
	@echo "SERVICES=$(SERVICES)  IMAGE_TAG=$(IMAGE_TAG)"
	SERVICES="$(SERVICES)" IMAGE_TAG="$(IMAGE_TAG)" DRY_RUN="$(or $(DRY_RUN),0)" \
		bash scripts/ops/local-deploy.sh import

k3s-deploy-prod:
	@echo ""
	@echo "🔴🔴🔴  即将直接升级【生产集群】Helm Release  🔴🔴🔴"
	@echo "    正常发布路径：GitOps 环境仓库 PR → ArgoCD 同步"
	@echo "    此命令仅用于 ArgoCD 不可用的极端应急情况"
	@echo ""
	@echo "按 Ctrl+C 取消，或等待 5 秒后继续..."
	@sleep 5
	bash scripts/ops/k3s-deploy-prod.sh

release-observe:
	@echo "执行发布后观察..."
	bash scripts/ops/release-observe.sh

rollback-drill:
	@echo "执行回滚演练（默认不执行真实回滚）..."
	bash scripts/ops/rollback-drill.sh

# ============================================================================
# 信号数据模型契约（RFC §6.1 JSON Schema 机器强制）
# ============================================================================

gen-schemas:
	@echo "导出信号 v2 JSON Schema 契约..."
	python backend/scripts/gen-schemas.py

schema-check:
	@echo "运行信号 v2 JSON Schema 契约校验..."
	python scripts/ci/check_signal_schemas.py

build-offline-collector:
	@echo "构建 Linux x86_64 静态离线采集运行时..."
	cd backend/diagnosis-service/offline-collector && \
		CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -buildvcs=false -trimpath -ldflags="-s -w -buildid=" \
		-o ../resources/bin/hci-collect-linux-amd64 .

test-offline-collector:
	@echo "运行 Go 离线采集运行时测试..."
	cd backend/diagnosis-service/offline-collector && go test ./...
