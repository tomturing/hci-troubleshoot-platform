---
status: proposed
category: requirement
audience: product, architect, developer, tester, operator, expert, security
last_updated: 2026-08-06
owner: team
---

# hci-sim 阶段 C2：获批 Artifact 与不可变 Bundle Registry 需求

## 目标

C1 已能对全部 126 个 KBD 只读解析出 KBD/Signal/Tool/Policy 的能力缺口，但不能把
`ready_for_artifact_binding` 误写为“已构建仿真环境”。C2 建立 Artifact 的受控入口和
Bundle 的对象存储边界，使下一阶段只能使用经扫描、双角色批准且可追溯的 Artifact。

本阶段不采集真实 HCI、不创建客户 Artifact、不向对象存储写入真实客户数据；没有受权的
Artifact 时，系统必须返回 capability gap。

## 功能需求

| 编号 | 需求 |
|---|---|
| C2-FR-01 | Artifact metadata 必须具有不可变 ID、payload digest、大小、media type、schema version、来源摘要、脱敏摘要、采集策略、采集者、采集时间和 trace ID。禁止保存原始字节、客户 URL、命令输出或身份正文。 |
| C2-FR-02 | Artifact 必须依次经历 `staged → scanned → approved`；secret、PII、license、schema 任一扫描不通过即拒绝。`revoked` 的 Artifact 不得再绑定。 |
| C2-FR-03 | 只有 Expert 和 Security 两个不同身份完成批准，Artifact 才可绑定；登记者不得自审，同一身份不得满足两个强制角色。 |
| C2-FR-04 | Compiler 不得相信调用请求中的 `approved=true` 一类声明，必须向 Artifact Registry 验证 ID、digest 和批准状态。 |
| C2-FR-05 | Bundle 的 Manifest 语义 digest 与对象 payload digest 必须分离；prepare/verify/commit 都校验实际 payload digest、大小和不可变目标键。 |
| C2-FR-06 | 依赖变化必须持久化为 stale outbox；失败可被 reconciliation 重放，旧 published Bundle 在 stale 后不得创建新 Run。 |
| C2-FR-07 | DB 写入必须为 CAS/乐观锁预留 `version`；生产实现使用 PostgreSQL 事务，Runtime 只读 published Bundle。 |

## 验收边界

- 未登记、未扫描、扫描失败、单人双角色、登记者自审、digest 不匹配和已撤销 Artifact 均不能通过 Compiler Gate。
- 同一 Bundle payload 只能以 payload digest 对应的不可变对象键发布；临时对象在重复编译或失败时可回收。
- C2 的内存 Registry/ObjectStore 只用于确定性测试，**不是**生产 PostgreSQL CAS 或 OCI/S3 实现。
- C2 不改变 C1 对 126 个 KBD 的结论：2 条仅为 `ready_for_artifact_binding`，尚无获批 Artifact，故没有 positive-realistic Bundle 或真实 E2E 通过项。

## 关联

- [C1 权威 KBD 解析方案](../../solution/agent/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证方案.md)
- [阶段 C 总体需求](2026-08-05-hci-sim阶段C-Fixture编译与注册控制面需求.md)
- [C2 方案](../../solution/agent/events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry方案.md)
- [C2 任务](../../task/agent/events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry任务.md)
- [C2 验证](../../verify/events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry验证报告.md)
