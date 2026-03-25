# 架构决策记录（ADR）

> Architecture Decision Records — 记录每一个重要架构决策的背景、选项、结论和后果。
>
> **格式约定**：每个决策独立一个文件，状态一旦变更在文件头部更新即可。
>
> **状态说明**：
> - `proposed`：提议中，待讨论
> - `accepted`：已采纳，当前执行标准
> - `deprecated`：已废弃，有新决策替代
> - `superseded`：被新 ADR 取代（注明取代者编号）

---

## 决策索引

| 编号 | 标题 | 状态 | 决策日期 |
|------|------|------|---------|
| [ADR-001](001-选择K3s作为生产编排.md) | 选择 K3s 而非标准 K8s | accepted | 2025-10 |
| [ADR-002](002-GitOps双仓模型.md) | GitOps 双仓模型设计 | accepted | 2026-03 |
| [ADR-003](003-RAG对接架构决策.md) | AI 层与 RAG 层对接方式三阶段演进 | accepted | 2026-03 |
| [ADR-004](004-发布工作流方案选型.md) | 发布工作流方案选型（方案21 vs 方案22） | accepted | 2026-03 |
| [ADR-005](005-Helm-Chart资源归属拆分.md) | Helm Chart 资源归属拆分（ArgoCD 资源竞争消除） | accepted | 2026-03 |
