# HCI智能排障平台 - Makefile
# 依赖管理: uv (https://docs.astral.sh/uv/)

.PHONY: help install dev-up dev-down test lint clean vk vk-stop vk-restart quality-gate conflict-check post-merge k3s-release k3s-deploy-prod release-observe rollback-drill sync-claw-configs check-claw-configs db-sync db-check db-migrate-test

help:
	@echo "HCI智能排障平台 - 可用命令:"
	@echo ""
	@echo "  基础命令:"
	@echo "  make install        - 安装所有依赖 (uv sync + pnpm install)"
	@echo "  make dev-up         - 启动开发环境(Docker Compose)"
	@echo "  make dev-down       - 停止开发环境"
	@echo "  make test           - 运行测试 (uv run pytest)"
	@echo "  make lint           - 代码检查 (uv run ruff)"
	@echo "  make clean          - 清理临时文件"
	@echo ""
	@echo "  多Agent工作流命令:"
	@echo "  make vk             - 启动 Vibe Kanban（任务编排中枢）"
	@echo "  make quality-gate   - 运行质量门禁（lint + test）"
	@echo "  make conflict-check - Worktree 冲突预检"
	@echo "  make post-merge     - 合并后集成验证"
	@echo "  make k3s-release    - 应急发布到 K3s（本地构建+导入+升级+校验）"
	@echo "  make k3s-deploy-prod- 🔴 生产 Helm 升级（会弹出 5 秒确认，需集群权限）"
	@echo "  make release-observe- 发布后观察（默认30分钟采样）"
	@echo "  make rollback-drill - 回滚演练（默认演练模式，不执行真实回滚）"
	@echo ""
	@echo "  数据库迁移命令（dbmate）:"
	@echo "  make db-sync        - 同步 database/migrations/ 到 Helm ConfigMap（新增迁移后必须运行）"
	@echo "  make db-check       - 检查同步状态 + version 唯一性（CI 同款检查）"

install:
	@echo "安装Python依赖 (uv sync)..."
	uv sync
	@echo "安装前端依赖..."
	cd frontend && pnpm install

dev-up:
	@echo "启动开发环境..."
	docker-compose -f deploy/docker/docker-compose.yml up -d
	@echo "服务已启动:"
	@echo "  - API Gateway: http://localhost:8000"
	@echo "  - Case Service: http://localhost:8001"
	@echo "  - Conversation Service: http://localhost:8002"
	@echo "  - Scheduler Service: http://localhost:8003"

dev-down:
	@echo "停止开发环境..."
	docker-compose -f deploy/docker/docker-compose.yml down

test:
	@echo "运行测试 (按服务隔离，避免 app/ 命名空间冲突)..."
	uv run pytest tests/ -q
	uv run pytest backend/api-gateway/tests/ -q
	uv run pytest backend/case-service/tests/ -q
	uv run pytest backend/conversation-service/tests/ -q
	uv run pytest backend/scheduler-service/tests/ -q
	uv run pytest backend/kb-service/tests/ -q
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

# VK 固定端口（避免每次重启端口变化导致 MCP 需要 Reload Window）
VK_PORT ?= 9527

vk:
	@echo "启动 Vibe Kanban（端口 $(VK_PORT)）..."
	PORT=$(VK_PORT) npx vibe-kanban

vk-stop:
	@echo "停止 Vibe Kanban..."
	@pkill -f "vibe-kanban" 2>/dev/null && echo "✓ VK 已停止" || echo "VK 未在运行"

vk-restart:
	@echo "重启 Vibe Kanban..."
	@pkill -f "vibe-kanban" 2>/dev/null || true
	@sleep 1
	PORT=$(VK_PORT) npx vibe-kanban

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

# ======================== Claw 配置管理 =====================================

## 将 deploy/claw-configs/ 同步到 Helm chart 内副本
## 规则：deploy/claw-configs/ 是人工编辑的源，Helm chart 副本用于 .Files.Get
## 每次修改 deploy/claw-configs/ 后运行此命令，然后 helm upgrade
sync-claw-configs:
	@echo "同步 claw-configs 到 Helm chart..."
	@cp -r deploy/claw-configs/learningclaw \
		deploy/helm/hci-platform/claw-configs/learningclaw && \
		echo "  ✓ learningclaw 已同步"
	@cp -r deploy/claw-configs/productionclaw \
		deploy/helm/hci-platform/claw-configs/productionclaw && \
		echo "  ✓ productionclaw 已同步"
	@echo "✅ 同步完成（请运行 helm upgrade 让更改生效）"

## 检查 deploy/claw-configs/ 与 Helm chart 副本是否一致
check-claw-configs:
	@echo "检查 claw-configs 一致性..."
	@diff -r deploy/claw-configs/learningclaw \
		deploy/helm/hci-platform/claw-configs/learningclaw && \
		echo "  ✓ learningclaw 一致" || \
		echo "  ⚠️  learningclaw 不一致，请运行 make sync-claw-configs"
	@diff -r deploy/claw-configs/productionclaw \
		deploy/helm/hci-platform/claw-configs/productionclaw && \
		echo "  ✓ productionclaw 一致" || \
		echo "  ⚠️  productionclaw 不一致，请运行 make sync-claw-configs"

## 同步 database/migrations/ 到 Helm ConfigMap（新增迁移文件后必须运行）
db-sync:
	@echo "=== 同步数据库迁移到 ConfigMap ==="
	@bash scripts/ci/sync-db-migrations.sh
	@echo "✅ 完成。请将 db-migrations-configmap.yaml 纳入本次 commit。"

## 检查迁移文件是否已同步 ConfigMap，并检测重复 version 号（CI 同款检查）
db-check:
	@bash scripts/ci/check-db-migrations-sync.sh

## 在本地 Docker PostgreSQL 中执行全量迁移，验证所有 SQL 语法正确（提交 PR 前必须通过）
## 用法：make db-migrate-test
## 依赖：Docker 已运行，dbmate 已安装（brew install dbmate 或 curl 安装）
db-migrate-test:
	@echo "=== 本地迁移执行验证（全量，使用临时 PostgreSQL 容器）==="
	@docker run --rm -d \
		--name hci-migrate-test-pg \
		-e POSTGRES_USER=hci \
		-e POSTGRES_PASSWORD=hci_test \
		-e POSTGRES_DB=hci_test \
		-p 15432:5432 \
		postgres:15 2>/dev/null || true
	@echo "等待 PostgreSQL 就绪..."
	@sleep 3 && docker exec hci-migrate-test-pg pg_isready -U hci -d hci_test -q || sleep 3
	@DATABASE_URL="postgres://hci:hci_test@localhost:15432/hci_test?sslmode=disable" \
		dbmate --migrations-dir database/migrations up \
		&& echo "✅ 全量迁移执行成功" \
		|| (echo "❌ 迁移失败，查看上方错误"; docker stop hci-migrate-test-pg 2>/dev/null; exit 1)
	@echo "=== 验证核心表 ==="
	@PGPASSWORD=hci_test psql -h localhost -p 15432 -U hci -d hci_test \
		-t -c "SELECT string_agg(tablename, ', ' ORDER BY tablename) FROM pg_tables WHERE schemaname='public';"
	@docker stop hci-migrate-test-pg 2>/dev/null || true
	@echo "✅ db-migrate-test 完成"
