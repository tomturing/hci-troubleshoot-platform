---
status: in_progress
category: task
audience: developer, tester, operator, expert, security
last_updated: 2026-08-05
owner: team
---

# hci-sim 阶段 C：Fixture 编译与注册控制面任务

关联[阶段 C 需求](../../../requirement/events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面需求.md)、[阶段 C 方案](../../../solution/agent/events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面方案.md)和[阶段 C 验证](../../../verify/events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面验证方案.md)。

> 当前事实：控制面代码级内核、Registry 生命周期、Atlas migration 与 desired schema 已实施；C1 已接入只读 active KBD snapshot Resolver，并对 126 条 KBD 完成 capability 基线（2 条待 Artifact 绑定、4 条 Tool stale、120 条未发布）。Artifact/对象存储/RBAC 和 Bundle 试点仍未完成，不能写为阶段 C 环境验收完成。见[C1 验证报告](../../../verify/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证报告.md)。

## 前置 Go/No-Go

- [ ] 阶段 B Manifest v2/Runtime 契约冻结并验证通过。
- [ ] KBD active/精确 revision、signals、Tool/Policy 和 Artifact 的权威读取接口明确。
- [ ] 真实 Artifact 采集、审批、脱敏和保留责任人明确。
- [ ] desired schema、对象/OCI 存储、签名和 RBAC 经过架构/DBA/Security 评审。

## WBS

| ID | 任务 | 主要影响 | 产物/证据 | 依赖 |
|---|---|---|---|---|
| T-SIM-C-01 | 定义 desired schema、索引、约束、RLS/RBAC 和 migration | PostgreSQL | migration/rollback review | B Go |
| T-SIM-C-02 | 实现 support ID/revision/KBD/Tool/Policy/Artifact Resolver | control plane | C1 已完成只读 KBD/Tool/Policy snapshot 与 126 条 gap report；Artifact Resolver 待 C2 | 01 |
| T-SIM-C-03 | 定义输入指纹、编译作业幂等、outbox/audit | compiler metadata | 并发幂等测试 | 01–02 |
| T-SIM-C-04 | 复用生产 Tool Compiler，构建 Signal/producer/consumer 图 | Agent/Compiler | graph/gap tests | 02 |
| T-SIM-C-05 | 实现 Artifact observation 提取、参数化和 provenance | compiler/artifact | golden diff | 04 |
| T-SIM-C-06 | 实现 positive/negative/near-miss/fault variant 和 Oracle | compiler | variant matrix | 05 |
| T-SIM-C-07 | 实现 schema/semantic lint、matcher dry-run、mutation | validator | mutation report | 04–06 |
| T-SIM-C-08 | 集成 secret/PII scan 和发布阻断 | security pipeline | 注入测试 | 05–07 |
| T-SIM-C-09 | 实现 Registry metadata、内容寻址上传、签名和读取完整性 | Registry/storage | tamper tests | 01、07–08 |
| T-SIM-C-10 | 实现 draft→validated→approved→published→stale/retired 状态机 | Registry | transition/RBAC tests | 09 |
| T-SIM-C-11 | 实现依赖反向索引、事件 stale 和定时 reconciliation | control plane | 漂移测试 | 02、10 |
| T-SIM-C-12 | 实现 compile/preview/approve/publish/stale API、CLI/管理入口 | API/UI | E2E review flow | 03–11 |
| T-SIM-C-13 | 用 27123 + 至少 2 个异构 KBD 试点并形成报告 | 全链路 | 阶段 C 证据 | 12 |

## 实现检查

- [ ] `active` 在编译开始时锁定为不可变 revision/checksum，运行中不漂移。
- [x] 同一输入/Compiler revision 并发请求不会生成冲突版本（内存参考实现；生产需 PostgreSQL 唯一键/CAS）。
- [ ] production Tool/Command contract 是唯一命令规则来源。
- [ ] 变量图拒绝循环、缺失、类型/基数冲突和跨节点越权。
- [ ] positive-realistic 必须有批准真实 Artifact provenance。
- [ ] OCR/图片不能直接成为机器 observation。
- [x] 自动生成最大状态为 validated，不能自动 approved/published。
- [x] published 字节不可改，只能发布新 digest。
- [x] Runtime reader 只可读取 published，无法发布或访问草稿。
- [x] stale 阻止新 Run，但历史证据仍可查询。

## 数据库与存储任务

- [ ] 创建 scenario、bundle、dependency、provenance、approval、audit desired schema。
- [ ] 唯一约束覆盖 input fingerprint、bundle digest 和 lifecycle version。
- [ ] metadata 与大对象分离，数据库不重复保存原始大输出。
- [ ] 上传采用 prepare/commit，失败对象可回收，发布对象 versioned/immutable。
- [ ] 下载验证服务端 URI、大小、schema、signature、digest 和状态。
- [ ] migration 支持空库、已有库、重复执行和向前修复演练。

## 测试任务

- [ ] Resolver：不存在、未发布、空 signals、指定/active revision 和竞态变化。
- [ ] Graph/parameterization：多 producer、缺失变量、客户身份替换、shape 保真。
- [ ] Variants/Oracle：真假/near-miss/unknown/error 不被布尔压平。
- [ ] Mutation：matcher-only 自洽样本被识别。
- [ ] Scan：token、私钥、客户 IP/主机/标识注入无法发布。
- [ ] Lifecycle/RBAC：非法跳转、越权、自审自批、并发 publish 被拒绝。
- [ ] Integrity：对象篡改、digest/size/schema/signature 不符被 Runtime 拒绝。
- [ ] Stale：KBD/Tool/Policy/Artifact 变化传播，漏事件被 reconciliation 修复。
- [ ] Runtime compatibility：已发布 Bundle 可由阶段 B 加载和命中。

## 回滚与失败处理

- Compiler/验证失败不得修改当前 published 指针。
- migration 采用 expand/contract 和向前修复；生产数据不执行破坏性 down。
- 新 Registry 读取异常时停止新发布/新 Run，保留历史对象；不得回退读取 draft 或忽略 digest。
- secret 泄漏触发对象隔离、key rotation、审计和所有依赖 Bundle stale。

## 阻断条件

- 阶段 B 未 Go 或 Manifest v2 仍变动；
- 权威 KBD/Tool/Artifact revision 不可追溯；
- 无法建立真实 Artifact 脱敏与审批责任；
- Matcher 是 positive-realistic 的唯一来源；
- RBAC 允许 Compiler 自动发布或 Runtime 读取草稿；
- migration/存储无法保证已发布 Bundle 不可变。

## Definition of Done

- [ ] 27123 当前 active revision 和至少 2 个异构 KBD 产生可审查草稿。
- [ ] provenance、mutation、secret scan、审批、发布和 stale 全链路通过。
- [ ] published Bundle 被阶段 B Runtime 完整性校验并执行。
- [ ] capability gap 结构化、可查询、有 owner，不伪造 Fixture。
- [ ] desired schema、migration、API、运行手册和阶段 C 验证报告获签署。
- [ ] 阶段 D 获得明确 Go。
