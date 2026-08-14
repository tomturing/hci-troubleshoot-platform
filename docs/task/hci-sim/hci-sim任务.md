# hci-sim 仿真运行时 · 全量任务

> 本文为 hci-sim 子模块的现行全量任务说明，事件化阶段任务见同目录 `events/`。
> 设计侧见 [solution/hci-sim/hci-sim设计.md](../solution/hci-sim/hci-sim设计.md)。

## 1. 实施边界（对抗性审查结论）

hci-sim 当前只能声明「代码级基础就绪」，以下前提未满足即不可宣称产品级验证：

1. 没有 approved Artifact（artifact.approval 双角色审批通过）。
2. 没有真实校准环境与生产 CAS / Bridge E2E。
3. 20-repeat、100+ 并发、真实 Capability Matrix 未验证。

## 2. 阶段任务依赖

严格按 `A → B → C → D → E`；B～E 依次被前置阶段门禁阻断。事件任务见 `events/`。

## 3. 当前 BLOCKED 状态

- KBD 27123 revision 25 已完成真实 Chromium 下的 Admin→Case/Conversation→Agent→K3s terminal_bridge→hci-sim→Result 纵向闭环，仅接受为纵向样板。
- 面向终端用户的生产交付 `BLOCKED`。

## 4. 下一步任务（Bundle 工厂化）

必须把新增 KBD 改造为**无需代码/镜像/Helm/Argo 变更的 Bundle 工厂化发布**：

- [ ] Bundle Compiler 从代码产出转为配置/Artifact 驱动发布。
- [ ] Artifact Gate 全量接入（secret/PII/license/schema 四重扫描 + expert/security 双角色审批）。
- [ ] Capability Matrix 真实采集与缺口可视化。
- [ ] 主库 15 张空旧表 contract/drop 与恢复演练独立完成（独立数据库事件遗留项）。
- [ ] 20-repeat 稳定性与 100+ 并发容量验证补证。

## 5. 用户操作任务（验收入口）

- 在 admin-ui 开仿真会话（对应 KBD 的 caseId）。
- 看 SSH 标签「已连接」（失败看 fatal 提示）。
- 看会话流命令卡片 passed/failed + output，定位失败命令。
- risk=2 命令需手动「允许执行」，避免 inconclusive。

> 注意：旧版 Customer UI 仿真租约表单已迁移至 admin-ui 的 `SimulationConversation.vue`，以受管 terminal_bridge 连接，不再需手动粘贴 connection.json。
