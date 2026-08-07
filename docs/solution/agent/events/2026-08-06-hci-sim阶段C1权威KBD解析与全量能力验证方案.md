---
status: implemented
category: solution
audience: architect, developer, tester, operator, expert, security
last_updated: 2026-08-06
owner: team
---

# hci-sim 阶段 C1：权威 KBD 解析与全量能力验证方案

## 目标和边界

C1 把阶段 C 的内存参考输入替换为现有 KBD 发布链路中的**只读权威快照**：`kbd_entry` 的
published 状态与 `dynamic_resource_active → dynamic_resource_revision` 的不可变 KBD snapshot。
它提供单条和全量 `support_id` capability report，但不读取或写入真实 HCI、Artifact 原文、Bundle
对象或 TestRun。

126 条 KBD 都进入全量报告；未发布、无 Signal、无 active snapshot 或 Tool Contract 漂移的条目
必须产生结构化 gap，不能因批量验证被隐式发布或自动编译。

## 读取路径与不变量

```text
GET /api/kb/hci-sim/capabilities[/support_id]
        ↓ 仅内部身份
kbd_entry(status=published) + dynamic_resource_active
        ↓ 精确 active_revision
dynamic_resource_revision(status=published, content_json)
        ↓
support_id / checksum / Signal digest / Tool revision / policy revision
        ↓
ready_for_artifact_binding | capability_gap
```

- 不调用 `DynamicResourcePublisher.ensure_published()`；验证是只读操作，绝不将编辑态 KBD 推入
  active。
- active 指针 checksum、revision 行 checksum、snapshot 内 `support_id` 和 KBD 记录必须一致。
- Signal 只读取 snapshot 内的 v2 `signals_json`，而非回读可能已变化的主表。
- `publish_validation.status=passed` 与 Tool Contract revision 必须存在且等于当前代码生成 revision。
- policy revision 来自受版本控制的 `hci_sim_policy_contract()`；它只定义 sim-ssh、published Bundle、
  精确 RouteKey、禁止 real fallback 和 Lease 绑定等安全边界，不复制生产 Tool/命令规则。
- `ready_for_artifact_binding` 不是“已编译”或“已验证”；缺少获批 Artifact 时仍不得生成
  `positive-realistic` Bundle。

## API 契约

| API | 权限 | 副作用 | 用途 |
|---|---|---|---|
| `GET /api/kb/hci-sim/capabilities/{support_id}` | `INTERNAL_API_TOKEN` | 无 | 编译前冻结单条 KBD 输入或读取 gap。 |
| `GET /api/kb/hci-sim/capabilities` | `INTERNAL_API_TOKEN` | 无 | 对全量 KBD 产生 status/gap 聚合和逐条报告。 |

返回的 `resolved` 仅包含 `support_id`、内部 KBD ID、active revision、checksum、signals digest、
Tool/Policy revision 和 source trace；不返回原始 Artifact、任意 URL、Lease 或客户输出。

## 已知能力缺口与下一步

C1 不处理 Artifact approval/provenance、对象存储 prepare/commit、PostgreSQL Bundle Registry CAS、
专家/安全审批或 stale outbox。这些属于 C2，且只有 C2 完成后 `ready_for_artifact_binding` 才能转化为
可审查的 draft Bundle。

关联：[阶段 C 需求](../../../requirement/events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面需求.md)、
[阶段 C 任务](../../../task/agent/events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面任务.md)、
[C1 验证报告](../../../verify/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证报告.md)。
