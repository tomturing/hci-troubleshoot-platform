# AI Agent 技术原理深度讲解

**版本**: v1.0  
**日期**: 2026-03-23  
**定位**: 技术基础文档，理解 Agent 架构的前置必读  
**核心命题**: 验证并深化"所有的一切最后都聚焦在 Context Window"这一洞察  
**适读对象**: 所有参与本项目的工程师和产品负责人

---

## 零、先验证你的核心洞察

> **用户洞察**：「所有的一切最后都聚焦在上下文窗口（Context Window），也就是 prompt。agent 就是利用 Skill、Memory、RAG、Tool、MCP 来丰富和校正 prompt 然后喂给 LLM。」

**这个洞察 90% 正确，是真正的第一性原理。**

正确的部分：
- ✅ Context Window 是 LLM 推理的唯一输入，这是物理事实
- ✅ RAG / Memory / Tool 结果都必须"物化"为 token 进入 context window 才能影响 LLM
- ✅ Agent 的本质就是不断丰富 context window 的过程

需要补充的 10%：
- ⚠️ **参数化知识（Parametric Memory）** 不在 context window 中，但永远有效
  - LLM 在训练时压缩进权重的知识（Python 语法、HCI 通用原理、逻辑推理能力）
  - 这些知识"无形地"参与每次推理，不占 context window 一个 token
  - 这是 GPT-4/Claude 与 GLM-4 能力差距的根本来源之一：权重里压缩的知识量不同

```
完整的 LLM 知识来源：

  参数化知识（训练所得）          上下文知识（运行时注入）
  ────────────────────          ──────────────────────
  永远有效，不占 token            只在本次推理有效，占 token
  不可在运行时修改                可以按需更换和扩展
  LLM "与生俱来"的能力            Agent 动态组装的能力
         │                               │
         └──────────────┬────────────────┘
                        ▼
              LLM 推理输出（一次 forward pass）
```

**结论：你的洞察抓住了 Agent 架构的核心本质。下文在此基础上展开。**

---

## 一、LLM 是什么：一个极其强大但极其"短视"的函数

### 1.1 LLM 的数学本质

```
LLM = f(tokens_in_context_window) → next_token_probability_distribution

每次推理，LLM 就是做一件事：
  "给定当前 context window 中的所有 token，
   预测下一个 token 是什么（概率分布）"

反复采样下一个 token，直到生成 <end> token，得到完整输出。
```

**这就是全部。** LLM 没有内部状态，没有记忆，没有感知，没有行动能力——它只是一个（非常强大的）函数。

### 1.2 "短视"的含义

```
对 LLM 来说，不在 context window 里的东西 = 不存在

  你上周跟它说的话？不存在（新会话 context 是空的）
  外部数据库里的数据？不存在（没有注入 context）
  真实世界当前的状态？不存在（没有工具采集后注入）
  你的用户身份？不存在（没有在 system prompt 里说明）

一切信息必须"物化"为 token，才能影响 LLM 的行为。
```

### 1.3 这引出了 Agent 存在的根本原因

如果 LLM 是一个短视的函数：
- 怎么让它"记住"跨会话的信息？→ **Memory 系统**
- 怎么让它"知道"外部数据库的内容？→ **RAG 检索**
- 怎么让它"能做"现实世界的操作？→ **Tool Use**
- 怎么让它"会用"特定领域技能？→ **Skill 注入**
- 怎么让它"可以访问"外部服务？→ **MCP 协议**

**Agent = 让 LLM 突破"短视"局限的工程框架。**每个组件的作用，都是将外部信息转化为 context window 中的 token。

---

## 二、Agent 的核心循环：ReAct

### 2.1 ReAct 是什么（Reasoning + Acting）

ReAct 是目前工业界应用最广泛的 Agent 工作模式，由 Princeton/Google 在 2022 年提出。

核心思想：**让 LLM 在推理（Thought）和行动（Action）之间交替运行**，每次行动的结果作为新的观测（Observation）追加进 context window，再触发下一轮推理。

```
用户输入（进入 context window）
         │
         ▼
┌────────────────────────────────────────────────────────────┐
│  Thought（思考）                                           │
│  LLM 内部推理："我需要什么信息？下一步做什么？"             │
│  这段思考本身也会写入 context window                       │
└────────────────────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────────┐
│  Action（行动）                                            │
│  LLM 输出结构化的工具调用请求                              │
│  eg: {"tool": "acli_vm_status", "args": {"vm_id": "vm-1"}}│
└────────────────────────────────────────────────────────────┘
         │ 框架执行工具，获取结果
         ▼
┌────────────────────────────────────────────────────────────┐
│  Observation（观测）                                       │
│  工具执行结果追加到 context window                         │
│  eg: "vm-1: STATUS=ERROR, last_error=磁盘路径不可达"      │
└────────────────────────────────────────────────────────────┘
         │
         ▼
    继续 Thought → Action → Observation 循环
         │
         ▼
    最终输出（Final Answer）
```

### 2.2 每次循环都在扩展 context window

这是理解 Agent 的关键：**ReAct 的每次循环都在往 context window 里追加新的 token**。

```
循环开始时的 context window:
  [System Prompt] [工具定义] [用户消息]
  ↓ 第1次推理
  [System Prompt] [工具定义] [用户消息] [Thought-1] [Action-1]
  ↓ 工具执行，结果追加
  [System Prompt] [工具定义] [用户消息] [Thought-1] [Action-1] [Obs-1]
  ↓ 第2次推理
  [System Prompt] [工具定义] [用户消息] [Thought-1] [Action-1] [Obs-1] [Thought-2] [Action-2]
  ↓ ...
  随循环进行，context window 持续增长
```

**这也是 context window 大小至关重要的原因**：窗口越大，Agent 能完成的推理步骤越多，能积累的信息越多。

### 2.3 终止条件

```python
while True:
    thought = llm.generate(context_window)  # 推理
    
    if thought.is_final_answer:
        return thought.content              # 任务完成，退出
    
    action = thought.extract_action()
    if action.risk_level > user_auth_level:
        # 等待用户授权（本项目：风险分级授权机制）
        await user_confirmation()
    
    observation = execute_tool(action)
    context_window.append(thought, action, observation)  # 扩展 context
    
    if len(context_window) > MAX_TOKENS:
        context_window = compress(context_window)        # 压缩（MemGPT 方案）
    
    if loop_count > MAX_STEPS:
        return "超过最大步数，请人工介入"               # 防止无限循环
```

---

## 三、五大组件的技术本质

### 3.0 Context Window 的所有内容来源（完整清单）

Skill/Memory/RAG/Tool/MCP 是**机制层**的五分类，但 context window 的实际内容来源更多：

| 来源 | 在 context 中的体现 | 易被忽视的原因 |
|------|-------------------|---------------|
| **用户输入本身** | `role=user` 的消息 | "太显然了"，不列入机制讨论 |
| **对话历史** | 累积的 user/assistant 轮次 | 视为"背景"而非"主动提供" |
| **LLM 自我生成的推理（CoT）** | ReAct 的 Thought 步骤 | **最容易被忽略，也最重要** |
| **运行时元数据** | 时间戳、用户身份、工单 ID | 工程实现时常遗漏 |
| **结构化状态对象** | 诊断阶段 JSON、已确认事实 | 本项目专有设计 |
| Skill | 方法论/领域知识段落 | System Prompt 固定区 |
| Memory（情节记忆） | recall 召回的历史案例 | Tool Results 动态区 |
| RAG | 检索到的 KB chunks | Tool Results 动态区 |
| Tool 定义 | 工具 JSON Schema | Tool Definitions 区 |
| Tool 结果 | 工具执行返回值 | Tool Results 动态区 |
| MCP | 外部服务工具定义/结果 | 同 Tool |

**其中最值得单独说明的是「LLM 自我生成的推理（CoT）」**：

```
ReAct 中的 Thought 步骤写回 context window 后，
会成为 LLM 下一步推理的输入。

LLM 在"给自己写参考资料"——这是一种自举（bootstrapping）。

效果：显式写出推理链 → 下一步推理质量更高
      （这就是 Chain-of-Thought prompting 有效的根本原因）

本项目含义：
  不要让 LLM 直接输出最终诊断命令
  要先让它"说出"诊断思路，再给出命令建议
  这段"说出来的思路"本身就在改善下一步的 context 质量
```

> **结论**：五大组件（Skill/Memory/RAG/Tool/MCP）是设计层的分类；用户输入、对话历史、CoT 推理、运行时元数据、状态对象同样是 context window 的重要组成部分，工程实现时不能忽视。

---

### 3.1 统一视角：所有组件都是"context window 的内容提供者"

```
┌───────────────────────────────────────────────────────────────────┐
│                     Context Window                                │
│  ┌────────────┐ ┌──────────┐ ┌──────────┐ ┌───────────────────┐ │
│  │System Prompt│ │Tool Defs │ │User Msgs  │ │Tool Results/Obs  │ │
│  │  (Skill/   │ │ (Tool/   │ │ + History │ │  (RAG/Memory/    │ │
│  │  Memory)   │ │  MCP)    │ │           │ │   Tool outputs)  │ │
│  └────────────┘ └──────────┘ └──────────┘ └───────────────────┘ │
└───────────────────────────────────────────────────────────────────┘
         ▲              ▲            ▲               ▲
         │              │            │               │
      Skill /        Tool /       对话历史          执行结果
      Memory        MCP 注册     (持久化)          (动态追加)
      （静态）       （元数据）   （检索注入）       （运行时）
```

每个组件向 context window 贡献的 **token 类型**和**贡献时机**不同：

| 组件 | 写入 context window 的内容 | 写入时机 | token 位置 |
|------|--------------------------|---------|-----------|
| **Skill** | 能力描述、方法论、领域知识 | 会话开始时（静态） | System Prompt |
| **Memory（工作记忆）** | 当前任务状态、已知事实 | 每轮更新 | System Prompt 尾部 |
| **Memory（情节记忆）** | 历史案例、过去经验 | LLM 主动 recall 时 | Tool Results |
| **RAG** | 检索到的相关文档片段 | 触发检索时 | Tool Results |
| **Tool（定义）** | 工具名称、参数 Schema | 会话开始时（静态） | Tool Definitions |
| **Tool（结果）** | 工具执行的返回值 | 每次工具调用后 | Tool Results |
| **MCP** | 外部服务的工具定义和结果 | 同 Tool | Tool Definitions + Results |

### 3.2 Skill（技能）

**技术本质**：将程序性知识（Procedural Knowledge）编码为 System Prompt 的固定部分。

```
Skill 的信息流向:
  
  专家知识（人类编写或从案例提炼）
          ↓
  结构化文本（Markdown 格式最佳）
          ↓
  写入 System Prompt 的特定段落（静态，每次推理都在）
          ↓
  LLM 在推理时以此为"行为准则"和"背景知识"
```

**Skill 和普通文本的区别**：

```
普通文本（RAG chunk）：
  "CPU overcommit 是指虚拟机配置的 vCPU 总数超过物理 CPU 核数..."
  LLM 将其视为"参考资料"，可能采纳也可能忽略

Skill（System Prompt 的方法论段落）：
  "当用户描述 VM 启动失败时，你必须按以下顺序诊断：
   1. 先收集症状（现象、时间、范围）
   2. 再形成假设（3个以内，按概率排序）
   3. 最后验证假设（最小代价先行）
   ——你不得跳过步骤，不得在信息不足时给出确定性答案"
  LLM 将其视为"必须遵守的工作规程"，行为一致性高得多
```

**关键特性**：
- 无检索开销（始终驻留 context window）
- 影响 LLM 的**行为模式**，而非仅仅提供知识
- Token 消耗固定可预测
- 适合"高频使用、内容稳定"的知识（例如：排障方法论、HCI 核心机制）

**对本项目的映射**：
- `_SYSTEM_BASE` 的 Segment 1（专家身份）和 Segment 2（诊断方法论）= Skill 的典型实现
- 机制知识锚（HCI 系统核心原理）= Skill 层的知识内容

### 3.3 Memory（记忆）

Memory 是 Agent 架构中最复杂的组件，因为它横跨**时间维度**（跨会话持久化）和**空间维度**（如何在 context window 中表示）。

**三种记忆的技术实现对比**：

#### 工作记忆（Working Memory）

```
技术实现：context window 本身
容量：受 context window 大小限制
持久性：仅在当前推理会话，会话结束即清空
写入方式：LLM 推理过程中自然形成（Thought / Action / Observation）

特点：
  - 无需额外工程，context window 天然就是工作记忆
  - 容量是瓶颈（GLM-4 约 32K 可用，Claude 可达 200K）
  - 内容对 LLM "完全透明"（全部在窗口内）
```

#### 情节记忆（Episodic Memory）

```
技术实现：外部数据库（PostgreSQL JSONB + 向量索引）
容量：无限（受存储限制，不受 context 限制）
持久性：跨会话永久保存
写入方式：LLM 调用 create_memory() 工具，或工单关闭时自动固化

进入 context window 的方式（关键）：
  LLM 主动调用 recall_memory(query) 工具
           ↓
  数据库检索返回相关情节
           ↓
  情节内容作为 Tool Result 追加到 context window
           ↓
  变成 LLM 可见的"参考资料"

特点：
  - 不主动召回 = 不占 token（按需付费）
  - 召回质量决定了 LLM 能得到多好的"提示"
  - LLM 是主动管理者（自己决定 recall 什么）而非被动接受者
```

#### 语义记忆（Semantic Memory / Parametric）

```
技术实现：LLM 权重本身（不可在运行时修改）
容量：无限（压缩在数十亿参数中）
持久性：永久（随模型版本更新）
写入方式：训练时学习（Fine-tuning / RLHF），不是运行时可控的

进入 context window 的方式：
  不需要进入——直接影响每个 token 的生成概率
  这是"无形的知识"，不占 context window 任何空间

特点：
  - 对 GLM-4：通用训练知识（Python、英语、常规逻辑），Sangfor HCI 私有知识缺失
  - 对 Claude：更广泛的训练数据，HCI 相关技术文档可能在训练集中
  - 无法在生产环境修改（需要重新训练/微调）
```

#### 三种记忆的组合策略

```
设计原则：Semantic（权重）> Working（context）> Episodic（外部）

优先级含义：
  能靠模型自身知识解决的，不额外注入 token
  需要当前会话上下文的，放在 context window
  需要历史经验的，按需从外部召回

本项目场景：
  HCI 通用原理 → 尽量注入 System Prompt Segment 3（Skill 层弥补参数不足）
  当前工单状态 → 工作记忆（context window 中的 JSON 状态块）
  历史案例      → 情节记忆（按需 recall，不预先注入）
```

### 3.4 RAG（检索增强生成）

**技术本质**：一种将大规模外部知识库的**相关片段**动态注入 context window 的技术。

```
标准 RAG 信息流：

  用户 query（或 LLM 生成的精化 query）
          ↓
  向量化（embedding model）
          ↓
  向量数据库检索（cosine 相似度 / BM25 混合）
          ↓
  top-K 文档片段（chunks）
          ↓
  ★ 这些 chunks 追加到 context window ★
          ↓
  LLM 基于 chunks + 原始 query 生成回答
```

**RAG 的本质局限**（重要）：

RAG 解决的是"知识太多，不能全部放进 context window"的问题，但它引入了新的问题：

```
问题1：检索相关性 ≠ 推理有用性
  检索到的 top-1 文档语义最相似，但可能是回答 2 年前类似案例的文档
  LLM 需要的不是"最相似的文档"，而是"解决当前问题所需的知识"

问题2：检索时机问题（本项目的核心痛点）
  在用户第一句话就触发 RAG：query = "VM起不来"（极度模糊）
  在 S1 阶段（明确了故障域）触发：query = "VM启动失败 存储扩容后 STATUS=REMOUNTING"
  两者的检索质量天壤之别

问题3：RAG 把知识当"答案"而非"素材"
  RAG 设计假设：找到相似文档 → 文档就是答案
  推理优先设计：找到相似文档 → 文档是推理的素材 → LLM 结合当前环境推断
```

**RAG vs Memory 的关键区别**：

```
RAG（被动）：
  每次消息 → 触发检索 → 检索结果注入 → LLM 被动接受
  检索决策者：平台（根据用户 query 自动触发）

Memory recall（主动）：
  LLM 推理 → 决定"我需要什么参考" → 调用 recall 工具 → 获取结果
  检索决策者：LLM 自己
```

这就是方案 A（认知记忆）比传统 RAG 更强的本质：**把检索的控制权从平台交给 LLM**。

### 3.5 Tool（工具）

**技术本质**：让 LLM 通过结构化输出请求执行代码，将执行结果写回 context window。

```
工具在 context window 中的两个存在形态：

  形态1：工具定义（Tool Definition）
    ─ 位置：context window 开头（System Prompt 或专用区域）
    ─ 内容：工具名称、功能描述、参数 Schema（JSON Schema 格式）
    ─ 作用：让 LLM "知道有哪些能力可用"
    ─ 示例：
      {"name": "acli_vm_status",
       "description": "查询虚拟机当前运行状态",
       "parameters": {"vm_id": {"type": "string", "description": "VM的唯一标识符"}}}

  形态2：工具调用结果（Tool Result / Observation）
    ─ 位置：对话历史中，跟在 assistant 的工具调用请求后面
    ─ 内容：工具实际执行的返回值
    ─ 作用：让 LLM "看到真实世界的状态"
    ─ 示例：
      {"tool_result": "vm-1: STATUS=ERROR, last_error=磁盘路径不可达, ts=2026-03-23T09:15:00Z"}
```

**工具定义的 token 成本不可忽视**：

这是工程实践中常被忽略的问题（你分享的截图中 Tool Definitions 占 10.8%）。

```
每个工具定义 ≈ 100-400 tokens
10 个工具 ≈ 1000-4000 tokens
30 个工具 ≈ 3000-12000 tokens

如果工具库很大，需要"动态工具注册"策略：
  不把所有工具定义全部塞进 context window
  而是根据当前诊断阶段，只注册相关工具
  eg: S0-S1 阶段只注册"信息收集工具"，S3 阶段才注册"执行类工具"
```

**Function Calling vs ReAct 的区别**：

```
ReAct（文本格式）:
  LLM 输出：
  "Thought: 我需要查看VM状态
   Action: acli_vm_status(vm_id='vm-1')"
  
  框架解析这段文本，提取工具名和参数并执行
  问题：LLM 可能输出格式不稳定（需要正则解析，容易出错）

Function Calling（结构化格式）:
  LLM 输出（JSON）：
  {"tool_calls": [{"name": "acli_vm_status", "arguments": {"vm_id": "vm-1"}}]}
  
  框架直接解析 JSON，不需要文本解析
  优势：格式稳定，解析可靠
  本项目：GLM-4 的 Function Calling 需要 JSON 修复兜底（T09 的 _safe_parse_json）
```

### 3.6 MCP（Model Context Protocol）

**技术本质**：Anthropic 在 2024 年底提出的工具调用**标准化协议**，解决的是"每个 AI 应用都要手写适配器"的问题。

```
没有 MCP 时（现状）：

  应用A ──自定义格式──→ 工具1（acli）
  应用B ──自定义格式──→ 工具1（acli）   ← 每个应用各写一套适配代码
  应用C ──自定义格式──→ 工具2（SCP API）

有 MCP 后（标准化）：

  应用A ──MCP 协议──→ MCP Server（acli）──→ 工具1
  应用B ──MCP 协议──→ MCP Server（acli）──→ 工具1   ← 适配代码只写一次
  应用C ──MCP 协议──→ MCP Server（SCP）──→ 工具2
```

**MCP 在 context window 中的表现**：

MCP 本身是通信协议，不改变 context window 的结构。从 context window 的视角来看，MCP 提供的工具和普通工具没有区别——都是 Tool Definitions + Tool Results。

MCP 解决的是**工具的供给侧标准化**，不改变**需求侧（context window）的工作方式**。

---

## 四、不同 Agent 架构的分类

### 4.1 按控制流分类

```
类型1：单步 Agent（Single-Step）
  用户输入 → 一次 LLM 推理（含工具调用） → 输出
  适用：简单问答、单次检索
  本项目：Phase 0-2 的 Prompt 重构大致属于此类

类型2：ReAct Agent（多步循环）
  用户输入 → N 次 Thought-Action-Observation 循环 → 输出
  适用：需要多步推理、多次工具调用
  本项目：Phase 3+ 的目标架构

类型3：Plan-and-Execute Agent
  先规划（生成完整计划） → 再逐步执行
  适用：任务复杂、步骤可预见
  代表：AutoGPT（早期），LangGraph

类型4：Multi-Agent（多 Agent 协作）
  主 Agent 分解任务 → 多个子 Agent 并行执行 → 结果聚合
  适用：复杂、可并行的任务
  本项目远期方向：LearningClaw + ProductionClaw 的角色分离
```

### 4.2 按记忆架构分类

```
无状态 Agent：
  每次对话独立，context window 不保留跨会话信息
  → 当前系统的现状

有状态 Agent（Session Memory）：
  会话内有状态，会话间无状态
  → Phase 2 目标（S0-S6 诊断状态机）

持久化 Agent（Long-term Memory）：
  跨会话持久化记忆，随时间"越来越聪明"
  → Phase 3 目标（方案A：情节记忆自增长）
```

---

## 五、本项目的 Agent 设计选型

### 5.1 当前状态（Phase 0 之前）

```
类型：单步伪 Agent（表面上有 RAG，实质上是增强型问答）
控制流：用户消息 → 平台检索 → 注入 context → 一次 LLM 生成 → 输出
工具调用：无（RAG 注入不是 LLM 主动调用工具）
记忆：会话内有对话历史，跨会话无记忆
缺陷：
  KB Service 未上线 → RAG 注入为空 → LLM 触发"知识库未收录"逻辑
```

### 5.2 Phase 2 目标（诊断状态机 + 改进 RAG）

```
类型：有限状态 ReAct Agent
控制流：用户消息 → 按诊断阶段(S0-S6)选择工具 → ReAct 循环 → 输出
工具调用：LLM 主动调用诊断工具（acli / SCP）
记忆：工单内有状态（诊断阶段、假设列表、已知环境信息）
改进：延迟 RAG 触发（S1 之后才检索）
```

### 5.3 Phase 3-5 目标（认知记忆 + 因果图 + 策略蒸馏）

```
类型：持久化推理 Agent（目标是 Claude 式体验）
控制流：
  认知记忆（方案A）→ LLM 自主 recall 历史案例
  因果图（方案B）   → 算法辅助工具选择（最大信息增益）
  策略蒸馏（方案C） → 小模型路由标准场景，GLM-4 处理复杂场景
工具调用：完整工具集（知识工具 + 诊断工具 + 操作工具）
记忆：三层（Core / Episodic / Working），跨工单积累，自进化
```

---

## 六、Agent 设计的关键工程原则

### 原则 1：Context Window 是最贵的资源，精打细算

```
每个 token 都有成本（API 计费 + 推理延迟）。
好的 Agent 设计：每个进入 context window 的 token 都在发挥作用。
坏的 Agent 设计：把所有可能相关的内容都塞进去，"反正放多点没事"。

量化思维：
  GLM-4 128K context，实际稳定使用建议 ≤ 32K
  System Prompt（含工具定义）≤ 8K
  留给对话历史 + 工具结果≈ 20K
  留给输出 ≈ 4K
  
  每增加一个工具定义（~300 tokens）= 减少 ~300 tokens 的对话历史空间
```

### 原则 2：动态 > 静态，按需 > 预注入

```
静态注入（总是在 context window）：只注入 Core Memory（机制知识、方法论）
动态注入（按需追加）：
  - 案例召回（recall_memory）
  - 工具执行结果
  - 特定阶段的工具定义

反模式（本项目 Phase 0-2 前的状态）：
  每次消息都注入大量 RAG chunks
  = 消耗固定 token 预算，但利用率极低（大部分 chunks 对本轮推理无用）
```

### 原则 3：工具结果要压缩，不要原样放进 context

```
工具原始输出可能很大：
  acli storage.list_volumes 可能返回 500 行
  
  ❌ 直接追加到 context：消耗大量 token，LLM 需要自行筛选关键信息
  ✅ 工具 wrapper 提取关键字段，只追加摘要版本：
     "storage_summary: volume-3(REMOUNTING, 73%), volume-1,2(ONLINE)"
     → 这 30 个 token 承载了 500 行输出的诊断价值
```

### 原则 4：LLM 主权（LLM Sovereignty）

```
平台的职责：
  ✅ 提供工具（供 LLM 调用）
  ✅ 执行工具（LLM 请求时）
  ✅ 管理 context window（token 预算、压缩、归档）
  ❌ 替 LLM 决定注入什么知识

LLM 的职责：
  ✅ 自主推理（下一步做什么）
  ✅ 自主决定 recall 什么历史经验
  ✅ 自主决定调用哪个工具
  ❌ 被动接受平台强制注入的知识块
```

### 原则 5：失败要优雅降级，不要崩溃

```
工具调用失败 → 追加错误信息到 context → LLM 根据错误信息调整策略
Context 接近上限 → 压缩旧的 Working Memory → 保留 Core Memory 不动
LLM 输出无法解析 → JSON 修复 → 重试 → 降级到文本提取
所有工具失败 → 告知用户需要哪些信息，转为对话式采集
```

---

## 七、「上下文管理」是核心，还是全部？

### 7.1 上下文 = prompt 吗？

```
prompt（狭义） = 用户输入的那段文字
context window = System Prompt + 工具定义 + 对话历史 + 工具结果 + 用户消息

context window ⊋ prompt（context 包含 prompt，但 prompt 只是其中一块）

广义上可以划等号——整个 context window 就是 LLM 接收的「超级 prompt」。
「Prompt Engineering」这个词在业界早已延伸为「设计整个 context 的艺术」。
```

### 7.2 「上下文管理」是核心，还是全部？

**是全部。** 精确论证：

```
LLM 是一个固定的函数 f(context) → output

对本项目（GLM-4 已选定，不可换模型）：
  模型权重固定（f 不变）
  唯一的优化变量 = context（输入）

所以：
  提升诊断准确率    = 优化 context 质量
  减少无效对话轮次  = 减少 context 噪音
  跨工单积累经验    = 让历史信息能高质量进入 context
  工具调用          = 用真实环境数据充实 context
  记忆系统          = 让 context 不因会话结束而遗忘
  RAG               = 让 context 在需要时获取外部知识

所有的工作都在回答同一个问题：
「什么信息、以什么格式、放在什么位置、在什么时机，进入 context window？」
```

一个微小的补丁（不影响上述结论，仅完整性补充）：

```
LLM 的参数化知识（训练权重）不在 context window 中，但永远有效。
对本项目，这个变量完全不受控制。

实践结论：
  本项目工程的核心 = 上下文管理，且上下文管理就是全部。
```

**这个框架的工程价值**——可以用它评估任意一个技术决策：

> 「这个改动是否让进入 context window 的信息更全面、更准确、密度更高？」  
> 是 → 值得做。否 → 不值得做。

---

## 八、直接回答"你的理解是否正确"

你的理解完全正确，并且是业界最重要的第一性原理。这里用最简洁的语言做最终校准：

```
你说的（高度概括版）：
  "Agent = 利用 Skill/Memory/RAG/Tool/MCP 来丰富 prompt，再喂给 LLM"

精确版（补上两个细节）：
  "Agent = 
    ① 通过 Skill/Memory/RAG/Tool/MCP 将外部信息转化为 context window 中的 token，
       [同时] LLM 通过训练所得的参数化知识无形地参与每次推理（不占 context）
    ② LLM 基于 context window 推理，主动决定下一步调用哪个工具
    ③ 工具结果再次写入 context window
    循环 ②③，直到任务完成"

两个补丁：
  补丁1：参数化知识（LLM 权重）不在 context window 里，但永远有效
  补丁2：在 ReAct/工具调用模式下，LLM 不仅是"接受 prompt 的被动函数"，
          还是"主动决定扩展自己的 context window 的主导者"
```

这个洞察的实践意义：**任何 Agent 的优化，本质上都是优化"什么信息、以什么方式、在什么时机进入 context window"**。

---

## 参考资料

- **ReAct 论文**: Yao et al., *ReAct: Synergizing Reasoning and Acting in Language Models*, Princeton/Google (2022)
- **Toolformer**: Schick et al., Meta AI (2023)
- **Agent 综述**: Wang et al., *A Survey on Large Language Model based Autonomous Agents* (2023)
- **MemGPT**: Packer et al., Stanford (2023)
- **MCP 协议**: Anthropic (2024), https://modelcontextprotocol.io
- **相关文档**:
  - [16_Claude式工作架构参考.md](16_Claude式工作架构参考.md)
  - [19_Context_Window深度讲解.md](19_Context_Window深度讲解.md)
