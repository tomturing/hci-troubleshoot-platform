<!--
  分片标识：新 05_AI助手层设计.md（架构决策 WHY）— 第 2/2 部分
  内容来源：原 05 §十 AI层选型结论（第 ~1660–1680 行）
  合并目标：新 05 全文的后半段
  说明：仅保留选型结论表，§十 的详细分析移至新11（实施规范文档）
-->

## 七、AI 层实现选型：GLM 直接调用 vs OpenClaw（agent runtime）

> 本节记录两种实现路径的最终选型决策。两者详细对比分析见 [11_完整技术方案.md §附录-AI层选型](../architecture/11_完整技术方案.md)。

### 接入方式对比（简要）

| 模式 | 接入层 | 基础设施 | 适用场景 |
|------|--------|---------|---------|
| **直接调用 GLM API** | `OpenClawAssistant` 类（复用同一接口）| API Key，零 K8s 资源 | 快速启动、成本优先 |
| **通过 OpenClaw**（agent runtime）| 相同接口规范 `/v1/chat/completions` | Pod 部署 + 热备池 | 长期 Agentic 路径 |

> 两者在 `ai_client.py` 中使用完全相同的 `OpenClawAssistant` 类——因为 `/v1/chat/completions` 接口规范相同，可随时切换。

### 选型结论

| 使用场景 | 推荐 |
|---------|------|
| 快速启动、成本优先、确定性排障 | 直接调用智谱 GLM API |
| 阶段一 RAG 注入排障（当前阶段）| 两者等效，OpenClaw 略复杂但可积累 agent 能力 |
| 阶段二 Agentic RAG（KB 自主检索）| **必须选 OpenClaw**，GLM API 无法实现自主工具调用闭环 |
| A/B 评估对照 | 启用两者，通过 `assistant_type` 字段分组采集数据 |
| 长期生产路径 | OpenClaw（能力演进路径更完整）|

> **核心结论**：当前阶段（Stage 1）两者能力接近，OpenClaw 作为 agent runtime 的真正优势要到 Stage 2 才能发挥。建议保留 OpenClaw 作为主路径，接入智谱作为对照组，通过 `assistant_evaluation` 表采集数据做质量对比。

---

## 八、长期架构方向：MCP Server 统一工具接口

> 本节是对第二节方案 C 的远期展望，不涉及当前实施。

```
长期目标状态（Phase 4+）：

AI Agent (ProductionClaw/LearningClaw)
    │
    │ 工具调用（统一 MCP 协议）
    ▼
┌────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│  KB MCP Server  │ │  SCP MCP Server  │ │  Context MCP     │
│  知识检索/入库   │ │  HCI 诊断工具     │ │  Case 上下文管理  │
└────────────────┘ └──────────────────┘ └──────────────────┘
```

**迁移条件（当满足以下条件时可启动 MCP 迁移）**：
1. Phase 3 ReAct 架构稳定运行（工具调用体系成熟）
2. 有第二类 AI 助手接入需求（需要统一接口）
3. 评估 openclaw 的原生 MCP 支持成熟度

在此之前，通过 `SCPAdapter` + `KnowledgeTools` 的 REST 方式满足工具调用需求。

---

*文档定位：WHY（架构决策背景） | 版本：4.0 | 最后更新：2026-03-25*
*实施细节见：[11_完整技术方案.md](../architecture/11_完整技术方案.md)*
