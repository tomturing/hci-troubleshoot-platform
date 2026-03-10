# HCI 智能排障平台 — 项目规范

> **本文件是所有 AI Agent（Claude Code / Codex CLI / Gemini CLI）的项目层规范文件。**
> `./CLAUDE.md` 是本文件的符号链接，确保 Claude Code 读到相同内容。
> `./CLAUDE.local.md` 存放个人本地配置（不提交 git）。
> 全局编码规范见 `~/.claude/CLAUDE.md`，全局避坑指南见 `~/.claude/pitfalls/`。

---

## 启动时必读文件

**在开始任何工作前，请依次读取以下文件：**

1. `.vk/workflow.md` — 通用多 Agent 并行开发工作流规范（角色、交叉审查、分支命名）
2. `~/Workflow/multi-agent-workflow/CLAUDE.md` — 调度器开发规范（含踩坑历史）
3. `~/Workflow/multi-agent-workflow/docs/04_VK_MCP手册.md` — VK MCP Server API 完整参考

> 如以上路径不可访问（在容器/CI 环境中），请查阅 `.vk/workflow.md`（已复制到本仓库）。

---

## 1. 项目概述

**HCI 智能排障平台** — AI 驱动的超融合基础设施运维故障诊断系统。

- 用户创建工单描述故障 → AI 助手多轮对话引导排障 → 建议命令和操作步骤 → 形成可复用知识库
- 当前版本：v2.1.0（MVP 全栈可用）

---

## 2. 技术栈

| 层 | 技术 |
|---|------|
| 后端 | Python 3.12, FastAPI, SQLAlchemy, asyncpg |
| 前端 | Vue 3, TypeScript, Vite, Element Plus |
| 数据库 | PostgreSQL 15, Redis 7 |
| 部署 | Docker Compose (开发), K3s + Helm (生产) |
| 可观测性 | OpenTelemetry, Loki, Tempo, Grafana |
| 包管理 | uv (Python), pnpm (前端) |

---

## 3. 目录结构与模块边界

```
hci-troubleshoot-platform/
├── backend/
│   ├── api-gateway/          # 流量入口、路由代理、WebSocket  [独立 Workspace]
│   ├── case-service/         # 工单全生命周期 CRUD + 状态机    [独立 Workspace]
│   ├── conversation-service/ # 对话管理、SSE 流式            [独立 Workspace]
│   ├── scheduler-service/    # Pod 热备池调度                [独立 Workspace]
│   └── shared/               # 共享代码（models, utils, db）  [⚠️ 需最先完成]
├── frontend/
│   ├── customer/             # 客户端对话 UI                 [独立 Workspace]
│   ├── admin/                # 管理控制台                    [独立 Workspace]
│   └── shared/               # 共享类型 + API 客户端          [⚠️ 需最先完成]
├── adapters/                 # CLI→OpenAI 适配器
├── database/                 # init_schema.sql
├── deploy/                   # Docker + Helm + 可观测性
├── scripts/                  # 自动化脚本
├── tests/                    # 根级测试
├── docs/                     # 设计文档
├── .vk/
│   ├── workflow.md           # 通用工作流规范（引用）
│   └── prompts/              # Agent 提示词模板
├── CLAUDE.md                 # 本文件（项目规范，提交 git）
├── AGENTS.md                 # → CLAUDE.md（符号链接）
└── CLAUDE.local.md           # 个人本地配置（不提交 git）
```

### 模块所有权规则

- `shared/` 模块修改需最高优先级完成，其他模块依赖它
- 每个微服务（`backend/xxx-service/`）是独立的 Workspace 单元
- 前端双应用（`customer/` + `admin/`）可并行，但共享类型变更需先完成
- `database/init_schema.sql` 修改必须附带迁移说明

---

## 4. 编码规范

- 代码注释**必须使用**中文
- Git commit 消息**必须使用**中文
- 所有请求日志**必须使用** trace_id（W3C traceparent 自动传播）
- 数据库表设计**必须包含** trace_id 字段
- 所有新增模块**必须**进行可观测性设计（指标、日志、链路追踪）
- Python: `ruff` 做 lint + format，`target-version = "py312"`, `line-length = 120`
- TypeScript: ESLint + Prettier

---

## 5. 构建/测试命令

```bash
# 安装依赖
make install              # uv sync + pnpm install

# 开发环境
make dev-up               # Docker Compose 启动
make dev-down             # Docker Compose 停止

# 测试（按服务隔离运行，避免 app/ 命名空间冲突）
make test                 # 全部测试
uv run pytest tests/ -q   # 根级测试
uv run pytest backend/api-gateway/tests/ -q         # 单服务测试
uv run pytest backend/conversation-service/tests/ -q

# 代码质量
make lint                 # ruff check
make quality-gate         # 完整质量门禁
make conflict-check       # worktree 冲突检测
make post-merge           # 合并后集成验证

# Vibe Kanban
make vk                   # 启动 Vibe Kanban
```

### VK 仓库脚本配置

- **Setup Script**: `uv sync && cd frontend && pnpm install`
- **Cleanup Script**: `bash scripts/agent-quality-gate.sh`
- **Dev Server**: `make dev-up`

---

## 6. 禁止操作清单

| 禁止操作 | 原因 |
|---------|------|
| 删除 `backend/shared/` 下的模型定义 | 多个服务依赖 |
| 直接修改 `database/init_schema.sql` 而不提供迁移脚本 | 生产数据安全 |
| 修改 `deploy/helm/` 中的 Secret 值 | 安全敏感 |
| 在代码中硬编码 API Key / Token | 安全规范 |
| 修改 `pyproject.toml` 的 Python 版本要求 | 全局影响 |
| 删除或重命名已有的 REST API 路径 | 前后端兼容性 |

---

## 7. 工作前必读（避坑指南）

> **规则：在编写或审查对应类型的代码前，必须先读取相关避坑指南。**

经验库统一存放在 `~/Workflow/multi-agent-workflow/agent-global/pitfalls/`。

Claude Code 用户：`~/.claude/pitfalls/`（symlink 到上述目录）会自动加载。
Codex / Gemini 用户：请根据下表主动读取对应文件。

| 触发场景 | 指南文件 |
|---------|----------|
| **任何涉及进程/状态/外部服务的问题排查** | `agent-global/pitfalls/debugging.md` |
| **网络/服务访问异常** | `agent-global/pitfalls/network-service-check.md` |
| Shell 脚本、Makefile | `agent-global/pitfalls/shell.md` |
| Python 代码（ORM、异常、数据类） | `agent-global/pitfalls/python.md` |
| 前端代码（pnpm、TypeScript、Vue） | `agent-global/pitfalls/frontend.md` |
| K8s/K3s 镜像、Helm、网络 | `agent-global/pitfalls/k8s.md` |
| dispatcher / 状态机 | `agent-global/pitfalls/dispatcher.md` |

跨设备同步经验库：`cd ~/Workflow/multi-agent-workflow && make sync-knowledge`
