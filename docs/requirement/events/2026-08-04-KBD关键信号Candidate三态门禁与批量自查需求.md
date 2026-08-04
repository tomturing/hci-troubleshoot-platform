---
status: active
category: requirement
audience: all
last_updated: 2026-08-04
owner: team
---

# KBD 关键信号 Candidate 三态门禁与批量自查需求

## 背景

PR #668 将写操作从 KBD Signal 中拒绝是正确的安全边界，但 Prompt 同时要求模型看到启停、删除、迁移等词时“不生成 Signal”。这把“模型提出候选”和“工程门禁决定是否可执行”混成一步，导致 KBD30880 的只读历史任务查询 `qkv_task(keyword=启动虚拟机)` 在模型阶段直接消失，服务端与专家均无从处理。

首批 5 篇自查又暴露两类问题：

- KBD27173、KBD27222 生成当前内置 aCLI catalog 中不存在的命令；如果模型直接省略，无法判断是真实 catalog 缺口还是模型乱造。
- KBD27653、KBD27736 的候选即使工具名或命令看似合理，仍可能因参数、变量依赖、Matcher、编译或现场运行失败而不可执行。

## 目标

抽取链路只使用三个概念：

1. `Candidate`：LLM 从标题、问题描述、告警信息、有效排查步骤识别出的全部候选。LLM 可使用工具 Schema、QKV/QFK 语义和 aCLI catalog 知识减少乱造，但不得替服务端过滤候选。
2. `Signal`：Candidate 通过当前工程门禁后形成的只读、可查看、可编辑、可进入执行契约的关键信号。
3. `Rejected Candidate`：Candidate 未通过门禁后的完整审计记录，必须保留原始 Candidate、证据和具体原因并展示给专家。

## 门禁需求

门禁当前只允许三个稳定分类，按优先级执行：

| `reason_code` | 含义 | 专家要求 |
|---|---|---|
| `write_signal` | Candidate 的真实执行语义是写入、配置变更、启停、删除等处置动作 | 必须审核；不得直接进入 KBD Signal |
| `not_exists` | Candidate 足以编译，但编译出的 aCLI 命令不在当前内置 catalog | 需要关注；确认真实能力、catalog 缺口或模型映射错误 |
| `run_failed` | Schema、args、变量依赖、Matcher、编译/预运行或真实运行验证失败 | 必须重点处理；catalog 已登记不能替代运行成功 |

具体要求：

- 写动作判断依据 Candidate 的真实执行语义，不得扫描自然语言查询词。`qkv_task.keyword=启动虚拟机` 是查询失败任务，不是执行启动命令。
- 正常 Candidate 不得因同批其他候选被拒而消失。
- Rejected Candidate 不得静默删除或只保存计数。
- 历史快照没有 `reason_code` 时继续兼容，不回写或篡改不可变历史。
- 管理端同时展示 Signal 与 Rejected Candidate；后者显示分类标签、具体原因、处置建议和完整 JSON。

## 批量自查需求

- 使用最新 Schema、Prompt 和代码重跑剩余草稿 KBD。
- 每批固定 5 篇：重抽、逐条审查准确性/合理性/可执行性、修复通用问题、同批重跑验证闭环；通过后才进入下一批。
- 每次通用优化形成独立本地提交，提交信息包含 `[env:dev:sf][agent:codex]`。
- 禁止按 support_id 硬编码；不能为凑通过率静默删除 Candidate。
- 最终将所有批次提交汇总到一个 PR，并添加 `env:dev:sf`、`agent:codex` 标签，等待 CI 全绿。

## 首批案例验收

| 案例 | 预期 |
|---|---|
| KBD30880 | 连续重抽均生成只读 `qkv_task` Candidate，并通过成为 Signal；真实写候选才进入 `write_signal` |
| KBD27079 | 合理告警、日志与系统检查继续成为 Signal |
| KBD27173 | `acli hardware mc info`、`acli hardware web_info` 等未登记命令进入 `not_exists`，原 Candidate 不丢失 |
| KBD27222 | `acli system lspci`、`acli hardware list`、`acli storage list` 等未登记命令进入 `not_exists`；Matcher 问题进入 `run_failed` |
| KBD27653 | args、变量或工具映射验证失败进入 `run_failed` |
| KBD27736 | 固化脱敏值、现场不可可靠命中的 Matcher 进入 `run_failed` |

## 非目标

- 不实现自动修复循环或第四种顶层状态。
- 不宣称当前 catalog 完整。
- 不把静态校验、命令预览或 catalog 命中冒充真实现场运行成功。
- 不回滚 PR #668 的 KBD Signal 只读安全边界。
