---
status: active
category: verify
audience: developer, tester
last_updated: 2026-08-04
owner: team
---

# 验证事件文档

> 本目录存放验证阶段的事件文档，记录"测试方案、验证流程改进和避坑指南升级"。

---

## 文档列表

| 文件 | 日期 | 说明 |
|------|------|------|
| [2026-08-05-hci-sim阶段A-B代码级实施验证报告.md](2026-08-05-hci-sim阶段A-B代码级实施验证报告.md) | 2026-08-05 | in_progress：A/B 代码与本地门禁证据、明确未部署/未做容量验证的边界 |
| [2026-08-05-hci-sim阶段A目录收敛与基础门禁验证方案.md](2026-08-05-hci-sim阶段A目录收敛与基础门禁验证方案.md) | 2026-08-05 | proposed：唯一源码、Go/race、Manifest/Helm、Bridge/真实 SSH 和反退化 CI 验收计划 |
| [2026-08-05-hci-sim阶段B运行时安全与确定性加固验证方案.md](2026-08-05-hci-sim阶段B运行时安全与确定性加固验证方案.md) | 2026-08-05 | proposed：RouteKey、Lease、exec/shell、fault/cancel/output 和资源对抗计划 |
| [2026-08-05-hci-sim阶段C-Fixture编译与注册控制面验证方案.md](2026-08-05-hci-sim阶段C-Fixture编译与注册控制面验证方案.md) | 2026-08-05 | proposed：Resolver、provenance、Mutation、安全扫描、RBAC、stale 和完整性计划 |
| [2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试验证方案.md](2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试验证方案.md) | 2026-08-05 | proposed：TestRun/Scheduler/Runner、状态、清理、无 real fallback 和 1/10 隔离计划 |
| [2026-08-05-hci-sim阶段E产品级验证与规模化运营验证方案.md](2026-08-05-hci-sim阶段E产品级验证与规模化运营验证方案.md) | 2026-08-05 | proposed：10～20 KBD、差分、Mutation、20-repeat、容量、故障和运营计划 |
| [2026-08-05-KBD关键信号四类执行契约验收.md](2026-08-05-KBD关键信号四类执行契约验收.md) | 2026-08-05 | qfk_log/system/service/vm 从 Schema、命令编译、Fake Executor 到 Match/变量池的完整契约矩阵 |
| [2026-08-04-KBD关键信号图片来源门禁收敛验证.md](2026-08-04-KBD关键信号图片来源门禁收敛验证.md) | 2026-08-04 | 四字段图片输入边界与 source ref 实际输入集合门禁收敛验证 |
| [2026-08-04-KBD人工复核标签三态验证.md](2026-08-04-KBD人工复核标签三态验证.md) | 2026-08-04 | 无标签/需人工复核/已人工复核三态、Revision 派生、后端完整测试与 Admin 构建 |
| [2026-07-30-qfk_log统一契约与KBD126日志回归.md](2026-07-30-qfk_log统一契约与KBD126日志回归.md) | 2026-07-30 | 统一 qfk_log、真实 HCI/aCLI 边界、126 条日志 Proposal 分类与跨层验证 |
| [2026-07-29-KBD126扩展语料实施验证.md](2026-07-29-KBD126扩展语料实施验证.md) | 2026-07-29 | 126 条真实来源、222 张截图、分类、Signal/Contract Proposal、Compiler/Replay 与专家门禁分层验证 |
| 2026-07-27-运行时代码完整性修复验证.md | 2026-07-27 | 新镜像实际加载、遗留源码挂载清理与 API Server 准入阻断验证 |
| 2026-07-25-KBD检索正确性残留风险修复.md | 2026-07-25 | KBD 伪向量、中文 FTS、相关性门禁与审计语义修复验证 |
| 2026-07-25-KBD向量搜索失效根因分析与修复.md | 2026-07-25 | KBD 向量搜索配置与查询文本根因分析 |
| 2026-04-02-流程验证问题整改任务.md | 2026-04-02 | 流程验证问题整改任务（从 task/events 迁移）|
| 2026-04-02-避坑指南优化升级方案.md | 2026-04-02 | 避坑指南优化升级方案 |
| 2026-04-02-AI交互全流程验证方案.md | 2026-04-02 | AI 交互全流程验证方案 |

---

## 相关目录

- `../` - verify 主干文档（测试指南.md）
- `../pitfalls/` - 验证类避坑指南
- `../../task/events/` - 任务事件文档
- `../../deploy/events/` - 部署事件文档
- `../../solution/events/` - 方案事件文档

---

*更新日期: 2026-08-05*
