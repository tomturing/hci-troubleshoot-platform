# hci-sim 仿真运行时 · 设计导航

> 子模块定位：`hci_sim/` 是强隔离、确定性、带租约鉴权的 HCI 仿真 SSH Runtime，用于在不可触碰真实生产客户环境的前提下，对诊断/排障知识库（KBD）的修复命令做端到端验证。
> 本目录为 hci-sim 子模块的**现行全量设计分支文档**；事件化阶段设计见 [events/](events/)。

## 现行全量设计

- [hci-sim 设计（全量）](hci-sim设计.md)：架构、四域数据库、CI 门禁、部署与用户视角的完整说明。

## 阶段化事件设计（按 A→E 严格依赖）

| 阶段 | 设计焦点 | 方案 |
|---|---|---|
| A | 唯一 `hci_sim/` 源码、旧实现处置、基础 CI 门禁 | [目录收敛与基础门禁](events/2026-08-05-hci-sim阶段A目录收敛与基础门禁方案.md) |
| B | Manifest v2、强 RouteKey、Lease/exec/shell/fault/output 安全内核 | [运行时安全与确定性加固](events/2026-08-05-hci-sim阶段B运行时安全与确定性加固方案.md) |
| C | 不可变 KBD 输入、Fixture Compiler、Registry、审批和 stale | [Fixture 编译与注册控制面](events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面方案.md) / [C2 Artifact Gate 与对象完整性](events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry方案.md) |
| C1 | 权威 KBD 解析与全量能力验证 | [C1 方案](events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证方案.md) |
| C3 | 两步人工验收闭环（admin-ui） | [C3 方案](events/2026-08-06-hci-sim阶段C3两步人工验收闭环方案.md) |
| D | TestRun API、Scheduler、Lease、缓存、真实 Bridge/Agent Runner | [Scenario 调度与通用 KBD 测试](events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试方案.md) |
| E | real/sim 差分、Mutation、稳定性、容量、SLO 和运营 | [产品级验证与规模化运营](events/2026-08-05-hci-sim阶段E产品级验证与规模化运营方案.md) |

## 横向重构事件

- [三段链路稳定性设计](events/2026-08-09-hci-sim三段链路稳定性设计.md)
- [三 PR 闭环方案](events/2026-08-10-hci-sim三PR闭环方案.md)
- [独立数据库隔离方案](events/2026-08-10-hci_sim独立数据库隔离方案.md)
- [P0–P1 27123 全链路修复方案](events/2026-08-11-hci-sim-P0-P1-27123全链路修复方案.md)
- [生产化差距审查与 Bundle 工厂化重构基线](events/2026-08-11-hci-sim生产化差距审查与Bundle工厂化重构基线.md)
- [HCI 真实环境与 hci-sim 双轨运行时设计](events/2026-07-30-HCI真实环境与hci-sim双轨运行时设计.md)

## 快速导航

| 我要做什么 | 看哪个 |
|-----------|--------|
| 实施或审查当前阶段 | 上表（严格按 A→E） |
| 看用户视角（喂 KBD / 拿 Lease / 在哪验收 / 结果过没过） | [hci-sim 设计（全量）](hci-sim设计.md) |
| 看验证证据 | [verify/hci-sim/README.md](../verify/hci-sim/README.md) |
| 看任务排期 | [task/hci-sim/README.md](../task/hci-sim/README.md) |
