# 多 Agent 并行开发工作流规范

> **本文件是通用的多 Agent 并行开发工作流规范。**
> 由 [multi-agent-workflow](https://github.com/tomturing/multi-agent-workflow) 模板仓库提供。
> 所有 Agent（Claude Code / Codex CLI / Gemini CLI）在开始编码前都应阅读本文件。
> 项目专属规范见项目根目录的 `CLAUDE.md`。

---

## 1. 工作流总览

```
Human (tom)                    VS Code Copilot (Plan Mode)
   │                                │
   │  描述需求                       │  分析代码库 → 输出依赖图 + 子任务
   │                                │
   └──── 审核拆解方案 ──────────────→ │  VK MCP create_task × N
                                    │  VK MCP start_workspace_session × N
                                    ▼
                          ┌─── Vibe Kanban ───┐
                          │  Kanban Board      │
                          │  Workspace/Worktree│
                          │  Agent Session     │
                          │  Quality Gate      │
                          └──┬──┬──┬──┬───────┘
                             │  │  │  │
                 ┌───────────┘  │  │  └───────────┐
                 ▼              ▼  ▼              ▼
             Agent-1        Agent-2  Agent-3    Agent-4
            (Claude)       (Codex)  (Gemini)  (Any/Review)
            vk/task-1      vk/task-2 vk/task-3 vk/task-4
                 │              │  │              │
                 └──────┬───────┘  └──────┬───────┘
                        ▼                 ▼
                   Quality Gate      Quality Gate
                   (lint+test)       (lint+test)
                        │                 │
                        ▼                 ▼
                   AI Code Review    AI Code Review
                        │                 │
                        └────────┬────────┘
                                 ▼
                     Merge → 集成验证 → 发布
```

### 核心原则

1. **Vibe Kanban 是唯一的任务编排中枢** — 所有任务、Workspace、Agent Session 都通过 VK 管理
2. **VK MCP Server 实现自动化** — Copilot Plan Mode 通过 MCP 工具自动创建 Issue 和启动 Agent
3. **Git Worktree 隔离** — 每个 Workspace 自动创建独立 worktree，Agent 之间不互相干扰
4. **所有 Agent 均可编码和审查** — Claude Code / Codex CLI / Gemini CLI 角色灵活分配
5. **质量门禁前置** — 每个 Workspace 完成时自动运行 lint + test，通过后才能合并

---

## 2. 角色定义

| 角色 | 承担者 | 职责 |
|------|--------|------|
| **需求设计者** | Human (tom) | 提出需求、审核拆解方案、最终合并决策 |
| **计划 Agent** | VS Code Copilot (Plan Mode) | 分析代码库、输出依赖图、通过 VK MCP 创建 Issue |
| **编排中枢** | Vibe Kanban | 管理看板、Workspace、Worktree、Agent Session |
| **编码 Agent** | Claude Code / Codex CLI / Gemini CLI | 在独立 Worktree 中编码实现 |
| **审查 Agent** | Claude Code / Codex CLI / Gemini CLI | 基于 diff 做 Code Review |

### Agent 交叉审查规则（强制）

**核心原则：编码 Agent 和审查 Agent 必须是不同类型。**

| 编码 Agent | 审查 Agent | 说明 |
|-----------|-----------|------|
| Claude Code | Codex CLI | Claude 写 → Codex 审 |
| Codex CLI | Claude Code | Codex 写 → Claude 审 |
| Gemini CLI | Claude Code | Gemini 写 → Claude 审（优先）或 Codex 审 |
| Claude Code | Gemini CLI | 当 Codex 不可用时的备选 |

**执行方式**:
1. 编码 Agent 完成后，在同一 VK Workspace 创建 **新 Session**
2. 选择上表对应的 Reviewer Agent
3. 使用 `.vk/prompts/reviewer.md` 中的提示词
4. Reviewer **不修改代码**，只输出结构化审查意见
5. 如有 `CHANGES_REQUESTED`，切回 Coder Session 修复后再审

---

## 3. 环境要求

### 3.1 必需工具

| 工具 | 用途 | 安装 |
|------|------|------|
| **Vibe Kanban** | 任务编排 + Workspace 管理 | `npx vibe-kanban` |
| **cc-switch** | AI Agent API 密钥统一管理 | 已配置 |
| **Claude Code CLI** | AI 编码 Agent | `claude` 命令 |
| **Codex CLI** | AI 编码 Agent | `codex` 命令 |
| **Gemini CLI** | AI 编码 Agent | `gemini` 命令 |
| **Git** | 版本控制 + Worktree | 系统自带 |

### 3.2 VK MCP Server 配置

在 VS Code Copilot 的 MCP 配置中添加：

```json
{
  "mcpServers": {
    "vibe_kanban": {
      "command": "npx",
      "args": ["-y", "vibe-kanban@latest", "--mcp"]
    }
  }
}
```

### 3.3 VK Agent Profiles 配置

在 VK Settings → Agents 中配置（`profiles.json`）：

```json
{
  "executors": {
    "CLAUDE_CODE": {
      "DEFAULT": { "CLAUDE_CODE": { "dangerously_skip_permissions": true } },
      "PLAN":    { "CLAUDE_CODE": { "plan": true } },
      "ROUTER":  { "CLAUDE_CODE": { "claude_code_router": true, "dangerously_skip_permissions": true } }
    },
    "GEMINI": {
      "DEFAULT": { "GEMINI": { "model": "default", "yolo": true } }
    },
    "CODEX": {
      "DEFAULT": { "CODEX": { "sandbox": "danger-full-access" } }
    }
  }
}
```

### 3.4 VK 仓库配置

在 VK Settings → Repositories 中：

- **Setup Script**: 见项目 `CLAUDE.md` 中的构建命令
- **Cleanup Script**: `bash scripts/agent-quality-gate.sh`
- **Dev Server**: 见项目 `CLAUDE.md` 中的构建命令

---

## 4. VK MCP 自动化任务拆解流程

### 完整流程（5 步）

```
Step A: tom 在 VS Code 中用 Copilot Plan Mode 描述需求
        ↓
Step B: Copilot 分析代码库，输出结构化拆解方案（依赖图 + Agent 提示词）
        ↓
Step C: tom 审核拆解方案（修改 / 确认）
        ↓
Step D: tom 指示 Copilot "将此计划创建为 VK 任务"
        → Copilot 调用 VK MCP list_projects 获取 project_id
        → Copilot 调用 VK MCP create_task × N 逐个创建 Issue
        ↓
Step E: tom 在 VK 看板确认 Issue，手动或通过 MCP 调用
        start_workspace_session 启动 Agent 并行执行
        ↓
Step F: Copilot 将 issue_id 写入各 worktree 的 .vk/issue_id
        （用于 cleanup 成功后自动流转 Issue 状态）
```

### Copilot Plan Mode 提示词模板

在 VS Code Copilot 中使用以下提示词启动计划：

```
你是项目架构师。请分析以下需求，并参照 .vk/workflow.md 中的任务拆解模板，
输出结构化的子任务列表。每个子任务包含：
1. 标题（简洁描述）
2. 描述（具体实现要求 + 验收标准）
3. 依赖关系（依赖哪些其他子任务的 ID）
4. 文件范围（只允许修改的目录/文件）
5. 推荐 Agent（Claude Code / Codex / Gemini）

需求描述：
[在此粘贴需求]
```

### 自动创建 Issue 提示词

审核通过后，指示 Copilot：

```
请将上面的子任务列表通过 VK MCP Server 创建到看板。
步骤：
1. 调用 list_projects 获取 project_id
2. 对每个子任务调用 create_task(project_id, title, description)
3. 输出创建结果汇总
```

---

## 5. 任务拆解模板

### 5.1 依赖图格式

```
Parent: feat/[feature-name] — [功能描述]
├── ST-1: [子任务标题] ([目录范围]) [无依赖，可先行]
├── ST-2: [子任务标题] ([目录范围]) [依赖 ST-1]
├── ST-3: [子任务标题] ([目录范围]) [依赖 ST-2]
├── ST-4: [子任务标题] ([目录范围]) [可与 ST-2 并行]
└── ST-5: [子任务标题] ([目录范围]) [可与 ST-2 并行]
```

### 5.2 子任务描述模板

```markdown
## [ST-X] [标题]

**依赖**: ST-Y, ST-Z（或"无"）
**Agent**: Claude Code / Codex / Gemini
**文件范围**: backend/xxx-service/, tests/unit/xxx/
**验收标准**:
- [ ] [具体可验证的完成条件 1]
- [ ] [具体可验证的完成条件 2]
- [ ] lint 通过
- [ ] 单元测试通过且覆盖新增逻辑
**Agent 提示词**:
你是 [项目名] 的开发者。请阅读 AGENTS.md 了解项目规范。
你的任务是：[具体描述]。
只修改以下目录：[目录范围]。
完成后运行质量门禁确保测试通过。
```

### 5.3 并行度判断规则

| 条件 | 决策 |
|------|------|
| 两个子任务修改不同目录且无数据依赖 | ✅ 可并行 |
| 子任务 B 依赖子任务 A 的接口定义 | ⚠️ A 先完成接口骨架后可并行 |
| 两个子任务修改同一文件 | ❌ 必须串行或合并为一个任务 |
| 子任务修改公共/共享模块 | ⚠️ 应最先完成，其他子任务依赖它 |

---

## 6. 并行编码规范

### 6.1 Workspace 隔离原则

- 每个 Subtask 对应一个 VK Workspace
- 每个 Workspace 自动创建独立 Git Worktree（`vk/task-xxx` 分支）
- **Agent 只允许修改子任务声明的文件范围**，违反此规则的变更在 Review 中拒绝

### 6.2 Agent 行为约束

所有 Agent 在开始编码前**必须**：

1. 阅读 `CLAUDE.md` 或 `AGENTS.md` 了解项目规范
2. 阅读 `.vk/workflow.md` 了解通用工作流规范
3. 阅读 `.vk/prompts/coder.md` 了解编码约定
4. 确认自己的文件修改范围
5. 完成编码后运行项目的质量门禁命令

所有 Agent **禁止**：

- 修改文件范围外的代码
- 删除或重命名已有的公共接口
- 修改数据库 schema 文件（除非任务明确要求且有迁移脚本）
- 安装新的系统级依赖（只能用项目包管理器添加项目依赖）
- 提交含敏感信息（密钥/密码/token）的代码

---

## 7. Git 分支策略

### 7.1 VK Worktree 默认策略

```
main (或 develop)
├── vk/task-001-xxx     ← Workspace 1 (Agent: Claude)
├── vk/task-002-xxx     ← Workspace 2 (Agent: Codex)
├── vk/task-003-xxx     ← Workspace 3 (Agent: Gemini)
└── vk/task-004-xxx     ← Workspace 4 (Agent: Claude)
```

- VK 自动创建 `vk/` 前缀分支（可在 VK Settings → Git → Branch Prefix 修改）
- 每个 Workspace 基于 Parent Issue 的目标分支（通常是 `main`）
- Subtask 自动继承父任务的 base branch

### 7.2 合并顺序

```
1. 公共/共享模块 → 2. 后端服务 → 3. 前端应用 → 4. 文档/测试
```

- 按依赖链顺序合并，避免破坏其他 Workspace 的 rebase
- 每次合并后通知其他活跃 Workspace 执行 rebase：

  ```bash
  # 在 VK Workspace 的 terminal 中
  git fetch origin && git rebase origin/main
  ```

### 7.3 合并方式

| 方式 | 适用场景 |
|------|---------|
| **Create PR**（推荐） | 需要人类最终审核、团队协作 |
| **Local Merge** | 个人开发、快速迭代 |

---

## 8. 质量门禁

### 8.1 自动门禁（VK Cleanup Script 触发）

每个 Workspace 完成时，VK 自动运行 `scripts/agent-quality-gate.sh`：

| 检查项 | 说明 | 通过标准 |
|--------|------|---------|
| 静态检查 | 语言对应的 linter | 无 error |
| 格式检查 | 语言对应的 formatter | 无 diff |
| 单元测试 | 项目测试框架 | 全部通过 |

> 具体的 lint/test 工具在 `scripts/agent-quality-gate.sh` 中配置，可按项目定制。
### 8.1.1 自动状态流转（VK Hook）

质量门禁通过后，`agent-quality-gate.sh` 自动调用 `scripts/vk-hooks.sh` 将 Issue 状态流转到 "In review"：

```
质量门禁 PASSED → vk_on_cleanup_success() → PATCH /api/remote/issues/{id} → Issue: "In review"
质量门禁 FAILED → vk_on_cleanup_failure() → Issue 保持 "In progress"
```

**前置条件**：Workspace 中必须存在 `.vk/issue_id` 文件，内容为关联的 VK Issue ID。

编排者（Copilot Plan Mode）在调用 `start_workspace_session` 后应立即写入此文件：
```bash
# 在 worktree 中写入关联的 issue_id
echo "19e8c043-4f30-40d8-b87b-baf009919c27" > .vk/issue_id
git add .vk/issue_id && git commit -m "chore: 写入 VK issue_id 用于自动状态流转"
```

> 如果 `.vk/issue_id` 不存在，Hook 静默跳过（不影响 cleanup 退出码），回退为手动流转。
### 8.2 AI Code Review 流程

```
Agent 编码完成
    ↓
在同一 Workspace 创建新 Session → 选择另一种 Agent
    ↓
Reviewer 查看 Changes Panel 中的 diff
    ↓
按以下清单审查：
  □ 是否只修改了声明的文件范围？
  □ 是否遵循项目编码规范？
  □ 是否有足够的错误处理？
  □ 是否添加了必要的测试？
  □ 是否有安全隐患（硬编码密钥、SQL 注入等）？
  □ 是否符合可观测性要求？
    ↓
输出结构化 Review 意见
    ↓
如有问题 → 切回 Coder Session 修复 → 再次 Review
如通过 → 标记 Ready to Merge
```

### 8.3 Worktree 冲突预检

合并前运行 `scripts/check-worktree-conflicts.sh`，检测不同 Workspace 间修改了相同文件的情况。

---

## 9. 合并与发布流程

### 9.1 标准合并流程

```
1. 质量门禁全部通过
2. AI Code Review 通过
3. Worktree 冲突预检通过
4. tom 在 VK 中确认合并（Create PR 或 Local Merge）
5. 按依赖顺序逐个合并
6. 每次合并后运行 scripts/post-merge-verify.sh
7. 全部合并完成后运行完整集成测试
```

### 9.2 合并后集成验证

`scripts/post-merge-verify.sh` 执行：

- 全量 lint + 单元测试
- E2E 集成测试（如配置）
- 输出集成验证报告
