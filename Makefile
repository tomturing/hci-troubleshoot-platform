# HCI智能排障平台 - Makefile
# 依赖管理: uv (https://docs.astral.sh/uv/)

.PHONY: help install dev-up dev-down test lint clean vk quality-gate conflict-check post-merge

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
	uv run pytest backend/conversation-service/tests/ -q
	uv run pytest backend/scheduler-service/tests/ -q
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

quality-gate:
	@echo "运行质量门禁..."
	bash scripts/agent-quality-gate.sh

conflict-check:
	@echo "运行 Worktree 冲突预检..."
	bash scripts/check-worktree-conflicts.sh

post-merge:
	@echo "运行合并后集成验证..."
	bash scripts/post-merge-verify.sh
