# hci-sim 仿真运行时 · 任务导航

> 本目录为 hci-sim 子模块的**现行全量任务分支文档**；事件化阶段任务见 [events/](events/)。
> 设计侧见 [solution/hci-sim/README.md](../solution/hci-sim/README.md)，验证侧见 [verify/hci-sim/README.md](../verify/hci-sim/README.md)。

## 现行全量任务

- [hci-sim 任务（全量）](hci-sim任务.md)：实施边界、当前 BLOCKED 状态与下一步 Bundle 工厂化任务清单。

## 阶段化事件任务（按 A→E 严格依赖）

| 阶段 | 任务焦点 | 任务 |
|---|---|---|
| A | 唯一 `hci_sim/` 源码、旧实现处置、基础 CI 门禁 | [目录收敛与基础门禁任务](events/2026-08-05-hci-sim阶段A目录收敛与基础门禁任务.md) |
| B | 运行时安全与确定性加固 | [运行时安全与确定性加固任务](events/2026-08-05-hci-sim阶段B运行时安全与确定性加固任务.md) |
| C | Fixture 编译与注册控制面 | [Fixture 编译与注册控制面任务](events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面任务.md) |
| C1 | 权威 KBD 解析与全量能力验证 | [C1 任务](events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证任务.md) |
| C2 | 获批 Artifact 与不可变 Bundle Registry | [C2 任务](events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry任务.md) |
| C3 | 两步人工验收闭环（admin-ui） | [C3 任务](events/2026-08-06-hci-sim阶段C3两步人工验收闭环任务.md) |
| D | Scenario 调度与通用 KBD 测试 | [Scenario 调度与通用 KBD 测试任务](events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试任务.md) |
| E | 产品级验证与规模化运营 | [产品级验证与规模化运营任务](events/2026-08-05-hci-sim阶段E产品级验证与规模化运营任务.md) |

## 横向重构任务

- [三段链路稳定性任务](events/2026-08-09-hci-sim三段链路稳定性任务.md)
- [三 PR 闭环任务](events/2026-08-10-hci-sim三PR闭环任务.md)
- [独立数据库隔离任务](events/2026-08-10-hci_sim独立数据库隔离任务.md)
- [P0–P1 27123 全链路修复任务](events/2026-08-11-hci-sim-P0-P1-27123全链路修复任务.md)
- [HCI 真实环境与仿真环境双轨测试任务](events/2026-07-30-HCI真实环境与hci-sim双轨实施任务.md)
