---
status: implemented
category: task
audience: developer, tester, operator, security
last_updated: 2026-08-06
owner: team
---

# hci-sim 阶段 C1：权威 KBD 解析与全量能力验证任务

## 完成项

- [x] 以 `dynamic_resource_active` 的精确 revision 作为 KBD 编译输入唯一读取点。
- [x] 校验 published 状态、active/snapshot checksum、support ID、v2 Signal、publish validation 和
  Tool Contract revision。
- [x] 定义受版本控制的 hci-sim Policy Contract revision，不复制 Tool/命令规则。
- [x] 实现受内部身份保护的单条/全量只读 capability API。
- [x] 对 dev 中全部 126 条 KBD 执行只读基线验证，并记录计数与 gap 分类。
- [x] 单元测试覆盖 ready、未发布、缺 active snapshot、Tool Contract stale、snapshot identity 篡改和
  批量聚合边界。

## 后续 C2 任务

- [ ] 建立获批 Artifact metadata/provenance 与脱敏、secret/PII/license scan。
- [ ] 建立对象存储/OCI 的临时上传、digest/size/schema/signature 校验和不可变提交。
- [ ] 实现 PostgreSQL Bundle Registry CAS、审批审计、stale outbox/reconciliation 与 RBAC。
- [ ] 为 C1 的 `ready_for_artifact_binding` KBD 生成 draft Bundle，先覆盖 27123 及结构异构样本。
- [ ] 形成 Artifact 缺失、Tool stale、KBD 未发布的 owner/SLA 队列，禁止用 waiver 掩盖。

关联：[C1 方案](../../../solution/agent/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证方案.md)、
[C1 验证](../../../verify/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证报告.md)。
