# hci-sim 仿真运行时 · 验证导航

> 本目录为 hci-sim 子模块的**验证分支文档**；事件化验证见 [events/](events/)。
> 设计侧见 [solution/hci-sim/README.md](../solution/hci-sim/README.md)，任务侧见 [task/hci-sim/README.md](../task/hci-sim/README.md)。

## 现行全量分析

- [hci-sim 架构评估与构建机制分析](hci-sim架构评估与构建机制分析.md)：代码 + 数据库 + 目录表的构建逻辑完整说明。

## 阶段化验证（按 A→E 严格依赖）

| 阶段 | 核心验证 | 验证计划 / 报告 |
|---|---|---|
| A/B | 唯一源码、Go/race、Manifest/Helm、运行时安全 | [A–B 代码级实施验证报告](events/2026-08-05-hci-sim阶段A-B代码级实施验证报告.md) / [A 验证方案](events/2026-08-05-hci-sim阶段A目录收敛与基础门禁验证方案.md) / [B 验证方案](events/2026-08-05-hci-sim阶段B运行时安全与确定性加固验证方案.md) |
| C | Resolver、Artifact provenance/scan、双角色审批、Manifest/object 双摘要 | [C 验证方案](events/2026-08-05-hci-sim阶段C-Fixture编译与注册控制面验证方案.md) / [C2 验证报告](events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry验证报告.md) |
| C1 | 权威 KBD 解析与全量能力验证 | [C1 验证报告](events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证报告.md) |
| C3 | 两步人工验收闭环（admin-ui） | [C3 验证报告](events/2026-08-06-hci-sim阶段C3两步人工验收闭环验证报告.md) |
| D | TestRun 幂等/CAS、Scheduler/Lease、真实 Runner、Oracle | [D 验证方案](events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试验证方案.md) |
| E | 10～20 KBD、real/sim、Mutation、20-repeat、容量 SLO | [E 验证方案](events/2026-08-05-hci-sim阶段E产品级验证与规模化运营验证方案.md) |

## 横向重构验证

- [三段链路稳定性验证](events/2026-08-09-hci-sim三段链路稳定性验证.md)
- [三 PR 闭环验证](events/2026-08-10-hci-sim三PR闭环验证.md)
- [独立数据库隔离验证](events/2026-08-10-hci_sim独立数据库隔离验证.md)
- [P0–P1 27123 全链路验证](events/2026-08-11-hci-sim-P0-P1-27123全链路验证.md)
- [C–E 控制面代码级实施验证报告](events/2026-08-06-hci-sim阶段C-E控制面代码级实施验证报告.md)

## KBD 27123 专项

- [KBD27123 P0 端到端验证](events/2026-07-30-KBD27123-hci-sim-P0端到端验证.md)
- [KBD27123 三信号执行闭环验证](events/2026-07-27-KBD27123三信号执行闭环验证.md)
- [KBD27123 环境构建持久化修复验证](events/2026-08-11-KBD27123环境构建持久化修复验证.md)
