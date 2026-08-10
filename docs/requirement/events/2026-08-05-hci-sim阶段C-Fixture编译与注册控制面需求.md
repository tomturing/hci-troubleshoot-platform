---
status: approved
category: requirement
audience: product, architect, developer, tester, operator, expert
last_updated: 2026-08-10
owner: team
---

# hci-sim 阶段 C：Fixture 编译与注册控制面需求

## 1. 背景

阶段 B 只提供安全 Runtime，仍无法从指定 KBD 自动生成可信场景。当前 KBD 27123 Fixture 是人工静态文件，已与 active KBD revision 漂移。阶段 C 建立从不可变 KBD revision、Tool Contract 和真实 Artifact 到已审核 Bundle 的控制面闭环。

## 2. 用户故事

| 用户 | 用户故事 |
|---|---|
| KBD 专家 | 我希望按 support ID 生成可审查的仿真草稿，并看到每个输出来自哪条真实证据。 |
| 测试工程师 | 我希望一份 Bundle 同时包含正例、反例、near-miss 和故障变体。 |
| Agent 开发者 | 我希望 Fixture 与当前 signals/tool contract 一致，变化后自动 stale。 |
| 安全人员 | 我希望真实 Artifact 在进入 Bundle 前完成脱敏、secret scan 和发布审批。 |

## 3. 功能需求

### C-FR-01 按 support ID 解析不可变输入

Compiler 接收 `support_id` 和 revision 选择策略，解析：

- KBD 内部 ID；
- active/指定动态资源 revision；
- KBD checksum；
- `signals_json`；
- publish validation；
- tool contract revision；
- policy revision；
- approved Artifact set。

不存在、未发布、signals 为空、revision 不可追溯时必须返回结构化 capability gap。

### C-FR-02 Fixture Compiler

Compiler 必须：

1. 复用生产 Tool/Command 编译器，不维护第二套命令规则；
2. 构建 producer/consumer 变量图；
3. 从真实 Artifact 提取并参数化 observation；
4. 生成 `positive-minimal`、`positive-realistic`、`negative`、`near-miss`、`timeout`、`permission`、`unknown` 草稿；
5. 生成 expected oracle 和 provenance；
6. 生成 Manifest v2 和内容 digest。

### C-FR-03 独立性与反自洽门禁

- Matcher 不能成为生成 witness 的唯一事实源。
- positive-realistic 必须引用经过批准的真实 Artifact。
- negative/near-miss 必须包含能区分错误实现的反例。
- 自动 mutation 至少覆盖关键字删除/替换、阈值边界、stderr 移动、节点/路径变化、producer 缺失、参数变化和执行顺序变化。
- 自动生成只能得到 draft，禁止自动发布。

### C-FR-04 发布状态机

Bundle 生命周期：

```text
draft → validated → approved → published → stale/retired
```

- validated 只代表机器门禁通过；
- approved 必须记录专家身份、时间和审查意见；
- published Bundle 不可修改，只能产生新 revision/digest；
- KBD、Signal、Tool、Policy、Artifact provenance 变化自动 stale；
- stale Bundle 不得创建新 TestRun。

### C-FR-05 Registry 与存储

- PostgreSQL 存 metadata、状态、关系、审批和审计。
- 大输出和 Bundle 使用对象存储或 OCI Artifact，按 digest 内容寻址。
- 数据库不得保存大段重复原始输出。
- Runtime 只能读取 published Bundle；不得拥有发布权限。
- Bundle 下载必须校验 digest、大小、schema 和签名。

### C-FR-06 Lint 和预览

专家必须能够查看：

- KBD/revision/tool/policy 引用；
- Signal 与 Route 映射；
- 原始 Artifact 到参数化输出的差异；
- 变量图；
- 每个 variant；
- secret/PII 扫描结果；
- Matcher/Extract dry-run；
- mutation 结果；
- capability gaps。

### C-FR-07 审计和幂等

- 同一输入指纹的重复编译必须幂等复用或明确生成相同 digest。
- 每次编译、校验、审批、发布、撤销和 stale 都必须有 trace ID 和 actor。
- 失败不得覆盖上一份 published Bundle。

## 4. 数据与安全需求

| 编号 | 要求 |
|---|---|
| C-SEC-01 | 真实凭据、token、私钥和客户标识不得进入 Bundle。 |
| C-SEC-02 | Artifact 参数化必须保留语义和 output shape，不保留客户特有身份。 |
| C-SEC-03 | 图片/OCR 只能作为 provenance/草稿辅助，不能直接成为机器 observation。 |
| C-SEC-04 | Compiler 不得触达真实 HCI；真实 Artifact 由独立受控采集流程提供。 |
| C-SEC-05 | 发布操作需要可信身份和 RBAC，不能沿用匿名占位审核。 |

## 5. 范围外

- 不在本阶段实现大规模 Scheduler。
- 不自动批准或发布 LLM 生成 Fixture。
- 不把真实 HCI 采集权限放入 Runtime。
- 不承诺所有 KBD 都可自动编译；复杂 KBD可以明确进入人工补充队列。

## 6. 验收标准

- [ ] 可以按 support ID 编译 KBD 27123 当前 active revision 草稿。
- [ ] 至少再选择 2 个不同 Tool/Matcher 结构的 KBD完成草稿编译。
- [ ] 所有 Bundle 绑定不可变 KBD/Tool/Policy/Artifact 引用和 digest。
- [ ] 自动生成不能绕过专家审批发布。
- [ ] KBD 或 Tool Contract 变化后旧 Bundle 自动 stale。
- [ ] Matcher-only 自洽 Fixture 被门禁或 mutation 检出。
- [ ] Secret/PII 注入样本无法发布。
- [ ] 发布 Bundle 可被阶段 B Runtime 加载并通过 digest 校验。
- [ ] 数据库迁移具备 desired schema、幂等迁移和回滚说明。

## 7. 关联文档

- [阶段 C 设计](../../solution/agent/events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面方案.md)
- [阶段 C 任务](../../task/agent/events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面任务.md)
- [阶段 C 验证](../../verify/events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面验证方案.md)

## 当前状态（2026-08-10）

阶段 C 已有 Manifest v2、Bundle digest、Resolver/Capability 参考契约和 synthetic bootstrap；正式 Bundle Registry/CAS、126 KBD approved Artifact 和持久化控制面仍是 capability gap，保持 pending。
