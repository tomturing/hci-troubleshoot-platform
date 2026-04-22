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
- **Git commit 消息和 PR 必须追加环境与工具标识**（见下方规则）
- 所有请求日志**必须使用** trace_id（W3C traceparent 自动传播）
- 数据库表设计**必须包含** trace_id 字段
- 所有新增模块**必须**进行可观测性设计（指标、日志、链路追踪）
- Python: `ruff` 做 lint + format，`target-version = "py312"`, `line-length = 120`
- TypeScript: ESLint + Prettier

### Git 推送规则（强制）

#### 文档门禁
改动 `backend/`、`frontend/`、`deploy/`、`scripts/`、`database/`、`.github/workflows/` 时，
**必须在同一 commit/PR 中**同步更新 `docs/`、`README.md`、`AGENTS.md` 或 `CLAUDE.md` 至少一项。
否则 CI `docs-governance` job 失败，PR 无法合并。

#### 分支与 PR 流程
- main 分支有保护规则，**禁止直接推送**，必须通过 PR
- 提交流程：创建 feature/hotfix 分支 → 推送远程 → 创建 PR → CI 全绿后合并

#### PIT-023：并发 hotfix 前置检查
创建 hotfix 分支**前**必须先执行：
```bash
gh pr list --state open
```
确认无其他 PR 正在修改同一目录。有并发 PR 时先协调合并，避免产生重复配置块。

#### PIT-024：安全基线改造必须分批 PR
全量修改 `securityContext`、`probe`、`resources.limits` 时，
必须按负载类型（**nginx / Python / Node.js**）拆成独立 PR，
不可一次提交跨多种运行时的安全基线变更。

#### PIT-025：修改 runAsNonRoot 前确认镜像文件系统
修改 `securityContext.runAsUser` 或 `runAsNonRoot` 前，确认镜像在非 root 下的写权限需求：
- **nginx 官方镜像**：需写 `/var/cache/nginx` 和 `/var/run`，必须挂载 `emptyDir` 覆盖这两个路径
- Python/Node.js 镜像：确认应用日志、临时文件写入路径的权限

### Git Commit/PR 标识规则

**所有 commit 消息末尾必须追加 `[env:<环境>:<hostname>][agent:<工具>]` 标识。**

**所有 PR 必须添加对应的 labels：`env:<环境>:<hostname>` 和 `agent:<工具>`。**

格式：
```
<commit message>

[env:<环境>:<hostname>][agent:<工具>]
```

示例：
```
fix: 修复 ArgoCD 升级脚本

[env:dev:gs][agent:claude]
```

**强制执行流程**：
填写 `[env:<环境>:<hostname>]` 前，**必须先执行**以下命令获取当前值：
```bash
# 步骤 1：获取环境（dev/staging/prod）
kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'

# 步骤 2：获取 hostname
hostname | tr '[:upper:]' '[:lower:]'
```
**禁止使用记忆中的值、假设值或旧对话中的值**。每次都必须重新执行命令验证。

- **工具**：根据当前会话使用的工具填写 `claude` 或 `copilot`

**实现方式**：使用 `gcm` 和 `gpr` 函数（已配置在 `~/.my_custom_configs`）：

```bash
# Claude Code 提交 commit
gcm "fix: 修复问题"

# GitHub Copilot 提交 commit
AGENT=copilot gcm "feat: 新功能"

# Claude Code 创建 PR（自动添加 labels）
gpr "fix: 修复问题"

# GitHub Copilot 创建 PR
AGENT=copilot gpr "feat: 新功能"
```

> ⚠️ **注意（GitHub Copilot 执行时）**：
> 1. `gpr` 默认 `AGENT=claude`，**Copilot 必须显式加 `AGENT=copilot` 前缀**，否则标签打错
> 2. `gpr` 生成的 body 是硬编码占位符，**创建 PR 后必须立即用以下模板补写完整描述**：
>    ```
>    ## 问题
>    （描述触发原因、影响范围、复现路径）
>    ## 修复
>    （按子任务分节列出具体改动）
>    ## 影响文件
>    （表格：文件 | 变更类型 | 说明）
>    [env:dev:sz][agent:copilot]
>    ```
>    补写命令：`gh api --method PATCH /repos/{owner}/{repo}/pulls/{num} -f body="$(cat /tmp/pr_body.md)"`

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
- **数据库初始化**（新环境）: `psql -f database/desired_extras.sql && atlas schema apply --env local --auto-approve`
- **Cleanup Script**: `bash scripts/ci/agent-quality-gate.sh`
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
> **规则：在排查问题前，必须优先读取相关避坑指南。**

避坑指南权威来源：`docs/deploy/pitfalls/`（部署类）和 `docs/verify/pitfalls/`（验证类）。

**所有 Agent 按场景读取对应索引：**
- 部署类：首先读取 `docs/deploy/pitfalls/_index.md`
- 验证类：首先读取 `docs/verify/pitfalls/_index.md`

Codex / OpenCode / Gemini 用户：请根据下表主动读取对应文件。

| 触发场景 | 指南文件 | 关键条目 |
|---------|----------|---------|
| **任何涉及进程/状态/外部服务的问题排查** | `docs/verify/pitfalls/debugging.md` | 原则一~六 |
| **网络/服务访问异常（502/503/超时/SSL/LLM）** | `docs/deploy/pitfalls/network-service-check.md` | §一~十一 |
| Shell 脚本、Makefile、CI 脚本 | `docs/deploy/pitfalls/shell.md` | PIT-001,002 |
| Python 代码（ORM、异常、数据类） | `docs/verify/pitfalls/python.md` | PIT-003,004,009,040,041 |
| 前端代码（pnpm、TypeScript、Vue）/ Docker 构建 | `docs/verify/pitfalls/frontend.md` | PIT-005,023,025,028,029 |
| dispatcher / 状态机 / 幂等资源管理 | `docs/verify/pitfalls/dispatcher.md` | PIT-006,007,008 |
| K8s/K3s 镜像导入、Helm、网络、HostPath | `docs/deploy/pitfalls/k8s.md` | PIT-014~019,021,022,024,034,037,038 |
| OpenClaw 401/崩溃/WebSocket/AI 超时 | `docs/verify/pitfalls/openclaw.md` | PIT-010,013,026,027,030,032,035 |
| Grafana 重定向/Ingress/iframe | `docs/deploy/pitfalls/grafana.md` | PIT-011,012,020,036 |

> 新发现的坑：先在对应 `_index.md` 分配编号（部署类 D- 前缀，验证类 V- 前缀），再写入对应分类文件，同一 commit 提交。

---

## 8. 服务间 API 变更规范（G-4）

> **违反此规范会导致服务间契约破裂和运行时 422 / KeyError 错误。**

### 8.1 变更三步法

所有修改 `backend/shared/models/` 或微服务 HTTP 接口的 PR **必须**遵循：

```
步骤 1：先更新共享类型（backend/shared/models/），提交并合并
步骤 2：更新提供方实现（新字段向后兼容，不立即删除旧字段）
步骤 3：更新所有调用方代码，移除对旧字段的依赖
步骤 4：提交 PR，CI 契约测试（tests/contract/）必须全部通过
```

### 8.2 破坏性变更禁令

| 禁止操作 | 原因 | 正确做法 |
|---------|------|---------|
| 直接重命名返回字段 | 调用方运行时 KeyError | 先增加新字段，一个 Release 后再删旧字段 |
| 删除 Pydantic 模型字段 | schema 序列化失败 | 先标注 `deprecated=True`，再删除 |
| 改变字段类型（str → int） | 类型校验报错 | 新增独立字段 + 过渡期兼容 |
| 修改 API path 不保留旧路径 | 前端 404 | 保留旧路径（返回 301 或同等处理）至少一个 Release |

### 8.3 共享类型版本管理

```python
# backend/shared/models/__init__.py
__schema_version__ = "2.1.0"
# 升级规则：
#   patch (2.1.x)  — 新增可选字段（向后兼容）
#   minor (2.x.0)  — 弃用字段（deprecated=True）
#   major (x.0.0)  — 删除已弃用字段（破坏性变更，需整体升级协调）
```

### 8.4 内部服务调用规范

- 所有服务间 HTTP 调用**必须**继承 `backend/shared/utils/internal_http.py` 的 `InternalHTTPClient`
- 调用方**必须**调用 `response.raise_for_status()`，不允许静默忽略错误响应
- 内部认证统一使用 `INTERNAL_API_TOKEN` 环境变量（由 Helm Secret 注入）
