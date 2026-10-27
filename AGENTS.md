# HCI 智能排障平台 — 项目规范

> **本文件是所有 AI Agent（Claude Code / Codex CLI / Gemini CLI / Antigravity IDE）的项目层规范文件。**
> `./CLAUDE.md` 是引用本文件的薄壳（内容为 `@./AGENTS.md`），确保 Claude Code / Antigravity IDE 读到相同内容。本文件是规范的唯一事实源。
> `./CLAUDE.local.md` 存放个人本地配置（不提交 git）。
> 全局编码规范见 `~/.claude/CLAUDE.md`，全局避坑指南见 `~/.claude/pitfalls/`。

---

## 启动时必读文件

**在开始任何工作前，请优先读取本仓库内的规范文件：**

1. `AGENTS.md` — 项目层规范。本文件是 Claude Code / Codex / Gemini / Copilot 等 Agent 的统一入口。
2. `docs/deploy/pitfalls/_index.md` 或 `docs/verify/pitfalls/_index.md` — 按任务场景读取对应避坑指南索引，再读取具体指南。
3. 涉及 KBD、关键信号、语义推荐或审核发布时，从 [KBD 与关键信号文档入口](docs/solution/knowledge-base/README.md) 按职责读取现行主文档；旧路径仅作兼容跳转，历史方案不作为当前执行规范。

---

## 1. 项目概述

**HCI 智能排障平台** — AI 驱动的超融合基础设施运维故障诊断系统。
用户创建工单描述故障 → AI 助手多轮对话引导排障 → 建议命令和操作步骤 → 形成可复用知识库。

> **历史修复复盘已归档，本文件不再承载过程记录。**
> 2026-H1/H2 共 116 条「根因 / 修复 / 测试守护 / 教训」已**逐字**搬移至（正文文字不改，
> 仅相对链接前缀按新位置适配），
> [docs/solution/events/2026-10-21-AGENTS历史修复复盘归档.md](docs/solution/events/2026-10-21-AGENTS历史修复复盘归档.md)
> （文件首含 116 条主题索引，按关键词检索原文即可）。
> **此后新事件的去向**：稳定结论蒸馏进对应 `docs/solution/**` 现行文档；过程与决策依据写
> `docs/**/events/YYYY-MM-DD-*.md`；可复现的技术坑按 §7 登记 `pitfalls/`。本文件只保留**规范**。

### 1.1 当前核心链路（以代码为准）

- 知识消费路径：确认分类 → 有 SOP 且未放弃则续跑 SOP；否则同分类 KBD → 强信号走 CDD 证据门禁 → 无可执行信号或证据不足走受控语义兜底 / 升级人工。
- **信号双轨（两条完全独立链路 + 结论级晚融合仲裁）**：关键信号 `signals_json`（现场可采集证据）与根因信号 `kbd_entry.root_signal_json`（结论侧定位锚点；v2 为 JSON 数组、整篇 `content_md` 输入、不限条数）。各自 `compile_signal_plan` → 各自归约与结论 → 仲裁顺序固定：**EXCLUDE 全局否决 → 任一 SUPPORTED → 任一 CANDIDATE → 全 REJECTED → 全 NOT_EXECUTABLE → INCONCLUSIVE**。
- 模式开关 `KB_SIGNAL_TRACK_MODE` ∈ `{dual（默认）, process_only}`；`process_only` 是根因链路的一键回滚档。非法值回落 `dual` 并告警。
- 主题入口：KBD / 关键信号 / 根因信号 / 语义推荐 / 审核发布，一律从 [知识库文档入口](docs/solution/knowledge-base/README.md) 进入。

### 1.2 跨任务硬约束（违反即返工；每条都曾在归档复盘中付出过真实代价）

**数据库与迁移**

1. 凡 `database/desired_extras.sql` 中**无条件重建**的对象（枚举型 CHECK 尤甚），扩值必须 `desired_schema.sql` 与 `desired_extras.sql` **同改**：CI 执行序列是 `extras → atlas apply → extras`，只改 schema 必然残留 diff。
2. Prompt 种子**必须**放 `database/seeds/02_system_prompts.sql`（由 Helm db-seed-job PostSync 应用）。迁移链路只应用 `seeds/`、`data-migrations/`、`desired_schema.sql`、`desired_extras.sql` 四类输入，放错位置会让生产路由 100% 抛 `PromptLoadError`。
3. AI 派生元数据列（`root_signal_json`、`ai_category_id`）不入 `kbd_revision`、不动 content_md/embedding/tsv，写回路径**禁止**使用 `require_mutable_kbd`（published 回刷必撞 409）。

**服务契约与网关**

4. 任何后端服务新增 admin 前端可达路由，**必须**同步在 api-gateway 对应 router 中**逐条枚举**补代理，否则端点「存在但 404」；下游 422 需原样透传，不得被网关错误兜底掩盖。

**测试与 CI**

5. 前端单测**不得**跨语言读取后端源码（CI「前端检查」作业使用 `sparse-checkout: frontend`，仓根只有 `frontend/`）；跨语言常量一致性校验必须放在全量检出的后端测试里。
6. 「本地全量测试全绿」不等于 CI 拓扑下可跑：写跨目录读写、幂等或基线断言前，必须先核对该 CI 作业的 checkout 范围与执行序列。
7. Helm 模板新增任何运行时变量，**必须**同步登记 `deploy/config/config-contract.yaml` 的 `helm_equivalents`（含 `value_paths`），否则 `REQUIRE_HELM=1 make config-contract-check` 失败。

**LLM 与抽取门禁**

8. 凡「可选但只接受具体类型」的输出字段，必须预设 LLM 直出显式 `null`（键存在与键缺失在 JSON Schema 中不同值），并在**保存边界**确定性归一；不能指望 Prompt 措辞或重试。
9. 抽取与人工编辑门禁失败**不得**静默降级，也**不得**清空既有数据：违规项只落 rejected 列并显式报错，「清空」仅由提交空集合表达。
10. 冲突度、唯一性等客观度量**不采信 LLM 自报**，一律用现场语料回算。

**领域不变量**

11. 分类树：凡改变分类存在性或顺序的写操作，**必须**在同一事务、唯一 commit 之前调用 `resequence_domain` 物化 code，否则编号出现空洞即破解「顺序即 code」不变量。
12. 根因信号不含生产者：`{{HOST}}`/`{{DATE}}`/`{{VMID}}` 优先取关键链路已完成的 `StepResult`，其次从 `env_context` 确定性派生，派生不出即 `BLOCKED`——**绝不猜值**。
13. SOP 变量守卫：变量未解析的 `tool_call`/`skill_call` 必须硬阻断并转人工，绝不把控制权交回模型；结论节点中无溯源证据的定量数值一律硬拦截。

**前端状态机**

14. 任何「成功后的副作用触发」若经过可能被守卫吞掉的通道，必须先把 UI 状态推进到与后端一致，再尽力触发，且被吞掉时必须有用户可感知的反馈；「清空输入框」绝不可先于状态确认执行。
15. 破坏性/写端点的展示态变更（清空、推进、关闭对话框）必须先取回最新详情再确认成功，避免假报错与脏态残留；服务间契约变更一律遵守 §8.2 禁令。

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
├── backend/                      # 10 个微服务 + shared，各服务自带 pyproject.toml
│   ├── api-gateway/              # 流量入口、路由代理、WebSocket
│   ├── agent-service/            # Agent 执行引擎：工具调用、技能、effect 验证
│   ├── diagnosis-service/        # 诊断会话状态机、证据包、报告生成
│   ├── kb-service/               # 知识库：KBD 条目、审核、信号抽取、SOP
│   ├── conversation-service/     # 对话管理、SSE 流式
│   ├── case-service/             # 工单全生命周期 CRUD + 状态机
│   ├── scheduler-service/        # Pod 热备池调度
│   ├── auth-service/             # 认证鉴权（JWT 签发与校验）
│   └── shared/                   # 共享代码（models, utils, db, security）[⚠️ 需最先完成]
├── frontend/
│   ├── customer/                 # 客户端对话 UI
│   ├── admin/                    # 管理控制台
│   └── shared/                   # 共享类型 + API 客户端 [⚠️ 需最先完成]
├── data-pipeline/                # 离线/批量知识生产层（产出可审核的知识 Proposal）
├── hci_sim/                      # 仿真环境（独立控制面数据库）
├── terminal_bridge/              # SSH Bridge（Go；桌面端与 K3s 共用同一套源码）
├── dsh/                          # DSH（DeepSeek Harness）Web UI 容器镜像
├── evaluation/                   # 评估脚本 + golden tickets + live E2E
├── skills/                       # 排障技能 Markdown（任务解析、告警解析等）
├── tools/ticket-sync/            # 工单同步工具
├── database/                     # desired_schema.sql / desired_extras.sql / seeds
├── deploy/                       # Docker + Helm + 可观测性
├── scripts/                      # 自动化脚本
├── tests/                        # 根级跨服务测试
├── docs/                         # 设计文档
├── AGENTS.md                     # 本文件（项目规范，提交 git）
├── CLAUDE.md                     # → AGENTS.md（符号链接）
└── CLAUDE.local.md               # 个人本地配置（不提交 git）
```

> 各后端服务虽有独立 `pyproject.toml`，但根 `[tool.uv]` **未启用 workspace 成员解析**
> （服务间 httpx/fastapi 版本未统一），因此依赖需按服务分别同步。

### 模块所有权规则

- `shared/` 模块修改需最高优先级完成，其他模块依赖它
- 每个微服务（`backend/xxx-service/`）有独立 `pyproject.toml`
- 前端双应用（`customer/` + `admin/`）可并行，但共享类型变更需先完成
- `database/desired_schema.sql` 或 `database/desired_extras.sql` 修改必须附带迁移说明
- **给已存在数据的表新增 `NOT NULL` 列必须带 DEFAULT**：`atlas schema apply` 会直接下发
  `ADD COLUMN ... NOT NULL`，无默认值时 PostgreSQL 因存量行无法填值而整体失败
  （`column "x" contains null values`），进而阻塞 db-migrate 的 PreSync hook 与环境同步
- **本项目无 Schema 迁移版本目录**：`atlas.hcl` 未配置 `migration_dir`，`atlas migrate *`
  命令无效；`database/atlas-migrations/` 已于 2026-10-24 整体删除（具体依据见
  `database/README.md`）。存量兼容逻辑要么落在 `database/data-migrations/`，要么直接
  表达在 `desired_schema.sql` 的列定义上；变更历史由 Git 承载，禁止重建该目录

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

映射规则由 `scripts/ci/check_module_doc_sync.py` 的 `MODULE_MAPPINGS` 定义，按声明顺序
`startswith` 取**第一个**命中项，因此更具体的前缀/精确文件名必须写在宽泛前缀之前。
`database/` 下除 `database/seeds/`（种子数据是 Prompt/工具定义，实质属 agent-service，
走 agent 文档）外，一律映射到 `docs/solution/数据库设计.md` 或 `docs/task/数据库任务.md`。
**不要为具体文件另设指向其他模块文档的特例**（如曾有的 `database/desired_schema.sql`
→ agent 文档）：它会静默遮蔽宽泛规则，导致改 schema 时门禁拒绝正确的设计文档。
回归测试：`tests/unit/test_module_doc_sync_mapping.py`。

#### 文档腐化门禁（`scripts/ci/check_docs_rot.py`）
改文档时另有一道失效引用门禁，口径按文档层分级（五种引用豁免与完整表见
[`docs/文档管理规范.md`](docs/文档管理规范.md) §九）：

- **现行全量文档层**（非 `docs/archive/` 且路径不含 `events/` 目录段，含仓库根
  `README.md`/`AGENTS.md`）：C1/C2/C3 不论是否本次改动**一律阻断**——该层已归零，
  出现一条就是本次引入的故障。
- **事件层 / 归档层**：只阻断本次改动行上的新增，存量仅警告。史料不得为了 CI 变绿而改写。
- **不要尝试清零事件层存量**（2026-10-26 分诊时实测 527 条，订正后仍余 519 条）：该数字是已写定的治理结论而不是待办债务，
  四档分诊口径与「不做清零」的决定见规范 §8.3.2。只有**被实测证据证伪的事实断言**才能按 §8.3.1
  订正（带「（新建）」「待创建」「如有」的句子断言的是未来，永不可证伪，不得改动）。
- 豁免只认客观事实（邻近的否定语义词、`.gitignore` 命中、`status: archived` 等），
  **不得靠堆词逃检查**；豁免条目会逐条打印供审。
- 新增/改动 `scripts/ci/` 下的门禁脚本与测试时，必须 `grep` 自己的文件名确认已登记进
  `ci.yml` 的 paths-filter / ruff check / ruff format / pytest 四处（未登记 = 测试永不执行，见 V-037）。

#### 变更历史节门禁（`scripts/ci/check_docs_changelog.py`）
实质改动了 `docs/` 现行文档却没在 `## 变更历史` 节补一行，会被这道门禁阻断（规范 §7.1）：

- **新建**现行文档：必须建节且含 ≥1 行日期数据行（首列 `YYYY-MM-DD`）。
- **存量文档**节外改动 ≥ 3 行：必须在节内新增 ≥1 行；尚无该节的须一并补建。
- 小修（节外 < 3 行）、纯删除、事件层/归档层、`*/pitfalls/`、`_index.md` 均不触发。
- **扫描范围有意窄于腐化门禁**：本门禁只含 `docs/` 现行文档，不含仓库根 `README.md`/`AGENTS.md`
  与模块 README（它们不与事件文档互联，变更理由 `git log` 已完整捕获，见规范 §7.1）。
- 机器只判「漏没漏一行」，**判不了行内容有无意义**：凑空行骗得过门禁、骗不过评审
  （四类逃逸面见规范 §九）。

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

[env:dev:gs][agent:codex]
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
- **工具**：必须按实际执行工具填写，当前允许值包括 `codex`、`claude`、`gemini`、`copilot`、`gpt`。
  - Codex / Codex Desktop / Codex CLI：必须使用 `codex`
  - Claude Code：使用 `claude`
  - Gemini / Antigravity IDE：使用 `gemini`
  - GitHub Copilot：使用 `copilot`
  - 直接通过 GPT/Claude API 自动化提交：使用 `gpt`

**实现方式**：使用 `gcm` 和 `gpr` 函数（已配置在 `~/.my_custom_configs`）：

```bash
# Codex 提交 commit / 创建 PR
AGENT=codex gcm "fix: 修复问题"
AGENT=codex gpr "fix: 修复问题"

# Claude Code 提交 commit / 创建 PR
AGENT=claude gcm "fix: 修复问题"
AGENT=claude gpr "fix: 修复问题"

# Gemini (Antigravity IDE) 提交 commit
# 在 Antigravity 终端中执行 gcm 即可（已基于环境变量自适应），或显式指定：
gcm-g "fix: 修复问题"
# 或者：
AGENT=gemini gcm "fix: 修复问题"
AGENT=gemini gpr "fix: 修复问题"

# GitHub Copilot 提交 commit / 创建 PR
AGENT=copilot gcm "feat: 新功能"
AGENT=copilot gpr "feat: 新功能"
```

> ⚠️ **注意**：
> 1. `gcm` / `gpr` 在部分本地环境中可能默认 `AGENT=claude`。Codex、Copilot、Gemini 等非 Claude 工具必须显式加 `AGENT=<工具>` 前缀，防止 commit footer 和 PR label 打错。
> 2. `gpr` 生成的 body 可能是硬编码占位符，**创建 PR 后必须立即用以下模板补写完整描述**：
>    ```
>    ## 问题
>    （描述触发原因、影响范围、复现路径）
>    ## 修复
>    （按子任务分节列出具体改动）
>    ## 影响文件
>    （表格：文件 | 变更类型 | 说明）
>    [env:dev:gs][agent:codex]
>    ```
>    补写命令：`gh api --method PATCH /repos/{owner}/{repo}/pulls/{num} -f body="$(cat /tmp/pr_body.md)"`

---

## 5. 构建/测试命令

```bash
# 安装依赖
make install              # uv sync + pnpm install

# 开发环境
make dev-up               # Docker Compose 启动（含自动数据库迁移）
make dev-down             # Docker Compose 停止
make db-sync              # 手动数据库 Schema 同步（修改 desired_schema.sql 后使用）

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

```

---

## 6. 禁止操作清单

| 禁止操作 | 原因 |
|---------|------|
| 删除 `backend/shared/` 下的模型定义 | 多个服务依赖 |
| 直接修改 `database/desired_schema.sql` 或 `database/desired_extras.sql` 而不提供迁移说明 | 生产数据安全 |
| 给存量数据表新增 `NOT NULL` 列却不带 DEFAULT | Atlas apply 失败 → db-migrate PreSync hook 失败 → Argo CD 同步卡死 |
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

---

## 9. KBD 数据管道架构原则：内容与呈现分离（Content–Presentation Separation）

> **此原则是 KBD 数据管道的最高设计约束，优先级高于任何局部实现细节。**

### 9.1 核心思想

```
┌──────────────────────────────────────────────────────────────┐
│                   KBD 数据流向                                 │
│                                                              │
│  Sangfor 知识库（源）                                          │
│      │  原始 HTML（含任意厂商样式、排版、嵌套结构）              │
│      ▼                                                       │
│  data-pipeline（抽取层）                                       │
│      │  只关心"语义内容"：文字 + 图片                           │
│      │  丢弃：颜色、字体、缩进、加粗/斜体、HTML 结构             │
│      ▼                                                       │
│  数据库（content_md / 结构化字段）                              │
│      │  规范化的 Markdown + 图片描述块（我们定义的 schema）      │
│      ▼                                                       │
│  Frontend（呈现层）                                            │
│      │  100% 由我们控制样式：字体/颜色/缩进/展开折叠/图标        │
│      ▼                                                       │
│  用户界面（高一致性呈现）                                        │
└──────────────────────────────────────────────────────────────┘
```

### 9.2 强制规则

| 规则 | 说明 |
|------|------|
| **只取语义，不取样式** | `_html_to_semantic_text` 和 `_parse_sections` 只提取文字语义内容和图片占位符（`![img:N]`），原始 HTML 的 `style`、`class`、`font`、`color` 等样式属性一律丢弃 |
| **输出格式由我们定义** | content_md 的 schema（章节结构、截图块格式、占位符格式）由本项目的 `converter.py` 定义并保持稳定，不受源 HTML 格式变化影响 |
| **截图块统一封装** | content_md 中图片以 `> **【截图说明】**` 块渲染（由后端 `rebuild_content_md` 展开 `![img:N]` 占位符得到），字段结构（BACKGROUND/TYPE/FULL_TEXT/KEY/TIPS/DESCRIPTION）固定；前端解析并渲染，不依赖原始图片 URL 或 alt 属性 |
| **前端是样式唯一来源** | 所有展示细节（展开/折叠、缩进、颜色、图标、字体）只在前端实现，data-pipeline 不输出任何影响呈现的 HTML/CSS |
| **管道规范化** | content_md 由后端 `rebuild_content_md()` 统一渲染（data-pipeline 只输出含 `![img:N]` 占位符的语义文本，不生成 content_md），保证截图块顶格、空行折叠等格式由单一权威函数控制，不受源 HTML 格式变化影响 |

### 9.3 违规示例与正确做法

```python
# ❌ 错误：将源 HTML 的样式带入 Markdown
def wrong():
    return f"**{text}**"       # 保留了原始加粗
    return f"<span style='color:red'>{text}</span>"  # 带入 HTML 样式

# ✅ 正确：只提取纯文本语义内容
def correct():
    return soup.get_text(strip=True)   # 剥离所有标签，只取文字
    return f"![img:{seq}]"  # 图片以占位符输出，视觉描述存入 images_json.desc，由后端渲染截图块
```

### 9.4 扩展新字段时的检查清单

当 data-pipeline 需要处理新的内容类型时，**必须**回答以下问题：

- [ ] 新字段速度或语义是「内容」还是「样式」？如果是样式，丢弃它
- [ ] 输出格式是否遵循现有 schema（Markdown 段落、截图块、占位符）？
- [ ] 前端是否能在不修改解析逻辑的情况下渲染新内容？
- [ ] 输出是否仅含语义文本 + `![img:N]` 占位符（content_md 由后端 `rebuild_content_md` 统一渲染）？
- [ ] 新字段有对应的单元测试验证输出格式吗？

### 9.5 设计背景

此原则来自 2026-07 KBD 27123 案例的问题复盘：

- **根因**：`markdownify` 在将嵌套列表内的截图 span 转为 blockquote 时，携带了 CommonMark 规范的列表缩进（属于样式信息），污染了 content_md，导致前端解析失败
- **修复**：data-pipeline 改为只取语义文本（新增 `_html_to_semantic_text` 作为唯一权威提取器），content_md 不再由 pipeline 生成，改由后端 `rebuild_content_md()` 统一渲染——截图块格式由单一权威函数保证，彻底根除 markdownify 缩进污染
- **结论**：data-pipeline 必须对输出做**格式规范化**（只输出语义文本 + `![img:N]` 占位符），任何来自 markdownify 或源 HTML 的"意外格式"都不应透传到 content_md

## 10. SOP 变量声明契约（Variable Schema Contract）

### 10.1 唯一事实源

`SOP.variable_schema` 是 SOP 变量声明的**唯一事实源**，契约由
[backend/shared/models/sop_variable.py](backend/shared/models/sop_variable.py) 定义，
kb-service（生产）/ agent-service（消费）/ conversation-service（消费）共用。

| 规则 | 说明 |
|------|------|
| **构造即校验** | `SopVariableSchema.parse()` 成功即代表数据可用；下游禁止再用 `var_def.get(...)` 裸取值，一律走 `SopVariableSpec` 的属性（`strategy` / `effective_tool` / `template_tokens()`） |
| **变量名必须是标识符** | `^[a-z][a-z0-9_]{0,63}$`。中文、空格、点号一律拒绝——"任务原始数据"这类描述必须放进 `description`，不能进 `name` 或 `depends_on` |
| **依赖必须是已声明的变量名** | `depends_on ⊆ 已声明 name` 且 DAG 无环。写自然语言描述会在保存时直接 422 |
| **策略必须被别名表识别** | 未知策略不得静默退化成 `user_input`（`is_known_strategy()` 显式判定），否则"自动采集"会静默变成"人工弹框" |
| **策略与字段矩阵** | `tool_call`/`skill_call`/`agent_pass` 必须有 `acquisition_tool`；`skill_call` 还必须有 `output_path`；`derived`/`json_extract`/`path_template` 必须有 `expression`；`user_input` 不得带 `depends_on`/`output_path`/`acquisition_tool` |
| **不存在"降级自由输入"** | 运行时请求未声明变量一律返回 `sop_variable_not_defined` 并标记 `config_error`，绝不下发输入卡片。写入一个 SOP 永远不消费的野键，是本类工单的第一成因 |
| **双事实源只允许单向上卷** | `tree_json.variables[].source` 由 `variable_schema` 单向重编译（见 040/051 迁移），禁止反向手改 |

### 10.2 三道闸

| 闸口 | 触发点 | 失败行为 |
|------|--------|----------|
| save | `POST /sop/{id}/variable-schema`（PATCH） | 422 `variable_schema_contract_violation` |
| publish | `POST /sop/{id}/approve` | 422（publish 态额外禁止废弃变量被活跃变量依赖、禁止必需变量无取值路径） |
| runtime | `sop_request_variable` / `sop_nav` 守卫门 | 结构化 error + `config_error=true` + ERROR 日志 |

### 10.3 新增/修改 SOP 时的检查清单

- [ ] 「依赖」列只写变量名，不写中文描述
- [ ] 「来源」列写规范策略（`tool:acli_exec` / `skill:xxx` / `env:xxx` / `derived` / `path_template` / `user_input`）
- [ ] `tool_call` 的变量必须能回答"用哪个工具、什么参数"
- [ ] 只是路径拼接的变量用 `path_template`（如 `/sf/log/{task_day}/kernel.log`），不要写成 `tool_call`
- [ ] `user_input` 的 `description` 必须写清"要填什么、从哪取值"
- [ ] 导入后确认无 422，再走发布

### 10.4 设计背景

来自 Q2026102600003（虚拟机开机失败，用户填了变量仍被要求重填）复盘：

- **根因**：SOP#2/#3/#5 的变量表「依赖」列被写成自然语言，且 22 个章节级变量因 4 列表头无法表达采集工具，解析后全部是 `tool_call + acquisition_tool=NULL`；运行时两者都静默降级为人工弹框，其中一个还写出了永远不会被消费的野键 `任务原始数据`。
- **二次复发原因**：#1149（前端已提交态）与 #1151（后端 `user_questions` 幻觉过滤）修的是"变量已写入但 UI/模型说谎"这条链路，而本次是"变量从未写对"，两次补丁天然治不到。
- **结论**：变量声明是跨服务契约，必须有值对象 + 三道闸；靠"下游修症状"永远追不上"上游自由写入"。

### 10.5 变量提交后的续跑契约

`POST /api/conversations/{id}/interactive-response` 对 `variable_input` / `variable_confirm`
**只写变量池，不触发推理**。原因是该会话上一轮的 SSE 连接已结束，后端无法主动推送。

因此：

- 路由必须返回 `should_resume: true`，调用方不得把 HTTP 200 当成"已继续"；
- 前端必须保证"续跑消息一定发出去"：`chatStore.sendMessage` 在 `isStreaming` 时返回
  `false`，调用方**必须等流结束（`resumeAfterStreamIdle`）后重发**，不能只弹一句"系统会自动继续"；
- 用户提交记录必须落库（`add_message` 需要 `trace_id`），否则事后无法对账。

### 10.6 变量卡片的展示名

变量名受标识符约束，直接展示给用户可读性差。卡片标题按
`display_name → description 首句 → variable_name` 渲染，`display_name` 由
SOP 作者显式定义（迁移 053 为存量批量补齐）。这只是展示层约定，不改变命名与提交语义。
