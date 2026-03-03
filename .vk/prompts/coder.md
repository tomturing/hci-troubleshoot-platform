# Coder Agent 提示词模板

> 在 VK Workspace 创建时，将以下内容作为 Agent 的初始提示词。
> 替换 `[...]` 占位符为具体任务内容。

---

## 模板

```
你是 [项目名] 的开发者。

## 第一步：阅读项目规范
请先阅读以下文件了解项目规范和工作流约定：
- AGENTS.md（或 CLAUDE.md）— 项目规范 + 多 Agent 工作流
- 特别关注"第 6 节 并行编码规范"和"第 13 节 编码规范"

## 你的任务
[具体任务描述，如：实现 KB Service 的文档摄入 API]

## 文件范围（只允许修改以下目录/文件）
- [目录1，如：backend/kb-service/]
- [目录2，如：tests/unit/test_kb_service.py]

## 验收标准
- [ ] [具体可验证的完成条件 1]
- [ ] [具体可验证的完成条件 2]
- [ ] 代码通过 lint（运行 `uv run ruff check [修改目录]`）
- [ ] 单元测试通过（运行 `uv run pytest [测试目录] -q`）

## 完成后
请运行以下命令确认质量门禁通过：
```bash
bash scripts/agent-quality-gate.sh
```

如果有测试失败或 lint 错误，请修复后再次运行，直到全部通过。
```

---

## 使用示例

```
你是 HCI 智能排障平台的开发者。

## 第一步：阅读项目规范
请先阅读以下文件了解项目规范和工作流约定：
- AGENTS.md — 项目规范 + 多 Agent 工作流
- 特别关注"第 6 节 并行编码规范"和"第 13 节 编码规范"

## 你的任务
为 scheduler-service 添加 Pod 池 gauge 指标端点，暴露当前各助手类型的
Pod 数量（total / ready / busy）。

## 文件范围
- backend/scheduler-service/app/
- backend/scheduler-service/tests/

## 验收标准
- [ ] 新增 GET /pool-metrics 端点，返回 JSON 格式的池状态
- [ ] 每种助手类型独立统计 total/ready/busy
- [ ] 添加对应的单元测试（mock Redis）
- [ ] 代码通过 lint
- [ ] 单元测试通过

## 完成后
bash scripts/agent-quality-gate.sh
```
