---
status: active
category: solution
audience: developer
last_updated: 2026-05-12
version: v1.0
owner: team
---

# 知识库 AI 格式与存储决策

> **文档目的**：记录关于"AI Agent 所用知识库的最优格式"和"知识库存库 vs 存文件"两个核心架构决策的分析过程与最终结论。
> **关联文档**：[知识库设计.md](./知识库设计.md) | [知识库-消费-优化分析.md](./知识库-消费-优化分析.md)

---

## 一、决策 1：知识库内容的最优格式

### 1.1 分析框架

AI Agent 消费知识库不是"解析数据结构"，而是**基于 token 序列做语义推理**。评估格式的标准：

| 维度 | 核心问题 |
|------|---------|
| **语义密度** | AI 能以最少 token 理解完整意图吗？ |
| **推理可循性** | AI 能沿分支逐步遍历吗？ |
| **歧义性** | 条件与动作是否无歧义？ |
| **可维护性** | 人类维护成本如何？ |
| **工具友好性** | Agent 的代码工具能否程序化解析？ |

### 1.2 不同 Agent 类型的最优格式

#### 纯 LLM Agent（直接问答/RAG）

**最优：结构化 Markdown + 决策表**

LLM 在海量 Markdown 上预训练，这是它的"母语"。关键是用**显式决策语法**而非模糊叙述：

```markdown
## 排障节点：服务无响应

| 条件 | 判断方式 | 结果 → 下一步 |
|------|---------|--------------|
| Pod CrashLoopBackOff | `kubectl get pod` | → [节点A：分析崩溃日志] |
| Pod Running 健康检查失败 | `kubectl describe pod` | → [节点B：检查探针配置] |
| Pod Pending | `kubectl get events` | → [节点C：资源调度问题] |

### 节点A：分析崩溃日志
**IF** 日志包含 `OOMKilled` **THEN** → [节点D：内存限制调整]
**ELSE IF** 包含 `connection refused` **THEN** → [节点E：依赖服务检查]
```

#### Agentic Framework（LangChain / AutoGen 类）

**最优：YAML with Schema + 嵌入 Markdown 描述**

```yaml
nodes:
  node_service_down:
    id: "node_service_down"
    title: "服务无响应"
    description: |
      确认 Pod 运行状态，区分调度问题或运行时崩溃。
    check:
      command: "kubectl get pod -n {namespace} {pod_name}"
      parse: "status.phase"
    branches:
      - condition: "CrashLoopBackOff"
        next: "node_analyze_crash"
      - condition: "Pending"
        next: "node_scheduling_issue"
    fallback: "node_escalate"
```

工具链可程序化遍历，`description` 字段同时给 LLM 语义上下文。

#### 生产级 Agent（结构化输出 + 工具调用）

**最优：JSON 定义拓扑 + Markdown 内容分离**

```json
{
  "id": "node_service_down",
  "title": "服务无响应",
  "check": {"tool": "kubectl_get", "args": {"resource": "pod"}},
  "branches": [
    {"when": {"field": "status", "eq": "CrashLoopBackOff"}, "goto": "node_crash"}
  ],
  "content_md": "## 服务无响应\n确认 Pod 状态..."
}
```

**结构与语义分离**：Agent 用 `tool_call` 遍历节点，用 `content_md` 向用户解释。

### 1.3 本项目最终决策

| 知识类型 | 格式决策 | 理由 |
|---------|---------|------|
| **SOP 决策树** | **YAML**（结构体）+ `content_md`（Markdown 描述） | SOP 需要程序化遍历，不能仅靠 LLM 语义导航 |
| **KBD 案例** | **结构化 Markdown**（`content_md` 字段） | 案例是叙述性知识，Markdown 按语义自然分块，RAG 友好 |
| **分类树** | **YAML 源文件** + DB 运行时缓存 | Git 版本控制 + 程序化访问两者兼顾 |

**关于格式，比选型更重要的 3 个原则**：
1. 条件必须可验证——每个判断节点对应可执行命令或可观测指标，不能是模糊描述
2. 避免超过 4 层的深度嵌套——用节点 ID 引用代替嵌套，AI 可"跳转"而非"深入"
3. SOP 决策树**不应该 embedding 进向量库**——它不是语义召回的对象，而是精确路由后全量注入的对象

---

## 二、决策 2：知识库存库 vs 存文件

### 2.1 问题本质

"存在哪里"本质上是"**谁来消费、如何消费**"的问题。AI Agent 消费知识库有三个阶段，每个阶段对存储形态的需求完全不同：

```
①召回（Retrieval）→ ②注入（Injection）→ ③推理（Reasoning）
  需要：语义搜索      需要：结构化全文       需要：语义密度高
  最优：向量数据库    最优：DB 结构化字段     最优：Markdown/YAML
```

**这是一个伪二选一问题。正确答案是分层存储。**

### 2.2 业界标准三层架构

```
┌─────────────────────────────────────────────┐
│  Layer 3: 文件系统 / Git（源文件）            │
│  用途：人类维护、版本控制、CI/CD 触发同步      │
│  格式：Markdown / YAML（决策树排障手册）       │
├─────────────────────────────────────────────┤
│  Layer 2: 关系型数据库（PostgreSQL）          │
│  用途：结构化元数据、状态管理、权限控制         │
│  内容：category_id、status、版本、创建时间     │
├─────────────────────────────────────────────┤
│  Layer 1: 向量存储（pgvector）               │
│  用途：语义相似召回（RAG 检索阶段）             │
│  内容：embedding chunks + 关联 ID             │
└─────────────────────────────────────────────┘
```

### 2.3 本项目各知识类型的存储策略

| 知识类型 | Git 源文件 | DB 存什么 | 向量存什么 |
|---------|-----------|---------|-----------|
| **SOP 决策树** | YAML 原文（人类维护） | 元数据 + status + category_id + content_md 全文 | **不做整体 embedding**；每节点 description 单独切块 |
| **KBD 案例** | 可选（线上生产环境不依赖文件） | 结构化字段全量存（symptoms / root_cause / resolution 分字段） | `title + symptoms + root_cause` 组合 embedding |
| **分类树** | `category_baseline.yaml`（Git 管理） | kb_category 表，运行时查询 | 不做向量化（精确匹配） |

### 2.4 关键决策点

#### SOP：全文存库 + 精确路由，不做语义召回

SOP 的正确消费方式是：
```
category_id → SQL 精确查 sop_document → 全文注入 context（不是 RAG 召回）
```

把决策树 embedding 进向量库，让 AI 语义搜索"第几步该干什么"是**错误的**。SOP 是程序性知识，应该被完整遍历，不应该被片段召回。

> **✅ 本项目已正确实现**：第1轨 SOP 直查走 category_id → SQL，不走向量检索。

#### KBD：按语义分层存储，精准注入

目前 `kbd_entry` 整条 embedding 存一个向量——这对长文档是不够的。建议优化：

```sql
-- 当前：整条案例一个向量（问题）
embedding vector(1536)  -- 整条 content_md 的向量

-- 优化：按语义段落分开存储（提升召回精度）
-- 新增 kbd_chunk 分块表，或在 kbd_entry 拆分字段
symptoms_embedding   vector(1536)   -- 症状描述向量
root_cause_embedding vector(1536)   -- 根因向量
```

#### SOP 节点级 embedding（待实现）

SOP 决策树的每个节点条件（condition 字段）应单独做 embedding，支持：
- 用户描述症状 → 语义匹配到 SOP 某个具体分支（而不只是 SOP 标题）
- 支持"从中间节点进入"而非总从根节点开始

### 2.5 最终结论

| 结论 | 说明 |
|------|------|
| **存库是对的**（运行时） | DB 是 Agent 的运行时数据源，必须存库 |
| **同时维护文件**（源文件） | Git 里的 YAML/Markdown 是"人类可维护的源文件"，通过 Pipeline 同步到 DB |
| **两者职责不同** | 文件 = 版本控制 + 人类可读；DB = 程序查询 + 状态管理；向量库 = 语义召回 |
| **content 字段存全文** | `sop_document.content_md` 必须存完整原文，不能只存摘要——Agent 需要完整决策树 |

---

## 三、待实现的优化项

| 优先级 | 优化项 | 涉及文档 |
|--------|--------|---------|
| P0 | KBD 按语义段落分块存储和检索 | [知识库-消费-优化分析.md](./知识库-消费-优化分析.md) |
| P1 | SOP 节点级 embedding（支持从中间分支进入） | [知识库-消费-优化分析.md](./知识库-消费-优化分析.md) |
| P1 | SOP 改为 YAML 结构化格式（当前是纯 Markdown） | [知识库-生产-优化分析.md](./知识库-生产-优化分析.md) |
| P2 | KBD 内容结构化（症状/根因/解决方案分字段存储） | [知识库-生产-优化分析.md](./知识库-生产-优化分析.md) |
