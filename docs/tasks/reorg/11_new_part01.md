<!--
  分片标识：新 11_完整技术方案.md（实施规范 HOW）— 第 1/5 部分
  内容来源：原 11 第 1–102 行（方案总纲 + 全局 Phase 划分）
  合并目标：新 11 文档开头
  说明：保持原文，无删减
-->

# HCI 智能排障平台 — 完整技术方案

> **文档目的**：整合框架选型和各层设计，输出可直接指导开发的完整技术方案，包含实施路径、接口契约、数据模型和里程碑。
> **最后更新**：2026-03-25（§六、§九、§十全量刷新，新增§十一进度快照）
> **关联文档**：[05_AI助手层设计.md](./05_AI助手层设计.md) | [08_HCI平台效果差距分析与重构方案.md](./08_HCI平台效果差距分析与重构方案.md)

---

## 一、方案总纲

### 1.1 目标

将现有的"RAG 文档检索 + 摘要输出"系统，演进为**专业 HCI 智能排障 Agent**：

```
现状：用户提问 → 关键词匹配知识库 → 摘要返回
目标：用户描述/告警输入 → ReAct 推理循环 → 主动调用诊断工具 → 结构化根因分析 → 可执行修复方案
```

### 1.2 核心架构全景（目标态）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         HCI 智能排障 Agent                                    │
│                                                                              │
│  客户端（Vue3）                                                               │
│    ├── SSE 流式对话                                                           │
│    ├── 诊断进度条（S0→S6）                                                   │
│    └── 人工确认弹窗（Level2/3 操作）                                          │
│                              │ HTTP/SSE                                      │
│  API Gateway                 │                                               │
│    └── 路由到 → Conversation Service                                         │
│                              │                                               │
│  Conversation Service（核心）                                                 │
│    ├── ConversationManager   → 会话状态管理（S0-S6）                          │
│    ├── ReactExecutor         → ReAct 推理循环                                 │
│    ├── GLMClient             → LLM接入（格式兼容层）                          │
│    └── PromptBuilder         → 5段式动态 Prompt                              │
│                              │                                               │
│    工具调用层                 │                                               │
│    ├── KnowledgeTools        → 调用 KB Service（RAG检索）                    │
│    ├── SCPAdapter            → 调用 SCP（告警/任务/VM状态/acli）              │
│    └── DialogTools           → ask_user() / confirm_action()                │
│                              │                                               │
│  KB Service（知识库）         │   SCP（平台侧）                               │
│    ├── BM25 + pgvector 检索  │   ├── REST API（告警/任务/VM）                │
│    ├── SOP 文档（600+）       │   └── acli 安全容器（深度诊断命令）            │
│    └── 历史案例               │                                               │
│                              │                                               │
│  PostgreSQL + pgvector       Redis（会话缓存 + 确认等待队列）                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 框架决策

| 组件 | 选型 | 理由 |
|------|------|------|
| ReAct 执行器 | 自实现（~150行） | GLM 格式差异 + 现有架构集成代价；接口对齐 LangGraph，为后续迁移留余地 |
| LLM 接入 | 自实现 `GLMClient` | 处理 GLM 格式差异（并行工具调用、降级行为、历史剪裁）和 JSON 修复 |
| 工具注册 | Pydantic 模型 | 类型安全，含 `risk_level` 元数据；风险级别静态声明，框架强制，LLM 不判断 |
| 状态持久化 | PostgreSQL（扩展现有表） | 利用现有基础设施 |
| 人工确认 | Redis BRPOP + SSE | 利用现有 SSE 通道，BRPOP 阻塞确认（120s 超时）|
| 知识检索 | 现有 KB Service（修复后）| 已有 BM25 + pgvector 实现 |

---

## 二、实施路线图

### 2.1 全局 Phase 划分

```
Phase 0（1周）：Prompt 手术 + 基线修复
  → 重写 _SYSTEM_BASE，解除"禁止使用训练知识"限制
  → 空知识库时改为机制推理而非拒绝回答
  → 验证：GLM 能否用自身知识做初步诊断

Phase 1（2周）：知识库复活
  → 修复 KB Service 部署
  → 规范化 SOP 格式，入库全部 600+ MD 文件
  → 验证：RAG 检索能返回相关内容

Phase 2（2周）：诊断状态机
  → 实现 ConversationSession 状态扩展（S0-S6）
  → Prompt 跟随诊断阶段变化
  → 验证：对话能沿 S0→S1→S2→S3→S4 推进

Phase 3（3周）：ReAct + 工具接入  ← 当前阶段（代码完成，部署待解锁）
  → 实现 ReactExecutor
  → 实现 SCPAdapter（对接 SCP REST API）
  → 实现人工确认机制（Redis + SSE）
  → 接入 get_active_alerts + get_failed_tasks（覆盖 80% 场景）
  → 验证：告警驱动的端到端诊断

Phase 4（持续）：工具扩展 + 自学习
  → 扩展 acli 工具集（vm_power_on / vm_migrate 等操作类工具）
  → 工单关闭后自动学习（LearningClaw）
  → 多域并行诊断（评估引入 LangGraph/AutoGen）
```
