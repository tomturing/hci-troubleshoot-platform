# HCI智能排障平台 - Makefile
# 依赖管理: uv (https://docs.astral.sh/uv/)

.PHONY: help install dev-up dev-down test lint clean vk vk-stop vk-restart quality-gate conflict-check post-merge k3s-release k3s-deploy-prod release-observe rollback-drill sync-claw-configs check-claw-configs db-sync db-check local-deploy local-deploy-import

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
	@echo "  [废弃] db-sync / db-check              - dbmate 工具已停用"

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

local-deploy:
	@echo "🔧 本地构建部署（CI 替代）: push 模式（构建 → 推送 ghcr.io → ArgoCD 同步）"
	@echo "SERVICES=${SERVICES:-（全量）}  IMAGE_TAG=${IMAGE_TAG:-（自动生成）}"
	SERVICES="${SERVICES:-}" IMAGE_TAG="${IMAGE_TAG:-}" DRY_RUN="${DRY_RUN:-0}" \
		bash scripts/ops/local-deploy.sh push

local-deploy-import:
	@echo "🔧 本地构建部署（CI 替代）: import 模式（构建 → 导入 K3s → ArgoCD 同步）"
	@echo "SERVICES=${SERVICES:-（全量）}  IMAGE_TAG=${IMAGE_TAG:-（自动生成）}"
	SERVICES="${SERVICES:-}" IMAGE_TAG="${IMAGE_TAG:-}" DRY_RUN="${DRY_RUN:-0}" \
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

## [已废弃] 原 dbmate 迁移同步，自 v6.3 起由 Atlas 接管
## 历史迁移文件已归档至 docs/archive/db-migrations-history/
db-sync:
	@echo "⚠️  db-sync 已废弃（dbmate → Atlas v6.3）"
	@echo "    Schema 变更请修改 database/desired_schema.sql 并运行 atlas migrate diff"
	@echo "    等价命令: atlas migrate diff --env local <name>"
	@exit 0

## [已废弃] 原 dbmate 迁移检查
db-check:
	@echo "⚠️  db-check 已废弃（dbmate → Atlas v6.3）"
	@echo "    迁移状态请使用: atlas migrate status --env local"
	@atlas migrate status --env local 2>/dev/null || echo "    (atlas 未安装或无数据库连接，请手动执行)"
	@exit 0
