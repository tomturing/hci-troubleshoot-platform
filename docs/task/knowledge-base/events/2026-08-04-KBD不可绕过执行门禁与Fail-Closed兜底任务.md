---
status: proposed
category: task
audience: developer
last_updated: 2026-08-04
owner: team
---

# KBD 不可绕过执行门禁与 Fail-Closed 兜底任务

## 关联文档

- [方案](../../../solution/knowledge-base/events/2026-08-04-KBD不可绕过执行门禁与Fail-Closed兜底方案.md)
- [前置：人工复核标签事实模型纠偏方案](../../../solution/knowledge-base/events/2026-08-04-KBD人工复核标签事实模型纠偏方案.md)

## 当前状态

本任务只完成讨论、事实审计和拟议方案归档。根据用户要求，最终方案确认前不得修改业务代码、
数据库、API、部署配置或现行全量设计文档。

```text
当前：方案待确认
下一步：逐项确认七项硬门禁、能力证据和 rollout 边界
确认后：实施代码与测试
完成后：蒸馏更新现行全量文档和 README 第一屏
```

## 实施任务

| ID | 任务 | 状态 | 验收 |
|---|---|---|---|
| T-KB-HARD-GATE-00 | 归档问题、分析、事实边界和拟议方案 | ✅ 已完成 | solution/task 镜像事件文档和索引完整 |
| T-KB-HARD-GATE-01 | 用户确认 G1～G7、专家权限、能力证据和兜底承诺 | ⏸️ 待确认 | 七项确认无歧义，记录最终决策 |
| T-KB-HARD-GATE-02 | 审计抽取、保存、发布、消费、执行和结论入口的现有门禁及旁路 | ⬜ 待执行 | 形成入口矩阵：已有、缺失、重复、语义冲突 |
| T-KB-HARD-GATE-03 | 收敛 Revision 完整性、只读与编译单一共享实现 | ⬜ 待执行 | 精确 revision/hash/freshness；所有入口复用，无单篇 bypass |
| T-KB-HARD-GATE-04 | 建立版本绑定的目标能力证据和 Fail-Closed 判定 | ⬜ 待执行 | 区分 verified/absent/unverified/probe_failed；静态 Catalog 不单独授权现场执行 |
| T-KB-HARD-GATE-05 | 收敛目标、变量、作用域和依赖 DAG 的静态/动态门禁 | ⬜ 待执行 | 未解析或歧义均阻断；producer 失败不运行 consumer |
| T-KB-HARD-GATE-06 | 收敛执行结果、输出完整性、Matcher 和 Conclusion Gate | ⬜ 待执行 | 失败不进入 Matcher；未知不翻转；证据未闭环不得 Definitive |
| T-KB-HARD-GATE-07 | 补齐结构化审计、管理端真实状态和人工接管信息 | ⬜ 待执行 | 标签只表达事实；失败能定位 revision、目标、阶段和原因 |
| T-KB-HARD-GATE-08 | 完成对抗性、契约、集成和存量 Revision 回归 | ⬜ 待执行 | 方案文档全部反例有自动测试或明确真实环境验证 |
| T-KB-HARD-GATE-09 | 更新现行全量文档、验证事件和 README 第一屏 | ⬜ 待执行 | 文档只在代码事实成立后更新，治理检查通过 |

## 实施约束

1. 未获得用户对最终方案的明确确认前，只允许继续只读审计和文档修订。
2. Candidate / Signal / Rejected Candidate 三态不扩充；稳定拒绝原因仍只有
   `write_signal`、`not_exists`、`run_failed`。
3. Candidate 静态拒绝、运行时 Capability、Signal Outcome 和 Conclusion Decision 分层记录，
   不用一个字段冒充其他层事实。
4. 专家修改后必须重新编译、审核、发布；不实现“强制提升”“忽略校验”“仅本篇放行”。
5. 所有安全判断针对编译后的真实执行向量，不以标题或自然语言动作词代替。
6. 静态 Catalog 不是现场事实；能力证据必须绑定产品版本、目标和有效期。
7. 未知副作用、能力未证实、目标不明、变量未解析和依赖失败一律 Fail Closed。
8. 命令失败、输出缺失、Parser 或 Matcher 不确定不得形成 Signal 成立和 Definitive 根因。
9. fallback 命令必须预声明、同语义、只读、幂等并独立通过契约；禁止 LLM 运行时自由生成。
10. 存量已发布 Revision 不享有旁路；兼容只保证可读取，不保证免验证执行。
11. 不修改或提交工作区中用户已有的 `deploy/helm/hci-platform-obs/values.yaml` 改动。

## 建议实施顺序

```text
确认最终方案
  → 只读入口/旁路审计
  → 共享静态门禁收敛（G1～G3）
  → 目标能力、变量与 DAG 门禁（G4～G6）
  → 结果与结论门禁（G7）
  → 管理端和审计
  → 对抗性测试 + 存量回归
  → 现行文档蒸馏 + PR
```

可以先部署只读 shadow audit，用于统计潜在阻断和修正能力契约；shadow 阶段不得把
“审计通过”显示成已经强制执行。进入 enforce 后不保留 bypass 开关。

## 完成定义

- 用户已经确认最终解决方案；
- G1～G7 全部实现，且不存在专家按钮、历史兼容或单篇配置旁路；
- 当前实现入口矩阵和所有新增门禁都有自动回归；
- 现场能力证据与静态 Catalog 的事实边界可审计；
- 失败时安全停止，不将失败、未知或证据不足显示为诊断成功；
- 相关现行全量文档、验证事件和 README 第一屏按规范更新；
- CI 全绿后通过 PR 合并。
