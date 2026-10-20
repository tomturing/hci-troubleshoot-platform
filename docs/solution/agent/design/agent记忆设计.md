# Agent 记忆设计

> 参考来源：
> - *Cognitive Architectures for Language Agents*（CoALA，Sumers et al., 2023）
> - *LLM Powered Autonomous Agents*（Lilian Weng，OpenAI Blog，2023.06）
> - *Voyager: An Open-Ended Embodied Agent with Large Language Models*（Wang et al., 2023）
> - *MemGPT: Towards LLMs as Operating Systems*（Packer et al., 2023）
> - LangChain Memory 模块命名体系

---

## 一、业界主流分类范式

### 1.1 按存储介质分类

以**物理存储位置**为分类轴，关注数据存在哪里。

| 类型 | 描述 | 典型实现 |
|------|------|---------|
| **In-Context Memory** | 当前上下文窗口内的信息 | Prompt / System message |
| **External Memory** | 上下文外的持久化存储 | 向量数据库、KV 存储、SQL |
| **In-Weights Memory** | 训练时固化到模型权重的知识 | Fine-tuning、RLHF |
| **In-Cache Memory** | KV Cache 缓存的中间状态 | vLLM prefix caching |

### 1.2 Lilian Weng 综合框架

> 来源：*LLM Powered Autonomous Agents*（Lilian Weng，2023.06）+ LangChain 命名体系 + Voyager 技能库

以**功能角色与生命周期**为分类轴，是目前工程实践引用最广的综合范式。

#### 感知记忆（Sensory Memory）

Agent 在单次交互中感知到的原始信息流，推理完成后释放，不持久化。

| 子类型 | 含义 |
|-------|------|
| **空间感知上下文** | 当前环境快照（SSH 连接状态、节点 IP、终端路径） |
| **瞬时交互缓存** | 工具返回的原始未裁剪数据流，提取目标值后即丢弃 |

#### 工作记忆（Working Memory）

Agent 在当前任务/会话中持续追踪上下文的能力。核心挑战是在有限上下文窗口内保留最有价值的信息。

业界三种标准管理策略（注意：这是实现机制，不是记忆类型）：

| 策略 | LangChain 类名 | 机制 | 优缺点 |
|------|--------------|------|--------|
| **滚动窗口** | `ConversationBufferWindowMemory` | 只保留最近 N 轮，超出自动丢弃 | 成本低；容易断片，忘记任务初期关键信息 |
| **摘要汇总** | `ConversationSummaryMemory` | 每隔几轮 LLM 异步压缩成进度概要 | 节省 Token；丢失技术细节（报错、IP 等） |
| **混合动态** | `ConversationSummaryBufferMemory` | 最近 3-5 轮保留原文，更早历史压缩为摘要 | 业界最常用，兼顾细节与成本 |

#### 长期记忆（Long-term Memory）

跨会话、跨时间保留的知识与经验，底层依赖向量数据库或图数据库。

| 子类型 | 含义 | 典型场景 |
|-------|------|---------|
| **语义记忆** | 静态或动态更新的事实与专业知识库（RAG 架构） | SOP 文档库、运维手册、架构规范 |
| **情节记忆** | Agent 过去执行任务的完整经验轨迹（输入→推理→工具→结果） | 历史相似故障的成功修复案例，供未来检索复用 |

#### 程序化记忆 / 技能库（Procedural Memory）

> 来源：Voyager（Wang et al., 2023）

Agent 在长期运行中自主为自己编写并沉淀的可复用工具函数。发现某段操作序列频繁被使用时，自动固化为独立脚本存入技能库，下次直接调用，无需重新推理。

### 1.3 CoALA 认知架构框架

> 来源：Sumers et al., 2023

以**认知科学类比**为分类轴，是学术界引用最多的系统性框架。

#### 四类记忆存储

```

+--------------------------------------------------+
|                  Agent Memory                    |
+------------+------------+------------------------+
|  Working   |  Episodic  |  Semantic              |
|  Memory    |  Memory    |  Memory                |
|            |            |                        |
| 当前上下文窗口| 过去经历/轨迹 | 结构化知识/事实       |
| (in-context)| (episodes) | (documents/KB)        |
+------------+------------+------------------------+
|              Procedural Memory                   |
|    技能、工具使用方式、行为模式（in-weights/prompts）|
+--------------------------------------------------+

```

四类记忆：Working Memory（当前上下文窗口）、Episodic Memory（过去经历/轨迹）、Semantic Memory（结构化知识/事实）、Procedural Memory（技能、工具使用方式、行为模式）。

| 记忆类型 | 存储位置 | 写入时机 | 读取方式 | 典型场景 |
|---------|---------|---------|---------|---------|
| **Working** | Context window | 实时填充 | 直接读 | 当前对话、任务状态 |
| **Episodic** | 外部 DB（结构化） | 任务完成后 | 检索 / 时序查询 | 历史操作记录、ReAct 轨迹 |
| **Semantic** | 向量库 / KV | 知识入库时 | 相似度检索（RAG） | 文档、FAQ、规程 |
| **Procedural** | 权重 / System prompt | 训练或提示工程 | 隐式激活 | 工具调用格式、推理模板 |

#### 三类动作空间

三类动作空间：Memory Actions（读写四类记忆）、Reasoning Actions（内部推理，CoT/反思/规划）、Grounding Actions（与外部世界交互，包括 Storage I/O、Process、UI、Service）。

#### 核心决策循环

```
Observe → [Retrieve from Memory] → Reason/Plan → Act → [Write to Memory] → loop
```

1. **Retrieve**：从 Episodic + Semantic 拉取相关上下文到 Working Memory
2. **Reason**：在 Working Memory 内推理 / 规划（Procedural 隐式参与）
3. **Act**：执行 Grounding Action
4. **Learn**：将本轮经历写回 Episodic Memory，必要时更新 Semantic

### 1.4 时间维度三层模型（工程视角）

以**时间稳定性（Time Stability）**为分类轴，面向系统设计与数据库建模。

```

+-------------------------------------------------------------+
|  Layer 1：持久知识（Enduring Knowledge）                     |
|  跨所有任务实例通用，版本管理，只读                             |
|  SOP 文档、KBD、工具定义、variable_schema、模型权重            |
+-------------------------------------------------------------+
|  Layer 2：执行状态（Episode State）                          |
|  绑定到一次任务执行，动态写入，任务结束后归档                    |
|  context_variables、current_node_id、execution_log          |
+-------------------------------------------------------------+
|  Layer 3：轮次上下文（Turn Context）                         |
|  活跃于当前 LLM 单次推理，推理结束即消亡                        |
|  渲染后的 system_prompt 窗口、当前 tool_call 参数             |
+-------------------------------------------------------------+

```

**Layer 1：持久知识（Enduring Knowledge）**
跨所有任务实例通用，版本管理，只读。SOP 文档、KBD、工具定义、variable_schema、模型权重。

**Layer 2：执行状态（Episode State）**
绑定到一次任务执行，动态写入，任务结束后归档。context_variables、current_node_id、execution_log。

**Layer 3：轮次上下文（Turn Context）**
活跃于当前 LLM 单次推理，推理结束即消亡。渲染后的 system_prompt 窗口、当前 tool_call 参数。

| 维度 | 持久知识 | 执行状态 | 轮次上下文 |
|------|---------|---------|-----------|
| **存储介质** | PostgreSQL（发布表）/ 模型权重 | PostgreSQL（`sop_execution`） | Context window |
| **写入时机** | 人工审核发布 / 模型训练 | 任务执行期间动态写入 | 每轮 LLM 调用前构建 |
| **生命周期** | 永久（版本化管理） | 任务开始 → 任务结束（归档） | 单次 LLM 推理调用 |
| **跨实例共享** | ✅ 所有任务实例共用 | ❌ 一个实例独占 | ❌ 一次调用独占 |

---

## 二、各范式横向对比

| 维度 | 按存储介质 | Lilian Weng 综合框架 | CoALA | 时间维度三层模型 |
|------|----------|---------------------|-------|----------------|
| **分类轴** | 物理存储位置 | 功能角色 + 生命周期 | 认知科学类比 | 时间稳定性 |
| **主要用途** | 基础架构选型 | 解释 Agent 能力、选 LangChain 组件 | 学术分析、认知科学研究 | 系统设计、数据库建模 |
| **工作记忆细化** | 无 | ✅ 三种管理策略可直接选型 | 只有一个类型 | 不关注管理策略 |
| **类型与实现分离** | ✅ | 策略被混入类型定义 | ✅ 严格 | ✅ 严格 |
| **变量池归属** | External Memory（仅按位置） | ✅ 工作记忆（外化） | 因物理位置错归 External Memory | ✅ Layer 2 执行状态 |
| **工程落地指导** | 一般 | ✅ 直接对应框架组件 | 抽象，需转译 | ✅ 直接指导建表和存储选型 |

**使用建议**：
- **对外沟通**（产品、客户、团队内解释）→ Lilian Weng 框架，直觉友好
- **内部系统设计**（建表、中断恢复、存储选型）→ 时间维度三层模型，边界清晰
- **学术对齐 / 与论文对话** → CoALA

---

## 三、变量池的设计理念

### 3.1 什么是变量池

SOP 中的命令不是静态文本，而是含有占位符的模板：

```
acli vm start {vm_name}
acli network nic up {nic_name} --node-ip={node_ip}
acli service {service_name} restart --confirm
```

**变量池（`sop_execution.context_variables`）** 是在一次 SOP 执行过程中，持续收集并存储这些占位符对应实际值的运行时状态：

```json
{
  "vm_name":  {"value": "prod-vm-001", "source": "tool:get_vm_list",   "resolved_at": "..."},
  "node_ip":  {"value": "10.0.1.5",   "source": "env:ssh_context",    "resolved_at": "..."},
  "nic_name": {"value": "bond0",       "source": "tool:acli_nic_list", "resolved_at": "..."}
}
```

### 3.2 为什么需要变量池

**问题一：变量的生命周期横跨整个 SOP 执行过程**

第 1 步获取 `node_ip = "10.0.1.5"`，第 2 步确认 `vm_name = "prod-vm-001"`，第 6 步需要 `vm_name`，第 8 步需要 `node_ip`——如果不持久化，一旦会话中断、页面刷新或上下文被截断，变量值就会丢失，后续命令无法安全执行。

**问题二：依赖 LLM 记住变量值是不可靠的**

| 方式 | 可靠性 |
|------|--------|
| 依赖 LLM 从历史消息中"记住"变量值 | ❌ 上下文过长时会遗忘或混淆 |
| 服务端显式管理变量池，注入时预渲染 | ✅ 确定性，可校验，LLM 只看渲染后的具体值 |

### 3.3 核心设计原则

**原则一：Schema 与值两层分离**

| 层次 | 存储 | 内容 | 类比 |
|------|------|------|------|
| Schema（模板层） | `sop_document.variable_schema` | 变量名、类型、获取策略 | Terraform `variable {}` 声明 |
| 值（执行层） | `sop_execution.context_variables` | 运行时实际填充的值 | `terraform.tfvars` |

**原则二：懒加载（JIT）获取**

不在 SOP 开始时批量获取所有变量，而是在进入需要该变量的节点时即时获取：
- SOP 是有分支的树，未执行的分支不触发变量获取
- 变量间存在依赖关系，JIT 天然满足
- `user_input` 类变量只在真正需要时打扰用户

`env_context` 类变量（节点 IP、连接信息）除外，初始化时批量注入，成本接近零。

**原则三：服务端预渲染，不依赖 LLM 解析**

命令模板的占位符替换在服务端窗口注入时完成，LLM 看到的始终是已渲染的具体值：
- 已知变量 → `"acli vm start prod-vm-001"`（确定性）
- 未知变量 → `"acli vm start {vm_name}  ← 需先通过 get_vm_list 获取"`（明确提示）

---

## 四、变量池合适的范式

### 4.1 在各范式下的归属分析

| 框架 | 变量池归类 | 问题 |
|------|----------|------|
| **按存储介质** | External Memory | 只描述了物理位置，没有区分"跨任务知识"和"任务内状态" |
| **CoALA** | Working / Episodic / Semantic 三者摇摆 | 将 Working Memory 严格定义为 in-context，导致变量池找不到干净归属 |
| **Lilian Weng** | ✅ 工作记忆（外化形式） | 无问题，见 §4.3 |
| **时间维度三层** | ✅ Layer 2 执行状态 | 无问题，精确且无歧义 |

### 4.2 CoALA 框架的归类缺陷

CoALA 将 Working Memory **严格定义为 in-context（上下文窗口内）**，导致变量池（物理位置 = PostgreSQL）被迫归入 External Memory，再在 Episodic 和 Semantic 之间摇摆。

根本原因：CoALA 用**物理存储位置**定义工作记忆，而变量池的**功能角色**是工作记忆——两个维度得出不同结论，框架缺乏消歧机制。

### 4.3 正确归属：工作记忆（外化形式）

按 Lilian Weng 框架以**功能角色与生命周期**为判断依据：

| 判断维度 | 工作记忆特征 | 长期记忆特征 | 变量池实际行为 | 归属 |
|---------|------------|------------|-------------|------|
| 生命周期 | 当前任务/会话 | 跨任务永久保留 | 绑定 `conversation_id`，会话结束归档 | ✅ 工作记忆 |
| 跨实例共享 | 专属当前任务 | 对所有任务可用 | 每次 SOP 执行独立一份 | ✅ 工作记忆 |
| 内容性质 | 当前任务的活跃状态 | 可复用的知识/经验 | `vm_name=prod-vm-001` 是本次故障专属 | ✅ 工作记忆 |

长期记忆的对照：`sop_document`（SOP 文档库）对所有任务适用，发布后持久有效——变量池与之完全相反。

### 4.4 外化工作记忆（MemGPT 概念）

MemGPT（Packer et al., 2023）提出"外化工作记忆"的动机与本项目设计完全对应：

```
传统工作记忆（CoALA）:    [context window] ← 大小受限，断连即失

外化工作记忆（本项目）:   [PostgreSQL] ──渲染──→ [context window]
                                   ↑
                         突破容量限制 + 支持中断恢复
                         语义仍属工作记忆：任务范围内活跃
```

外化不改变记忆的语义分类，只是突破了物理存储的限制。

---

## 五、在 Lilian Weng 框架下的具体应用

### 5.1 sop_execution 各字段的记忆角色

| 字段 | 记忆角色 | 说明 |
|------|---------|------|
| `context_variables` | 工作记忆（外化） | 动态积累的任务活跃状态 |
| `execution_log` | 工作记忆流水账 | 推进历史，支持中断恢复；任务归档后可升级为情节记忆 |
| `current_node_id` | 工作记忆（外化） | 当前任务位置 |
| `completed_steps` | 工作记忆（外化） | 防止恢复后重复执行写操作 |
| `pending_variable_name` | 工作记忆（外化） | 当前阻塞的用户输入等待 |
| `variable_schema` | 程序记忆（来自 `sop_document`） | 跨实例通用的获取方法 |

变量池中每个条目的元数据结构：

```json
{
  "vm_name": {
    "value":            "prod-vm-001",
    "source":           "tool:get_vm_list",
    "resolved_at":      "2026-05-27T10:30:00Z",
    "resolved_by_tool": "get_vm_list"
  }
}
```

### 5.2 工作记忆的外化→激活态流转

每轮 LLM 推理前，外化工作记忆被渲染投影到上下文窗口，形成"激活态工作记忆"：

```
外化工作记忆（PostgreSQL）
  context_variables + current_node 内容
              │
              ▼  模板渲染（服务端预渲染）
激活态工作记忆（Context Window）
  system_prompt 中已渲染的命令模板 + 已知变量值
              │
              ▼  LLM 推理
工具调用 / sop_advance（含 variables_extracted）
              │
              ▼  写回
外化工作记忆更新（PostgreSQL）
  新变量值写入 context_variables
  节点事件追加 execution_log
```

### 5.3 工作记忆管理策略：结构化滑动窗口

对应 Lilian Weng 框架的三种工作记忆管理策略，本项目采用**混合动态策略的变体**：

| 传统策略 | 本项目实现 | 优化点 |
|---------|----------|--------|
| 滚动窗口 | 每轮替换当前 SOP 节点内容（窗口替换，不累积） | 窗口按树结构滑动而非按时间滑动 |
| 混合动态 | 最近工具调用结果保留原文 + 历史节点内容被替换 | 历史节点被窗口替换，而非 LLM 摘要压缩 |
| 摘要汇总 | 中断恢复时由 `execution_log` 构建恢复摘要 | 只在恢复场景使用，正常执行不触发 |

**结果**：context 大小全程可控，与 SOP 树的节点总量无关。

### 5.4 中断恢复机制

工作记忆的外化设计使中断恢复成为可能：

```

触发：用户关闭页面后重新打开同一工单

1. 检测到 sop_execution.status = 'active'
2. 从外化工作记忆重建激活态：
   +---------------------------------------------------------+
   | [身份+方法论]（固定段）                                  |
   | [恢复说明]                                               |
   |   "正在执行 SOP：《VM 启动失败排障》                      |
   |    已完成步骤 3 步，当前位置：存储 I/O 故障 → 磁盘检查     |
   |    已知变量：vm_name=prod-vm-001, disk_id=disk-004"      |
   | [当前节点窗口]（current_node_id 对应节点 + 子节点）        |
   +---------------------------------------------------------+
3. completed_steps 防止已执行的写操作节点被重复触发

```

### 5.5 在完整记忆层次中的位置

```
感知记忆（单轮，不持久化）
  ← SSH 环境上下文、工具原始 JSON 输出

工作记忆（外化层，PostgreSQL，任务范围）
  ← context_variables / execution_log / current_node_id

工作记忆（激活态，Context window，单轮）
  ← 渲染后的 system_prompt 窗口

长期语义记忆（sop_document / kb-service，跨任务）
  ← SOP 内容在进入节点时注入激活态

程序记忆（variable_schema，跨任务只读）
  ← 指导 JIT 获取策略
```

---

## 六、相关参考

- **CoALA 论文**：Sumers et al., *Cognitive Architectures for Language Agents*, 2023
- **Lilian Weng 博客**：*LLM Powered Autonomous Agents*, 2023.06
- **MemGPT**：Packer et al., *MemGPT: Towards LLMs as Operating Systems*, 2023
- **Voyager**：Wang et al., *An Open-Ended Embodied Agent with LLMs*, 2023
- **LangChain Memory**：ConversationBuffer / Summary / VectorStore / EntityMemory
- **本项目实现**：[agent设计.md §12.7](agent设计.md) — 结构化滑动窗口与变量池详细设计

---

## 七、架构设计决策与技术答疑 (ADR)

### 7.1 Q1: `prompt_audit.py` 为什么放在 `services/` 目录，而不是底层 `observability/` 或 `memory/`？
* **与 `memory/` 的边界隔离（读写语义与单一职责原则 SRP）**：
  * **原则与范式**：**单一职责原则（SRP）**要求一个模块仅有一个引起它变化的原因。**在线工作记忆（Working Memory）** 范式规定，memory 是推理决策的主动输入端，其状态修改会即时回馈并重新拼装 prompt（例如从变量池中提取变量替换模板命令），直接决定 LLM 推理控制流的流向。
  * **设计决策**：相比之下，`prompt_audit` 属于**旁路可观测性（Bypass Observability）**范式，是纯粹的非功能性数据沉淀。它是**只写不读**的，数据一旦落地即成历史，绝对不会被重新调入 LLM 参与交互决策。它的写入被设计为**异步非阻塞、可容错的 Fire-and-Forget 模式**——即使写库发生瞬时异常，也必须在 Service 内部完成安全吞吐与隔离，绝对不能让“旁路监控”的报错阻断核心排障流转。
* **与底层 `observability/` 的边界隔离（依赖倒置原则 DIP）**：
  * **原则与范式**：**依赖倒置原则（DIP）**要求高层模块不应该依赖低层模块，两者都应该依赖抽象；基础设施层（如 Trace、Metrics、Logger）应当是**绝对无状态、无具体业务感知的公共 Utility 泛式库**。
  * **设计决策**：DIP 原则禁止底层通用库反向依赖高层的强业务实体。由于 `PromptAuditService` 在执行日志写入时，必须导入具体的 `AuditLog` ORM 模型、知晓数据库 Session 事务，且需要业务化地解析 `assistant_type`、动态推断 `has_sop`（是否包含 SOP 树引用）等強业务属性，因此它是一个标准的**业务应用层服务（Application Service）**，而不属于纯粹的基础设施 Utility。将其放置在 `services/` 目录下，保证了清晰优雅的依赖流向（Controller -> Application Service -> Database ORM / Infrastructure Utility），彻底避免了底层循环依赖引发的工程灾难。


### 7.2 Q2: 现在的 `message` 表是如何处理的？是只作记录，还是组装给 LLM 作上下文？
* **消息流转定位**：
  * `message` 物理表扮演**“外化工作记忆（Externalized Working Memory）”**的角色，既防范刷新页面或连接断开，又作为原始轨迹数据沉淀。
  * **但它不再是粗暴地 100% 全量塞给 LLM**。在进入大模型前，系统会实施**“结构化滑动窗口与变量池替换”**：
    * 仅有最近 3-5 轮的用户消息及最新工具结果保留原始文本细节，用作瞬时环境感知（Sensory/Working Memory）。
    * 更早的历史节点对话会被剔除庞大的工具 raw JSON，且其中已被提取的实体和状态变量（如 vm_name）在 system_prompt 中由 `context_variables` 服务端物理预渲染直接注入。
    * 这既对抗了 Token 爆炸，又彻底杜绝了模型因上下文过长而“遗忘或遗漏”关键运维变量的隐患。

### 7.3 Q3: `OpenClawAssistant` 仍被频繁调用且保留，后续会一直不处理吗？
* **现状分析**：
  * 虽然在 HTP Agent 的 S1-S5 重构中引进了更高级的 `ReactEngine` 并宣布在该功能链上弃用了纯 SSE 文本流，但在 S0 triage（意图结构化解析）、think（诊断报告非流式生成）以及 `test_ai_client.py`、`test_remediation_agent.py` 等 10 多个测试类中，`AIAssistantRegistry` 注册的底层通讯实体仍是 `OpenClawAssistant`。它依然在底层负责处理 API 鉴权、网关切换和流瞬时重试。
* **重构演进路线图 (Consolidation Roadmap)**：
  * **当前共存阶段**：将其作为底层的通用 **LLM Gateway Adapter（模型网关适配器）** 保留。
  * **重构中立化计划 (v5.0)**：在后续的架构升级中，将其从底层的 HTP 业务概念中完全剥离，统一重构重命名为 `LLMGatewayClient` 或 `OpenAIChatClient`，使底层网关逻辑纯净化，彻底清偿“大模型网关层与应用层掺杂”的技术债务。

### 7.4 Q4: 什么是 context_variables 变量池，它的注入逻辑，以及它与 environment 表之间的关系是什么？
* **context_variables 的定义**：
  * 指的是 `sop_execution` 数据库表的 `context_variables` 字段（JSONB 格式）。它是智能体排障过程中的**外化工作记忆（Working Memory）**，用于存储 SOP 执行或 ReAct 推理过程中收集的精炼变量值（如 `node_ip`、`vm_name`）。
* **注入与流转逻辑**：
  1. **意图分诊阶段（S0）**：`conversation-service` 通过 `EnvironmentClient` 从 `case-service` 获取 `case_id` 关联的环境快照数据（包括告警日志和任务日志），这部分数据在调用 `agent-service` 时被作为 `env_context` 参数传入。
  2. **执行创建与初始化（S1+）**：当命中特定 SOP 时，`agent-service` 调用 `conversation-sop-client` 在数据库中创建 `sop_execution` 记录。虽然最初 `context_variables` 初始化为空字典 `{}`，但后续 `ReactEngine` 与 JIT 变量池引擎在执行过程中，会动态从 `env_context` 提取特定的变量，或是执行 acli/SCP 工具（对应 `tool_call` 策略），并调用推进/提交接口将变量值写回 `context_variables`。
* **与 environment 表的关系**：
  * **environment 表（原始感知层）**：由 `case-service` 维护，针对工单 (`case_id`) 存储大量、高频的底层原始环境数据快照（如全量告警、任务列表，大段 JSONB）。
  * **context_variables 字段（认知工作记忆）**：由 `conversation-service` 维护，针对单次排障执行会话 (`conversation_id`)，仅存储为特定 SOP 变量提取的、高价值精炼键值对。
  * **二者关系**：`environment` 是底层低级数据输入源，系统在运行时通过 `env_context` 将其加载并经过感知提取（JIT Ingestion）精炼为单个具体的变量值，随后存储进 `sop_execution.context_variables` 中用于排障命令（如 `{node_ip}`）的占位符预替换。


### 7.5 Q5: 如何处理 `env_injection` 变量的关联性约束（如 `node_ip` 与 `disk_sn` 的关联关系）？
* **关联性约束的挑战**：
  * 通常一个工单在 `environment` 中会包含多条告警（`alert`）或任务（`task`）记录。如果单独、无序地解析它们，可能会产生将“主机 A 的 IP”与“主机 B 硬盘的 SN”错配关联的问题，从而破坏数据的有效性。
* **设计与管理策略**：
  * **日志锚定机制（Log Anchoring）**：在 SOP 创建初始化解析变量时，所有依赖同一类型（如 `alert` 或 `task`）的 `env_injection` 变量必须**从同一个日志条目（如首条活跃告警 `alert_logs[0]`）中进行关联提取**。
  * **具体解析方式**：
    * `hci_version`：直接从全局 `env_info` 中提取。
    * `node_ip`（映射自 `env:object_name`）：从 `alert_logs[0]` 的 `host` 字段提取。
    * `disk_sn`（映射自 `env:description`）：利用正则表达式（如匹配 `SN[：:= ]*([A-Za-z0-9_\-]+)`）从首条告警 `alert_logs[0]` 的 `description` 字段中提取出硬盘的唯一标识。
  * **容错与兜底（Fallback）**：若自动提取失败或数据缺失，JIT 引擎会在调用时记录警告，并将该变量的获取策略动态降级为 `user_input`，提示用户手动补充，确保排障流程不被阻塞。

### 7.6 Q6: 变量在多叉决策树中的“层级/局部”声明如何被解析与校验？
* **滑动窗口下的动态变量池**：
  * 在多叉决策树 SOP 执行中，系统通过滑动窗口动态加载当前节点及路径。因此未被加载的叶子节点变量无需在会话初始就强制获取，`context_variables` 是一个随着节点推进动态新增的过程。
* **发布（Approve）校验的对齐**：
  * **全局与局部表扫描**：SOP 文档不仅支持头部的全局 `## 变量声明` 表格，也支持节点内部的局部变量声明（例如 `##### 变量声明`）。在 SOP 发布阶段，后端解析器会扫描整篇 Markdown 里的所有变量声明表格，合并生成完整的 `variable_schema` 并入库。
  * **双向校验**：只要 SOP 文本中使用的任何变量（无论是根节点还是叶节点）能在任一声明表格中找到定义，即视为合法，防止因局部定义而导致发布阻断。

### 7.7 Q7: 为什么 Markdown 文本中的反斜杠（如 `disk\_dev`、`\## 变量声明`）会导致发布校验报错？如何解决的？
* **问题成因**：
  * **转义污染**：很多 Markdown 编辑器或 Git 会自动在特殊字符（如下划线 `_`、井号 `#`）前添加反斜杠进行转义，形成 `disk\_dev`、`${check\_meth}` 以及 `\## 变量声明`。
  * **正则匹配失效**：原本的正则表达式 `r"^(#{1,10})\s+(.+)$"` 匹配标题和 `r"(?<!\{)\{([a-z][a-z0-9_]*)\}(?!\})"` 匹配变量引用，均由于反斜杠的干扰而无法匹配。导致：
    1. 局部变量声明的标题 `\##### 变量声明` 无法识别，其中的 `disk_dev` 未被系统解析为已声明。
    2. 正文中的 `${check\_meth}`、`{disk\_dev}` 等变量无法被提取，导致系统认为没有使用这些变量，或者如果提取到了 `disk_dev`（因某些层去除了反斜杠），却在局部表格未被识别时报未声明错误。
* **解决与修复策略**：
  * **标题兼容正则**：将 `_parse_into_sections` 和 `_parse_variable_section` 中的标题匹配正则升级为 `r"^\\?#{1,10}\s+(.+)$"`，从而兼容带或不带反斜杠的标题行。
  * **变量容错提取**：将 `_extract_vars_from_text` 升级为支持可选 `$` 前缀及反斜杠的正则 `r"(?<!\{)\$?\{([a-z][a-z0-9_\\\\]*)\}(?!\})"`，并在匹配后将反斜杠替换清除（`.replace("\\", "")`），确保能正确解析和清洗出最终的变量名。

### 7.8 Q8: `sop_default` 策略的默认值在缺失列时如何降级提取？在运行时如何加载？
* **默认值自动提取（Fallback）**：
  * 当变量的来源（来源列 `source` 包含转义如 `sop\_default`）声明为 `sop_default` 时，若表格中没有定义“默认值”这一列，系统会自动将该变量的“说明”列（description）的内容作为默认值写入 `default_value` 字段。
* **JIT 运行时加载**：
  * 在 JIT 变量请求阶段，若变量的 `acquisition_strategy` 为 `sop_default`，引擎（`engine.py`）无需等待用户输入或工具调用，而是直接读取 `variable_schema` 中预存 of `default_value`，立即无 I/O 地返回其值，从而满足如配置判断方法、默认跳转动作的需求。

### 7.9 Q9: 如何在混合告警/任务流中精准路由并关联到当前 SOP 的故障数据？
* **核心挑战（未匹配的感知层噪点）**：
  * 在工单创建时，系统拉取的环境数据 `alert_logs` 和 `task_logs` 包含了当前环境下的全量事件流。如果直接采用首项（如 `alert_logs[0]`）进行变量提取，一旦第一条告警是无关的噪点（如“备份失败”、“登录失败”），而当前命中的 SOP 是“虚拟机开机失败”，系统就会提取到错误的故障上下文，导致诊断失效。
* **第一性原理与业界最佳实践（故障上下文过滤路由）**：
  * **关联依据**：在意图分诊阶段（S0），系统已经确认了故障分类（`category_l1`/`category_l2`/`category_id`），并且命中了具体的 SOP。SOP 标题和分类名称本身就是天然的“语义标签”。
  * **过滤路由机制**：在执行层（`sop_execution.py`）解析 `env_injection` 变量前，系统根据“当前会话的故障分类（L1/L2）”和“SOP 标题”动态推导出一组相关的**语义关键字（Filter Keywords）**（例如对于“磁盘寿命异常”，扩展出 `["disk", "磁盘", "硬盘", "sn", "smart", "寿命", "storage"]`；对于“虚拟机开机失败”，扩展出 `["vm", "虚拟机", "power", "boot", "start", "开机", "启动"]`）。
  * **多源路由**：使用这组语义关键字对 `alert_logs` 和 `task_logs` 进行预过滤，只保留包含任一关键字的相关日志条目。
  * **锚定与兜底**：过滤后，将变量解析锚定在**过滤后的首个相关条目**上。如果过滤后为空（未匹配到任何相关告警/任务），则退回并使用原始列表的首项，或者在 JIT 阶段降级为 `user_input` 确保整体鲁棒性。


