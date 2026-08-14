---
status: in_progress
category: solution
audience: architect, developer, tester, operator, expert, security
last_updated: 2026-08-06
owner: team
---

# hci-sim 阶段 C2：获批 Artifact 与不可变 Bundle Registry 方案

## 安全边界

```text
授权只读采集流程
        ↓ 受控对象存储（原始 Artifact，不进入 DB/Bundle）
Artifact metadata → Scan → Expert + Security 双人批准 → Artifact Gate
                                                           ↓
C1 immutable KBD input ─────────────────────────────→ Compiler → prepare/verify
                                                                  ↓
PostgreSQL CAS + stale outbox ← immutable Bundle metadata ← commit object payload
                                                                  ↓ 只读 published
                                                            hci-sim Runtime
```

Artifact 与 Bundle 的内容摘要使用不同语义：

| 摘要 | 覆盖内容 | 目的 |
|---|---|---|
| `bundle.digest` | 清空自引用 digest/signature 后的规范 Manifest | Runtime 的语义一致性和 Fixture 版本身份。 |
| `object_digest` | 上传到对象存储的完整 payload 字节 | 传输/存储完整性、对象地址和防篡改。 |

把两者合并会造成自引用：payload 内含 Manifest digest，Manifest digest 又试图覆盖整个 payload。因此
Runtime 必须同时验证 payload digest、大小和 Manifest 语义 digest/schema；任一不符拒绝运行。

## Artifact 生命周期

```text
staged --(全部扫描通过)--> scanned --(expert + security)--> approved
   |                                                          |
   +-------------------- 拒绝/失败 --------------------------+
                                                              ↓
                                                        revoked（永久不可绑定）
```

- Metadata 中只存 `source_ref_digest` 与 `redaction_digest`；对象地址由服务端生成，不作为用户输入。
- Approval 中角色和 actor 双重唯一；登记者及已经用另一角色审批的 actor 均被拒绝。
- Compiler 的 `ArtifactGate.VerifyApproved(id,digest)` 是唯一绑定入口。删除 `CompileInput.Artifact.Approved`
  这类可由调用者伪造的字段。
- Artifact 撤销产生 dependency stale outbox；生产消费者需以 `FOR UPDATE SKIP LOCKED` 领取事件，
  对 Bundle 状态作 version CAS，再写入审计。进程崩溃时由 pending/processing 超时 reconciliation 恢复。

## 存储和事务

生产发布顺序为：

1. Compiler 构建 Manifest，计算语义 `bundle.digest` 和原始 `object_digest`。
2. 上传临时对象，核验 payload digest、大小、schema、签名和 scan 结论。
3. 以 `digest` 唯一键和 `version` CAS 写入/更新 DB 的 draft/validated/approved metadata。
4. 将临时对象提交到只追加的 `bundles/{object_digest}`，重复提交只能接受字节相同对象。
5. 在同一 DB 事务将 Bundle 迁移为 `published`、写 audit，并在失败时保留可回收临时引用。

本 PR 实现了同等语义的 `MemoryArtifactRegistry` 与 `MemoryBundleObjectStore`，用于单测证明拒绝路径和
prepare/verify/commit 不变量；它们不含真实 Artifact bytes 字段和网络/数据库实现。实际 PostgreSQL
Repository、S3/OCI adapter、签名密钥、扫描器和运行 worker 必须在获得对象存储与 Artifact 授权后接入。

## 数据模型

- `agent_test_artifact`：无原文的 immutable metadata、状态、trace 和 CAS version。
- `agent_test_artifact_scan`：扫描器 revision 与四项强制通过结论。
- `agent_test_artifact_approval`：Expert/Security 分离审批。
- `agent_test_fixture_bundle.object_digest`：原始对象 payload 的完整性摘要。
- `agent_test_fixture_stale_outbox`：可重放的依赖失效事件。

迁移由 Atlas schema diff 根据 C1 desired schema 生成；项目历史 `atlas-migrations` 中存在两个同版本
`20260727000000` 文件，Atlas 的 versioned migration hash/diff 会先因该既有目录完整性问题失败。运行
环境实际使用 `atlas schema apply` 的 `desired_schema.sql`；本 PR 不重写已发布历史迁移，也不手改
`atlas.sum`。该历史重复版本必须作为独立修复项处理，不能借 C2 擅自改写已部署迁移记录。

## 明确不成立的结论

本方案不代表：

- 已提供或批准任何真实 Artifact；
- KBD 27123 或其余 125 条 KBD 已生成 Bundle；
- 已建立真实对象存储、PostgreSQL 生产 Registry、Bridge E2E、差分校准或 100+ 并发验证。
