# Planner Agent 提示词模板

> 在 VS Code Copilot Plan Mode 中使用，用于需求分析和任务拆解。
> 通过 VK MCP Server 自动创建 Issue 到看板。

---

## 模板 A：需求分析与任务拆解

```
你是项目架构师，负责将需求拆解为可并行执行的子任务。

## 第一步：阅读项目规范
请先阅读 CLAUDE.md，特别关注：
- 第 5 节 任务拆解模板 — 依赖图格式和子任务描述模板
- 第 12 节 目录结构与模块边界 — 模块所有权规则

## 需求描述
[在此粘贴需求]

## 分析要求
1. 理解需求涉及哪些模块（参考 CLAUDE.md 第 12 节的目录结构）
2. 识别模块间依赖关系
3. 输出结构化的子任务列表

## 输出格式
请按以下格式输出：

### 依赖图
```
Parent: feat/[feature-name] — [功能描述]
├── ST-1: [标题] ([目录范围]) [依赖说明]
├── ST-2: [标题] ([目录范围]) [依赖说明]
└── ...
```

### 并行度分析
| 阶段 | 可并行的子任务 | 前置依赖 |
|------|---------------|---------|
| Wave 1 | ST-X, ST-Y | 无 |
| Wave 2 | ST-A, ST-B | Wave 1 完成 |
| ... | ... | ... |

### 子任务详情（每个子任务）
**ST-X: [标题]**
- 依赖: [无 / ST-Y]
- 推荐 Agent: [Claude Code / Codex / Gemini]
- 文件范围: [目录列表]
- 验收标准:
  - [ ] [条件 1]
  - [ ] [条件 2]
- Agent 提示词: [参考 .vk/prompts/coder.md 模板填写]
```

---

## 模板 B：自动创建 VK Issue

> 在 Copilot 输出拆解方案并经 tom 审核通过后使用。

```
请将上面的子任务列表通过 VK MCP Server 创建到看板。

执行步骤：
1. 调用 list_projects 获取项目列表，找到目标项目的 project_id
2. 对每个子任务，调用 create_task：
   - project_id: [步骤 1 获取的 ID]
   - title: "[ST-X] [子任务标题]"
   - description: 包含依赖关系、文件范围、验收标准和 Agent 提示词
3. 输出创建结果汇总表

注意：
- 按依赖顺序创建（无依赖的先创建）
- 在 description 中标注依赖关系：`依赖: ST-X (task_id: xxx)`
- 保留完整的 Agent 提示词
```

---

## 模板 C：批量启动 Agent 执行

> Issue 创建完成后，可选择批量启动 Agent。

```
请启动以下子任务的 Workspace。

执行步骤：
1. 调用 list_projects 获取 project_id
2. 调用 list_repos 获取 repo_id
3. 对每个需要启动的子任务，调用 start_workspace_session：
   - task_id: [子任务的 task_id]
   - executor: [claude-code / codex / gemini]
   - repos: [{ repo_id: "xxx", base_branch: "main" }]

请按 Wave 顺序启动：
- Wave 1（无依赖）: 立即全部启动
- Wave 2（依赖 Wave 1）: 等 Wave 1 完成后启动

输出已启动的 Workspace 汇总。
```

---

## 完整工作流示例

```
1. tom: "我需要实现知识库 RAG 服务，支持 Markdown 文档摄入和向量检索"
2. Copilot（使用模板 A）: 输出 6 个子任务的依赖图和详情
3. tom: "确认，请创建到 VK"
4. Copilot（使用模板 B）: 通过 MCP 创建 6 个 Issue
5. tom: "启动 Wave 1 的任务"
6. Copilot（使用模板 C）: 通过 MCP 启动 3 个 Workspace
7. tom: 在 VK 看板监控进度，Review diff，逐步合并
```
