---
status: in_progress
category: task
audience: developer, tester, operator, expert, security
last_updated: 2026-08-06
owner: team
---

# hci-sim 阶段 C2：获批 Artifact 与不可变 Bundle Registry 任务

## 本 PR 完成项

- [x] 定义 Artifact staged/scanned/approved/revoked 状态和不可变 metadata/provenance。
- [x] 定义 secret、PII、license、schema 四项 fail-closed scan 报告。
- [x] 实现 Expert/Security 双角色、不同 actor、登记者不可自审的内存参考 Registry。
- [x] 移除调用者可伪造的 `Artifact.Approved`，编译时强制调用 Artifact Gate。
- [x] 实现 Bundle payload 的 prepare/verify/commit/abort 参考对象存储及内容寻址不可变键。
- [x] 扩展 desired schema、Atlas 增量 SQL、DB CI 核心表检查及 stale outbox/CAS version。
- [x] 完成反例单测和 C2 文档。

## 后续阻断项

- [ ] 由 Security/数据责任人提供 Artifact 采集范围、保留期、脱敏规则、许可证规则与真实审批身份源。
- [ ] 部署受控 OCI/S3/WORM 存储、KMS 签名和生产扫描器；禁止用 Memory 实现替代。
- [ ] 实现 PostgreSQL Repository：`SELECT ... FOR UPDATE`/version CAS、审计、outbox worker 和超时恢复。
- [ ] 修复历史 Atlas migration 目录的重复版本问题，再恢复 versioned migration hash/lint 工作流。
- [ ] 为两个 `ready_for_artifact_binding` KBD 绑定实际获批 Artifact 后，编译 `draft`，不得直接发布。

## 验收证据

见 [C2 验证报告](../../../verify/hci-sim/events/hci-sim阶段C2获批Artifact与不可变BundleRegistry验证报告.md)。
