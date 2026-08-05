---
status: in_progress
category: verify
audience: developer, tester, operator, expert, security, product
last_updated: 2026-08-06
owner: team
---

# hci-sim 阶段 C1：权威 KBD 解析与全量能力验证报告

## 验证范围

本报告验证 KBD/Signal/Contract 快照是否可作为后续 Fixture Compiler 的不可变输入，**不**验证
真实 HCI、Artifact、Bundle、Agent/Bridge E2E 或产品级效果。

## dev 只读基线（2026-08-06）

在 `hci-dev` PostgreSQL 执行只读聚合查询，未写入 KBD、dynamic resource、Artifact 或 TestRun：

| 指标 | 结果 | 解释 |
|---|---:|---|
| KBD 总数 | 126 | 全部纳入 capability report。 |
| `published` KBD | 6 | 其余 120 条不能被 Compiler 当作可执行知识。 |
| `published` 且有 Signal | 6 | 与已发布 KBD 数一致。 |
| KBD revision | 192 | 历史版本可用于审计，但 C1 只读取 active 动态资源 snapshot。 |
| active KBD dynamic resource | 6 | 与 published KBD 数一致。 |
| active snapshot identity/checksum mismatch | 0 | 6 个 active snapshot 的 ID/checksum 一致。 |
| `ready_for_artifact_binding` | 2 | Tool Contract 仍为当前 revision，可进入 C2 Artifact 绑定。 |
| `TOOL_CONTRACT_STALE` | 4 | 需要重新抽取/发布 Signal，不能编译。 |
| `KBD_NOT_PUBLISHED` | 120 | 需完成 KBD 发布与 Signal 审核，不得自动提升状态。 |

因此，当前全量报告预期为 2 条 `ready_for_artifact_binding` 和 124 条 `capability_gap`。即使那 2 条
也没有获批 Artifact/provenance，不能称为 compiled、validated 或 E2E passed。

## 代码级验证

在项目 kb-service Python 3.12 运行镜像中直接执行新增 Resolver 单元函数，结果通过：

```text
resolver unit functions: PASS
```

覆盖：current revision 冻结、未发布/缺 active snapshot 拒绝、Tool stale 拒绝、snapshot identity 篡改
拒绝、以及批量报告不将 Artifact 绑定状态伪装为验证通过。

本机 `uv` 选择 Python 3.14，而锁定的 `asyncpg==0.29.0` 不支持该解释器，故没有将该本地工具链构建
失败记为代码失败；PR CI 使用项目支持的 Python 版本重新执行 pytest/ruff。

## 尚未通过的门禁

- C2 Artifact approval/provenance、扫描、对象存储 immutable publish 和 PostgreSQL Registry CAS；
- 可用 KBD 的 Fixture draft/variant/oracle/mutation；
- Runtime Bundle 加载、TestRun/Runner、受控 real/sim calibration、20-repeat 和容量验证。

关联：[C1 方案](../../solution/agent/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证方案.md)、
[C1 任务](../../task/agent/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证任务.md)。
