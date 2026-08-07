---
status: in_progress
category: verify
audience: developer, tester, operator, expert, security
last_updated: 2026-08-06
owner: team
---

# hci-sim 阶段 C2：获批 Artifact 与不可变 Bundle Registry 验证报告

## 已执行的代码级验证

| 检查 | 结果 | 证据 |
|---|---|---|
| Go 格式化与单元测试 | 通过 | Go 1.20 容器执行 `go test ./...`，controlplane、fixture、lease、server 全部通过。 |
| Artifact 状态机反例 | 通过 | 未扫描、license scan 失败、登记者自审、单人双角色、撤销后再绑定均被拒绝。 |
| Compiler Gate | 通过 | 未配置 Artifact Registry 或 ID/digest 与获批记录不一致均产生 capability gap。 |
| 对象完整性 | 通过 | prepare 拒绝错误 digest；verify 后 commit 写入 `bundles/{payload-digest}`；提交后临时引用不可再验证。 |
| Atlas schema 一致性 | 通过 | 在 PostgreSQL 15 + pgvector 临时库应用 C1 schema 与 C2 增量 SQL 后，`atlas schema diff` 输出 `Schemas are synced`。 |

## 未执行且不得替代的验证

- 未访问真实 HCI、未读取/上传真实 Artifact、未写入 dev KBD/TestRun/Bundle metadata。
- 未验证 S3/OCI、KMS 签名、DLP/PII/许可证扫描器、PostgreSQL 多副本 CAS/outbox worker。
- 未为任何 KBD 生成 draft Bundle；126 KBD 的 C1 结论仍是 2 条可绑定前置、4 条 Tool stale、120 条未发布。
- 不能据此宣称 KBD 27123 或任一 KBD 已通过 positive-realistic、Bridge E2E、real/sim differential、20 次稳定性或 100+ 并发。

## 已识别的环境性限制

Atlas `migrate diff/hash` 对仓库已有两个版本同为 `20260727000000` 的文件 fail fast；这是 C2 前已存在的
迁移历史冲突。本次未修改历史文件，也没有手工编辑 `atlas.sum`。实际部署和 CI 的 schema 路径是
`atlas schema apply --to database/desired_schema.sql`，该路径已在临时 PostgreSQL 15 + pgvector 中验证。
重复版本的迁移历史修复应独立评审，以免破坏已部署环境的迁移账本。
