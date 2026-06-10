# HCI 智能排障平台 — 项目规范

> **本文件是所有 AI Agent（Claude Code / Codex CLI / Gemini CLI / Antigravity IDE）的项目层规范文件。**
> `./CLAUDE.md` 是本文件的符号链接，确保 Claude Code / Antigravity IDE 读到相同内容。
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
- **工单 Q2026061002370 诊断执行失败修复**：
  - 数据库：`database/desired_schema.sql` 补齐 `fact.trace_id` 字段及索引，解决 `FactStore` 写入时因字段缺失导致 SQL 报错
  - Helm：`agent-service` 注入 `SCP_BASE_URL` 与 `SCP_API_KEY` 环境变量；`secret.yaml` 中渲染 `SCP_API_KEY`；在 `values.yaml` 中定义二者默认值为空以保证环境兼容性
  - 环境：开启 `agentService.externalDns` 以解决大模型调用时的 DNS 解析超时问题
- 前端工具栏优化：工单信息 Popover（含 ID/工单号）→ 关闭工单 → SSH终端（Monitor 图标），终端历史按钮移入 TerminalPanel header-actions
- 环境采集命令更新：`task get -s failed -l 10`（仅失败任务）；后端字段映射已支持整数 status/urgent_type 与 Unix 时间戳（PR #285）
- **KBD 管理页面搜索增强**（PR #328）：
  - 支持按案例 ID 精准搜索、标题关键字模糊搜索
  - 显示顺序按导入时间倒序（新数据在前）
  - 前端通过 `support_id` 本地拼接 URL，替代已删除的 `support_url` 字段
- **数据库字段清理**（PR #328）：
  - `kbd_entry` 删除 `support_url`、`archived_at`（冗余字段）
  - `sop_document` 删除 `tree_scenario_name`、`tree_total_node_count`、`tree_generated_at`（已合并入 `tree_json`）
- **KBD 分类下拉框修复**（PR #329）：
  - 前端 `fetchCategories()` 解析 API 响应结构修正，增加空值兜底
  - Vision LLM prompt 抽离为独立文件，增加文件存在性检查
- **KBD 管理页面 UI 优化**（PR #330）：
  - 导入时间改用 `updated_at`（反映 override 重跑后的更新时间）
  - 分类筛选改为下拉选择框，支持搜索/清空，复用 `categoryOptions`
- **KBD 管理页面排序优化**（PR #331）：
  - KBD 列表排序改为 `updated_at DESC, id DESC`，最新更新的数据排在前面
  - Vision LLM prompt 强化表格/告警截图规则，增加正确/错误示例
  - 修复 `image_proc.py` 解析正则，支持带装饰线的新输出格式，告警截图每行合并为 `值1 | 值2 | ...`
- **SOP 管理页面决策树可视化**（PR #350）：
  - 点击「查看」按钮后，弹窗中渲染多叉决策树结构
  - 使用 `el-tree` 组件展示节点层级、前置条件、诊断方法、解决方案
  - 字段名统一：`chunk_count` → `tree_leaf_count`（节点数）
  - 决策树"有警告"状态时提供查看告警详情入口（Warning 图标）
  - 删除所有分块/chunk/embedding 相关的过期概念和文案
- **SOP 查看弹窗优化**（PR #351）：
  - 基础信息表格中"决策树"字段改为"节点数"，显示 `tree_leaf_count` 数值
  - 有警告时在节点数旁显示告警标签和查看按钮，点击可查看告警详情
  - 后端详情接口添加 `tree_validation_status` 和 `tree_validation_issues` 字段返回
- **SOP 决策树前置条件显示优化**（PR #352）：
  - 前置条件改为块状布局，每个前置条件单独一个白色卡片
  - 内容使用 `pre-wrap` 样式，保留换行和空格，正确显示多行内容（包括代码块）
- **AI 空响应兜底修复**：
  - TriageAgent 增加 LLM 空内容检测，返回友好提示而非空白气泡
  - agent-service `_event_stream` 增加空流检测，无内容时返回结构化 error 事件
  - conversation-service 增加空响应兜底，避免前端渲染空白消息
- **KBD 图片列表渲染优化**（PR #364）：
  - 前端从 `images_json`（权威数据源）渲染图片列表，而非从 `content_md` 解析
  - 添加 `parseImagesJson` 函数解析 desc.txt v3 格式（BACKGROUND/TYPE/FULL_TEXT/DESCRIPTION）
  - 图片按序号排序，accordion 卡片展示，解决部分案例多张截图只显示一张的问题
  - 后端 API 返回 `images_json` 字段，包含每张图片的完整描述信息
- **工单管理分类选择框与 S0 Triage 4+1 交互体验优化** (PR #370):
  - 前端工单编辑「工单分类」由输入框升级为 `el-select` 下拉检索框，实现与 SOP 分类编辑的一致样式与体验
  - 提取公共 `useCategories` composable，在工单管理、KBD 审查、SOP 管理页面中统一引用该公共分类函数加载逻辑
  - 修复 S0 阶段流式推理和 ACP 卡片的重复显示（Bug a）和由于落库时差导致的页面刷新乱序（Bug b）
  - 统一 S0 交互样式为 `4+1` 模式，提供 4 个高置信度选项与一个带症状补全的“以上都不是”选项，彻底解决点击“以上都不是”时的 LLM 异常报错（Bug c）
- **S0 Triage 4+1 按钮引导词正则判定范围扩容** (PR #371):
  - 扩展前端 `MessageBubble.vue` 对 AI 选项引导语的匹配范围（添加“请补充”、“请问”等高频提问前缀），彻底解决部分 4+1 选项文本未能渲染为可点击按钮的问题，实现与历史交互一致的“禁用/不可选”和补充描述（+1 模式）特性
- **KBD 溢出 DOM 兼容与前端贪婪解析修复** (PR #375):
  - 升级 `data-pipeline/kbd/converter.py` 板块解析器，使用平铺子节点动态遍历合并机制，自动提取并合入溢出在容器外的排障正文和截图，完美向后兼容。
  - 修复 `KbdReviewView.vue` 中由 `inDescription` 状态导致的 Markdown “贪婪吸入”解析 Bug，严格限定非空且非 `>` 开头的行为卡片结束标志，防止游离排障步骤被误吞进折叠面板内。
  - 修复 `KbdReviewView.vue` 内 `renderMarkdown` 转义星号渲染冲突，使用 DOMPurify + marked AST 解析替代手写正则，完美修复分割线及排障步骤中的反斜杠裂变与星号吞噬 Bug。
- **Prompt/工具/技能管理页面 Dialog 和布局样式优化** (PR #391):
  - 修复 Prompt/工具/技能 Dialog 中 teleported 弹窗 scoped 样式失效及 full screen 模式下按钮定位异常的 Bug
  - 优化 Prompt 管理页面主卡片最小高度及 Prompt 预览最大高度，杜绝列表下方的无效留白
  - 将工具管理中”功能描述”字段调整到右栏上方，均衡左右高度并拓宽编辑体验
- **技能管理列表排版优化** (PR #392):
  - 优化技能管理列表中的“Skill 标识”和“操作”按钮排版，防止折行，保持单行显示
- **技能编辑 Markdown 预览修复** (PR #393):
  - 修复技能编辑弹框中右侧“预览”由于未引入 marked 和 dompurify 模块导致的 Markdown 原文未解析 Bug
- **技能预览列表缩进与 SOP 环境变量自动注入优化**:
  - 修复技能管理页面 Markdown 预览中由于全局样式 reset 导致的列表（ul/ol/li）无前缀符号及缩进丢失问题。
  - 优化 SOP 执行实例创建接口 `sop_create_execution` 响应，使其包含并返回已解析的环境变量。
  - 优化排障 Agent `investigation_agent` 的系统提示词构建，在新执行实例的系统提示词中注入 `【已知变量】`，避免 AI 仍向用户手动询问已收集的信息。
  - 修复变量池 `sop_request_variable` 获取策略判定逻辑，支持并正确识别 `env:xxx` 格式的环境变量注入策略。
- **SOP 工具执行器参数适配修复**：
  - 修复 `SopToolExecutor.execute` 签名缺少 `**kwargs` 导致在 ReAct 循环中被调用时抛出 `TypeError: got an unexpected keyword argument 'conversation_id'`，彻底解决工具调用通道报错阻断的问题。
- **SOP 交互变量值提交失效问题修复**：
  - 修复前端 `MessageBubble.vue` 和 `InteractiveRequestCard.vue` 提交 `interactive-response` 时缺失 `kind` 和 `metadata` 导致路由错误的问题。
  - 在后端 `submit_interactive_response` 增加针对 `variable_input`/`variable_confirm` 的处理，直接将变量写入 SOP 变量库中，并恢复执行状态为 `active`。
  - 前端接收到变量提交流程成功后，自动调用 `sendMessage` 重新发送变量值，触发后端 HTP Agent 的 ReAct 推理循环从中断位置恢复继续运行。
- **SOP 执行路由漂移与恢复稳定性修复**：
  - 修复排障 Agent 在多轮对话中，由于用户发送的“继续”或命令回显数据与 SOP 文档内容在语义匹配上发生偏差，导致 `route_by_category` 三轨路由发生漂移、误判为非 SOP 轨道而回退到 fallback 推理模式的缺陷。
  - 优化：当检测到活跃的 `sop_resume_context`（正在执行的 SOP）时，直接绕过三轨路由匹配，通过 document_id 获取 SOP 详情，确保 Agent 在会话周期内牢牢锁定在 SOP 导航模式中。
- **工具调用可视化、变量交互重构与原始环境注入**：
  - **工具调用可视化与自动执行**：后端 `react_engine.py` 在工具执行生命周期中（执行前中后）广播 `tool_call` 与 `tool_result` 阶段事件并透传统一 `exec_id`。前端支持全局“自动执行”模式选择（Off / Safe-only / Aggressive），在满足风险级别时自动回复确认。对话流中新增工具卡片，以黑底 Terminal Console 折叠渲染命令执行日志与耗时。
  - **变量输入/确认交互重构**：将 `variable_input` 升级为行内表单，对 `validation_pattern` 正则及必填项进行失焦与实时校验，不合法时红框报错并禁用提交按钮。将 `variable_confirm` 升级为左右双栏对比，左栏一键快捷确认系统推荐值，右栏支持微调修改与实时校验。
  - **原始环境注入与向下兼容**：后端在开启 `USE_RAW_ENVIRONMENT_CONTEXT` 时直接将数据库的原始字典/JSON 喂给 LLM 提升推理准确率。在 `sop_execution.py` 中增加 is_raw 兼容层，自动将 Unix 时间戳、状态整型、紧急度等映射为 SOP 规则可识别的语义值。
- **SOP 工具执行器参数冲突与布尔变量归一化修复**：
  - 修复 `SopToolExecutor.execute` 在委派调用默认执行器时，由于 `**kwargs` 携带 `conversation_id` 造成的多值传递错误 `got multiple values for keyword argument 'conversation_id'`。
  - 在 `submit_interactive_response` 接口与 `sop_variable_response` 路由中，对 `boolean` 类型的变量提交值进行强制归一化（Truthful 词汇转为 `"true"`，Falsy 词汇转为 `"false"`），彻底解决大模型由于布尔值字符串（如“是”/“否”）不符合条件表达式规则而无法推进决策树节点的缺陷。
- **废弃技能数据与临时文件清理**（PR #411）：
  - 物理删除 `backend/kb-service/data/` 技能文件目录和 `sop_matcher` 废弃匹配器逻辑。
  - 修复 `health.py` 中数据库检查的探针变量拼写 Bug（修正 `db` 为 `database_manager`）。
  - 在 `kb-service` 和 `conversation-service` 契约验证中彻底下线 `/sop/match` 废弃路由。
  - 移除 Docker Compose 和 Helm 模板中对技能数据卷挂载及 `SOP_SKILLS_DIR` 环境变量的声明。
  - 清理误提交的 `.kb-service-portforward.pid` 与 Word 临时所有者文件，并在 `data-pipeline/kbd/.gitignore` 中新增 `*.pid` 过滤规则。
- **部署策略优化**：
  - 恢复 staging 自动化部署和自动同步，GitHub 提交 PR 合并至 main 后将自动同步镜像 tag 至 staging 环境，确保 schema 更新及时覆盖，同时保持 prod 环境为手动同步。
- **db-seed PostSync Hook UNIQUE 约束修复**（hotfix/db-seed-system-prompt-unique-constraint）：
  - `desired_schema.sql` 补齐 `system_prompt.name` 字段的 `CONSTRAINT system_prompt_name_key UNIQUE (name)` 声明，使 `ON CONFLICT (name)` 种子 SQL 可正常执行。
  - `desired_extras.sql` 新增幂等 `DO $$` 块，存量环境（未包含该约束的旧部署）在下次 ArgoCD deploy 时自动补齐约束，无需人工干预。
  - staging 环境已直接热修复数据库约束并重命名为 `system_prompt_name_key`，db-seed Job 下次重建后可正常完成。
- **API 网关命令反馈 `exec-result` 路由与鉴权修复**：
  - 修复 API 网关 (`api-gateway`) 缺少 `/api/conversations/{conversation_id}/exec-result` 代理路由，导致前端命令执行结果无法回传的问题。
  - 支持对不携带 Bearer 鉴权头的客户侧匿名请求，在网关层自动填充 `Bearer client-session-placeholder-token` 进行安全绕过，契合 `conversation-service` 端 MVP 阶段的临时鉴权需求。

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

**数据来源**：
- **环境**：从 `argocd` namespace 的标签 `hci.env.role` 获取（dev/staging/prod）
  ```bash
  kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'
  ```
- **hostname**：完整主机名，转小写
  ```bash
  hostname | tr '[:upper:]' '[:lower:]'
  ```
- **工具**：`claude`、`gemini` 或 `copilot`（当运行于 Antigravity IDE 且存在环境变量 `ANTIGRAVITY_AGENT=1` 时，脚本会自动识别并默认设定为 `gemini`）

**实现方式**：使用 `gcm` 和 `gpr` 函数（已配置在 `~/.my_custom_configs`）：

```bash
# Claude Code 提交 commit（默认路径）
gcm "fix: 修复问题"

# Gemini (Antigravity IDE) 提交 commit
# 在 Antigravity 终端中执行 gcm 即可（已基于环境变量自适应），或显式指定：
gcm-g "fix: 修复问题"
# 或者：
AGENT=gemini gcm "fix: 修复问题"

# GitHub Copilot 提交 commit
AGENT=copilot gcm "feat: 新功能"

# Claude Code 创建 PR（自动添加 labels）
gpr "fix: 修复问题"

# Gemini (Antigravity IDE) 创建 PR
# 在 Antigravity 终端中直接执行 gpr 即可，或显式指定：
gpr-g "fix: 修复问题"

# GitHub Copilot 创建 PR
AGENT=copilot gpr "feat: 新功能"
```

> ⚠️ **注意（GitHub Copilot 执行时）**：
> 1. `gpr` 在无自适应变量的环境下默认 `AGENT=claude`，**Copilot 必须显式加 `AGENT=copilot` 前缀**，否则标签打错
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
