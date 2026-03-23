# Context Window 深度讲解

**版本**: v1.0  
**日期**: 2026-03-23  
**定位**: Context Window 的物理结构、容量管理与工程优化  
**核心命题**: Context Window 是 LLM 的唯一"工作内存"，理解它才能设计高效的 Agent  
**适读对象**: 所有参与本项目的工程师  
**前置文档**: [18_Agent技术原理深度讲解.md](18_Agent技术原理深度讲解.md)

---

## 一、Context Window 的物理定义

### 1.1 什么是 Context Window

```
Context Window = LLM 每次推理时能"看到"的所有 token 序列

  ┌─────────────────────────────────────────────────────────────┐
  │                     Context Window                          │
  │                                                             │
  │  token_1, token_2, token_3, ..., token_N                   │
  │                           |                                 │
  │                    ← N ≤ 最大窗口大小 →                    │
  └─────────────────────────────────────────────────────────────┘
                              ↓
              LLM: f(token_1...token_N) → token_N+1
                   （基于所有可见 token，预测下一个 token）
```

**关键性质**：
1. **有限性**：最大 token 数是硬上限（GLM-4: 128K，Claude 3.5: 200K，GPT-4o: 128K）
2. **扁平性**：context window 内部没有"优先级"之分，都是 token 序列
3. **无状态性**：两次推理之间，context window 不自动保留任何信息
4. **完全可见**：context window 内的所有 token，LLM 都能"看到"并参与推理

### 1.2 Token 是什么

```
Token ≠ 汉字，也 ≠ 英文单词，是分词器（Tokenizer）的输出单元

  英文近似：1 token ≈ 0.75 个单词
    "troubleshooting" → 1-3 tokens
    
  中文近似：1 token ≈ 1-1.5 个汉字
    "故障" → 1-2 tokens
    
  代码：密集，比自然语言消耗更多 tokens
    "acli storage.list_volumes --json | jq '.volumes[].status'" → ~20 tokens

实践参考（粗略估计）：
  1K tokens ≈ 750 英文单词 ≈ 500 中文字符 ≈ 50 行 Python 代码
```

---

## 二、Context Window 的解剖：真实案例分析

### 2.1 你的截图（144.3K/160K，90% 满载）

下图是本次对话的真实 context window 使用情况，这是一个极好的教学案例：

```
总量：144,300 / 160,000 tokens（90% 使用率）

┌─────────────────────────────────────────────────────────────────┐
│ 组成部分                    占比    tokens（估算）               │
├─────────────────────────────────────────────────────────────────┤
│ Reserved Output（输出预留）  37.0%   ~59,200 tokens              │
│ Files（注入文件）            23.5%   ~37,600 tokens              │
│ Tool Definitions（工具定义）  10.8%   ~17,280 tokens              │
│ System Instructions（系统）   8.1%   ~12,960 tokens              │
│ Messages（对话历史）           9.9%   ~15,840 tokens              │
│ Tool Results（工具结果）       0.9%    ~1,440 tokens              │
└─────────────────────────────────────────────────────────────────┘
```

**注意**：这是 **GitHub Copilot（基于 Claude Sonnet）** 的 context window，不是本项目 GLM-4 的。但作为理解 context window 组成的案例，它非常典型。

### 2.2 逐区解析

#### Reserved Output（输出预留，37%）

```
这不是已使用的内容，而是为 LLM 输出预留的 token 配额

  总 window: 160K
  输出预留:  59.2K（37%）
  实际可用于输入:  160K - 59.2K = 100.8K

为什么需要预留？
  LLM 的 token 总配额 = 输入 + 输出
  如果不预留，输入撑满了，输出就只剩极少空间，回答会被截断

不同模型的预留策略不同：
  本账单中 37% 是较高的预留比例
  GLM-4 API 一般：max_tokens（输出）独立设置，不影响输入窗口
  Claude API：context_window = 输入上限，max_tokens = 输出上限，两者分开计
```

#### Files（注入文件，23.5%）

```
这就是本次对话中我查看的所有文件内容（架构文档、代码文件等）
在你的截图场景里：你打开了大量架构文档给 Copilot 参考

对应本项目的场景：
  这部分 = RAG 检索结果注入 + 工单相关文件
  
  优化空间：
  ❌ 把整个文档塞入（当前状态）
  ✅ 只注入文档中与当前问题相关的段落（Chunk 级别）
  ✅ LLM 自主决定 recall 哪些内容（方案A的做法）
```

#### Tool Definitions（工具定义，10.8%）

```
这是 GitHub Copilot 注册的所有工具的 JSON Schema 定义
  包括：文件操作、终端执行、搜索工具等
  每个工具定义 ≈ 100-500 tokens

10.8% × 160K = ~17,280 tokens 用于"告诉 LLM 有哪些工具可用"

这 tokens 是"永久固定成本"——不管本轮是否用到这些工具，它们始终占据 context 空间

本项目优化方向：
  当前诊断阶段 S0 → 只注册信息收集工具（减少 Tool Definitions token 消耗）
  进入 S4 验证阶段 → 才注册高风险操作工具
```

#### System Instructions（系统指令，8.1%）

```
= System Prompt，LLM 行为的"宪法"

本次对话中：
  AGENTS.md / CLAUDE.md 的内容（项目规范、编码要求）
  GitHub Copilot 的基础指令（角色定义、安全规则）

对应本项目的场景：
  = conversation_service 的 _build_system_prompt() 生成的内容
  = 段落 1（身份）+ 段落 2（方法论）+ 段落 3（机制知识）

8.1% × 160K ≈ 12,960 tokens → 本项目实际约 2,000-4,000 tokens（更精简）
```

#### Messages（对话历史，9.9%）

```
= 用户和 AI 的对话轮次记录

本次会话有大量多轮对话，所以消耗了 ~15,840 tokens

关键特性：
  - 对话越长，这个区域越大
  - 当 context window 满载时，这个区域最先被压缩（FIFO）
  - 本项目的诊断对话通常 10-20 轮，约 5,000-10,000 tokens

注意：对话历史不包含工具调用和结果，工具有单独区域
```

#### Tool Results（工具结果，0.9%）

```
= 本次会话中工具调用的返回值

0.9% × 160K ≈ 1,440 tokens → 本次对话中工具调用不多

对应本项目的场景：当诊断工具密集调用时，这个区域会快速增长
  5 次 acli 调用 × 每次 ~500 tokens = 2,500 tokens 工具结果
```

---

## 三、Context Window 的工作机制

### 3.1 LLM 注意力是如何分布的（Attention）

```
Transformer 的注意力机制：每个 token 可以"注意到"上文中的任意 token

但注意力权重是有差异的：
  ┌───────────────────────────────────────────────────────┐
  │ 位置效应：最前面的 token（System Prompt）权重高         │
  │           最后面的 token（最新消息/工具结果）权重高     │
  │           中间部分（长对话历史）权重相对低              │
  │                                                       │
  │  高权重  System Prompt                                │
  │          ■■■■■■■■■■■■■■■■                            │
  │                                                       │
  │  中权重  对话历史（早期）                              │
  │          ■■■■■░░░░░░░░░░                            │
  │                                                       │
  │  高权重  最新工具结果 / 最新用户消息                    │
  │          ■■■■■■■■■■■■■■■■                            │
  └───────────────────────────────────────────────────────┘

这就是为什么设计 System Prompt 至关重要——它在"最高权重位"
```

### 3.2 "Lost in the Middle"问题

这是学术界已证实的 LLM 行为现象：**当 context window 很长时，中间部分的信息容易被忽略**。

```
实验设计（Stanford CS, 2023）：
  在 100K token 的 context 中嵌入一个关键事实
  测试 LLM 是否能找到这个事实

  结论：
  ┌────────────────────────────────────────────────────┐
  │ 位于 context 开头（前 5%）: 准确率 > 90%            │
  │ 位于 context 中间（40-60%）: 准确率 < 50-60%        │
  │ 位于 context 结尾（后 5%）: 准确率 > 85%            │
  └────────────────────────────────────────────────────┘
```

**工程含义**：

```
不要把重要信息"埋"在 context 中间

正确做法：
  ✅ 关键背景、核心规则 → System Prompt（最前）
  ✅ 当前最相关的工具结果 → 对话最后面（最新 = 最后 = 高权重）
  ✅ 需要 LLM 重点关注的内容 → 不要放在"已经积累了大量历史"的 context 中间

本项目含义：
  当诊断已进行到 S4 阶段，前面积累了大量工具调用历史
  S1 时的关键环境信息可能已经"沉没"到 context 中间，权重降低
  → 方案A的做法：将关键诊断状态更新到 Core Memory（System Prompt），
    确保它始终在高权重位置
```

### 3.3 KV Cache 与推理效率

```
KV Cache 是 LLM 推理的关键优化机制：

  第一次生成 token：
    计算整个 context window 的 Key-Value 矩阵 → 计算密集，延迟高
    
  后续每个 token：
    只计算新 token 的 KV + 查询已缓存的历史 KV → 延迟低

  工程含义：
  ✅ System Prompt（变化少）→ KV Cache 命中率高 → 后续推理快
  ❌ 每次都完全不同的 System Prompt → 缓存失效 → 每次都要全量计算
  
  对本项目：
  不同工单的 System Prompt 前半部分（身份/方法论/机制知识）保持不变
  → 这部分的 KV Cache 可以复用 → 推理延迟更低
```

---

## 四、Context Window 的容量管理策略

### 4.1 满载时会发生什么？

```
当 context window 超过最大 token 数：

  策略1（常见 API 行为）：报错，拒绝请求
    → 需要应用层自己处理

  策略2（部分 API）：静默截断 context 开头的 token
    → 危险！可能截断 System Prompt！
    → LLM 失去角色定义和约束规则 → 行为异常

  策略3（MemGPT 方案）：LLM 主动管理，档案化旧内存
    → 将旧的 Working Memory 归档到 Episodic Memory
    → 保留 Core Memory（System Prompt）不动
    → 这是本项目方案A的核心思想
```

### 4.2 压缩策略比较

| 策略 | 做法 | 优点 | 缺点 | 适用场景 |
|------|------|------|------|---------|
| **FIFO 截断** | 删除最早的对话轮次 | 实现简单 | 可能丢失关键早期信息 | 简单聊天机器人 |
| **摘要压缩** | LLM 将早期历史压缩为摘要 | 保留关键信息 | 需要额外 LLM 调用，有信息损失 | 长对话场景 |
| **MemGPT 档案化** | LLM 主动将信息写入外部存储 | LLM 控制，保留完整信息 | 实现复杂 | 需要跨会话记忆的 Agent |
| **层次化记忆** | Core 不动，Working 压缩 | 关键信息受保护 | 需要分层设计 | 本项目目标架构 |
| **动态工具注册** | 按阶段注册/注销工具定义 | 减少固定 token 成本 | 需要工具管理逻辑 | 工具密集型 Agent |

### 4.3 本项目的 Token 预算规划

**GLM-4 可用配置**（基于实际 API 参数）：

```
GLM-4 理论上下文：128K tokens
实际稳定使用建议：32K-64K tokens（太长推理质量下降）

如果按 32K 规划：

  System Prompt（Core Memory）:     4,000 tokens  (12.5%)
  Tool Definitions（当前阶段工具）:  3,000 tokens  ( 9.4%)
  当前工单基本信息:                  1,000 tokens  ( 3.1%)
  对话历史（最近 N 轮）:            10,000 tokens  (31.3%)
  工具调用结果（当前轮）:            8,000 tokens  (25.0%)
  Episodic Memory 召回（按需）:      4,000 tokens  (12.5%)
  输出预留:                          2,000 tokens  ( 6.2%)
  ─────────────────────────────────────────────────────
  合计:                             32,000 tokens  (100%)
```

**优化前后对比**（当前 vs 目标）：

```
当前（Phase 0 前）：
  System Prompt:         2,000 tokens
  RAG chunks（预注入）:  10,000 tokens  ← 利用率低（大部分无用）
  对话历史:               8,000 tokens
  工具调用:                   0 tokens  ← 没有工具调用
  合计有效信息密度：约 30-40%（大量 RAG 内容未被利用）

目标（Phase 3）：
  System Prompt:         4,000 tokens  ← 更丰富的方法论/机制知识
  对话历史:               6,000 tokens  ← 精简（状态提取）
  工具调用结果:           8,000 tokens  ← 真实诊断数据
  Episodic Memory:       4,000 tokens  ← 只召回真正相关的
  合计有效信息密度：约 70-80%（每个 token 都在发挥作用）
```

---

## 五、Context Window 的结构设计（本项目实践）

### 5.1 标准结构模板

```
┌─────────────────────────────────────────────────────────────┐
│ 1. SYSTEM PROMPT（Core Memory）                              │
│    ├── 身份定义（~200 tokens）                               │
│    ├── 工作准则（~300 tokens）                               │
│    ├── 诊断方法论（~500 tokens）                             │
│    ├── HCI 机制知识（~1,000 tokens）                         │
│    └── 当前工单状态摘要（~500 tokens，动态更新）              │
├─────────────────────────────────────────────────────────────┤
│ 2. TOOL DEFINITIONS（按阶段动态注册）                        │
│    ├── 当前阶段可用工具（S0: 信息类 ~1,000 tokens）          │
│    └── （S3+: 增加操作类工具 ~2,000 tokens）                 │
├─────────────────────────────────────────────────────────────┤
│ 3. CONVERSATION HISTORY（Working Memory）                    │
│    ├── 最近 10 轮对话（~5,000 tokens）                       │
│    └── 历史摘要（早于 10 轮，~1,000 tokens）                 │
├─────────────────────────────────────────────────────────────┤
│ 4. TOOL RESULTS（本轮观测）                                  │
│    ├── 诊断工具返回（摘要版）                                │
│    └── recall_memory 召回的历史案例                          │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 关于 System Prompt 位置的重要提示

```
经验规则：System Prompt 要"前置"且"稳定"

GLM-4 API 的 messages 格式：
  [
    {"role": "system", "content": "..."},   ← System Prompt（最前）
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."},
    {"role": "tool", "content": "..."},     ← Tool Result
    {"role": "user", "content": "..."},     ← 最新消息（接近末尾 = 高权重）
  ]

  ✅ 把不变的内容放在 role=system
  ✅ 把当前工单状态放在 role=system 的末尾（会动态更新）
  ❌ 不要把重要规则散落在对话历史中间（容易被遗忘）
```

### 5.3 动态工单状态块（关键设计）

这是方案 A（认知记忆）的核心工程实现，将"工作记忆中的关键信息"固化到"高权重位"：

```python
# 工单状态块示例（追加在 System Prompt 末尾，每轮更新）
CASE_STATE_TEMPLATE = """
## 当前诊断状态（每轮自动更新）

- 工单ID: {case_id}
- 诊断阶段: {stage} ({stage_desc})
- fault_domain: {fault_domain}
- 故障现象: {symptom_summary}

### 已确认环境信息
{confirmed_facts}

### 当前假设（按概率排序）
{hypotheses}

### 已排除的假设
{excluded_hypotheses}

### 下一步计划
{next_steps}
"""
```

**为什么要把状态放在 System Prompt 而不是对话历史？**

```
对话历史（靠近开头的部分）：
  S1 收集的环境信息（host 数、版本、存储配置）
  ↓
  经过 20 轮对话后，这些信息"沉没"到 context 中间
  ↓ "Lost in the Middle"效应
  ↓
  S5 阶段 LLM 忘记了 S1 收集的存储配置，做出错误判断

System Prompt 末尾的状态块（始终在高权重位）：
  S1 后，状态块更新 confirmed_facts: "存储版本 3.2.1, RAID-6 降级状态"
  S5 阶段 LLM 依然能"看到"这个关键事实（System Prompt 权重最高，始终可见）
```

---

## 六、不同模型的 Context Window 能力对比

| 模型 | 最大 Context | 实测稳定推理 | 特点 |
|------|-------------|------------|------|
| **GLM-4（本项目）** | 128K | 建议 32-64K | 中文能力强，超长稳定性待验证 |
| **GPT-4o** | 128K | 64-100K | 稳定性好，长 context 质量较高 |
| **Claude 3.5 Sonnet** | 200K | 100-180K | 最大窗口，长 context 最优 |
| **Gemini 1.5 Pro** | 1M | 500K+ | 实验性超长，实用性待验证 |
| **Qwen2.5-72B** | 131K | 64K | 中文强，可私有化部署 |

**本项目的影响**：
- GLM-4 在 128K 内理论可用，但建议控制在 32K 以内确保稳定
- 如果遇到"推理质量随对话轮次下降"的问题，**首先检查 context window 是否过载**
- 压缩策略需要在 conversation_service 层实现（不依赖 API 侧自动处理）

---

## 七、Context Window 满载的诊断与处理（本项目应急手册）

### 7.1 如何判断 context window 过载

```
症状1：LLM 开始"遗忘"使用早期收集的信息
  eg: S5 阶段忘记 S1 收集的存储版本号
  → 可能是"Lost in the Middle"效应，context 过长

症状2：API 返回 token 超限错误
  GLM-4: {"code": 1301, "message": "context_length_exceeded"}
  → 需要清理 context

症状3：LLM 输出被截断（回答突然中断）
  → max_tokens 设置过小，或 context 挤压了输出空间

症状4：推理延迟突然增大
  → context window 变大，注意力计算量 ∝ n²（n = token 数）
```

### 7.2 应急压缩流程

```python
async def compress_context_if_needed(messages: list, max_tokens: int = 28000):
    """
    当对话历史超过阈值时，自动压缩
    保留策略：System Prompt + 最近 N 轮 + 重要工具结果
    """
    current_tokens = count_tokens(messages)
    
    if current_tokens < max_tokens * 0.8:
        return messages  # 不需要压缩
    
    # 1. 识别哪些可以压缩（对话历史，不包括 system/最近5轮）
    system_msgs = [m for m in messages if m["role"] == "system"]
    recent_msgs = messages[-10:]  # 保留最近10条消息
    old_msgs = messages[len(system_msgs):-10]
    
    if not old_msgs:
        return messages  # 没有可压缩的内容
    
    # 2. 用 LLM 压缩旧对话历史
    summary = await llm.summarize(
        content=old_msgs,
        instruction="将以下对话历史压缩为关键诊断事实摘要，不超过500字"
    )
    
    # 3. 将摘要追加到 system prompt（MemGPT 风格）
    system_msgs[-1]["content"] += f"\n\n## 早期对话摘要\n{summary}"
    
    return system_msgs + recent_msgs
```

---

## 八、常见误区澄清

### 误区1："RAG 会让 LLM 记住更多东西"

```
错误：RAG 让 LLM 记住了知识库的内容
正确：RAG 是每次查询前临时注入相关内容，LLM 不会"记住"它
      下次查询如果不再检索这些内容，LLM 就"忘了"

类比：RAG 像是每次考试前给你一份参考资料，不是让你背会了
```

### 误区2："context window 越大越好"

```
错误：总是使用最大 context window
正确：更大的 context 意味着：
  1. 更高的推理延迟（注意力计算量 ∝ n²）
  2. 更高的 API 费用（按 token 计费）
  3. 可能更低的推理质量（"Lost in the Middle"）
  4. 更高的内存需求（KV Cache）

最佳实践：精炼 context 内容，用最少的 token 表达最多的有效信息
```

### 误区3："LLM 会平等对待所有 context 内容"

```
错误：LLM 对 context 中所有内容的重视程度相同
正确：
  - 位置影响权重（开头/结尾 > 中间）
  - 重复出现的信息权重更高
  - "重要"的信息如果放在中间，可能被忽视

工程后果：不能依赖"LLM 会找到"，要主动把关键信息放在高权重位
```

### 误区4："系统提示词不重要，只是个开场白"

```
错误：System Prompt 只是告诉 LLM 它是谁
正确：System Prompt 是整个 Agent 行为的"宪法"
  - 确定 LLM 的推理框架（按什么步骤思考）
  - 约束 LLM 的行为边界（不能做什么）
  - 注入领域知识（参数化知识不足时的补充）
  - 定义输出格式（如何结构化输出工具调用）

本项目：诊断准确率的 60% 取决于 System Prompt 的质量
```

### 误区5："工具调用不消耗 context 空间"

```
错误：工具是外部执行的，不占 context
正确：
  工具定义：占 context 的输入空间（固定成本）
  工具结果：占 context 的输入空间（每次调用后增加）
  工具调用请求（LLM 输出）：占 context 的输出空间

5 次诊断工具调用 = 5条工具调用记录 + 5条工具结果 ≈ 3,000-10,000 tokens 增量
```

---

## 九、总结：Context Window 是 Agent 的第一公民

```
               ╔═══════════════════════════════════════╗
               ║         Context Window                ║
               ║                                       ║
               ║  如果 LLM 是一个厨师，                ║
               ║  Context Window 就是他的操作台。      ║
               ║                                       ║
               ║  操作台上放了什么（内容），             ║
               ║  操作台上空间够不够（容量），           ║
               ║  东西放在操作台哪个位置（权重），       ║
               ║                                       ║
               ║  决定了这位厨师能做出什么菜。           ║
               ╚═══════════════════════════════════════╝

Agent 工程 = 把对的食材（信息），
             放在对的位置（高权重区），
             在对的时机（按需动态），
             用对的形式（压缩、结构化），
             放进厨师的操作台（Context Window）。
```

---

---

## 十、本项目上下文可观测性设计

> 上下文管理是本项目工程的全部核心。因此，对上下文的持续观测与优化，是整个项目能够迭代进化的基础条件。本章给出完整的分类体系和落地方案。

### 10.1 为什么「入库」不是最好的单一答案

「入库（PostgreSQL）」可以解决**持久化和统计分析**，但有两个内在问题：

```
问题1：实时性差
  fire-and-forget 写入，不能实时 tail
  开发调试时需要比较及时地看到每轮组装结果

问题2：全文内容膨胀 DB
  一次对话的完整 context 约 5,000~30,000 tokens
  折合文本 4,000~24,000 字符
  按 1,000 次/天，一张表每天增加 ~24MB 文本 payload
  需要专门的归档和清理策略
```

**本项目已有 Loki + Tempo + Grafana**，这是关键：这套工具天然适合解决「内容可视」和「过程追踪」，且 `StructuredLogger` 已自动附加 `trace_id`/`span_id`。

**三工具各司其职，通过 `trace_id` 关联**：

```
┌─────────────────────────────────────────────────────────────────────┐
│  目的                 │  工具             │  解决的问题               │
├─────────────────────────────────────────────────────────────────────┤
│  每个分段的完整内容    │  Loki（结构化日志）│ 实时可见、全文可搜索       │
│  Prompt 组装过程/时序  │  Tempo（OTel 链路）│ 每步时间线、耗时分布       │
│  统计分析 + 质量评分   │  PostgreSQL        │ 跨工单聚合、SOP命中率等    │
│  超大 payload 归档     │  MinIO（可选）     │ 不撑大 DB，trace_id 索引   │
└─────────────────────────────────────────────────────────────────────┘
          │                    │                    │
          └────────────────────┴────────────────────┘
                          通过 trace_id 关联
```

---

### 10.2 分类体系：本项目 Context Window 的完整分类

这是第一个核心问题的答案。按来源和生命周期分为 5 层：

```
┌──────────────────────────────────────────────────────────────┐
│               Context Window 完整分类体系                     │
├──────────┬───────┬──────────────────────────────────────────┤
│ 层       │ 编码  │ 内容                    │ 生命周期         │
├──────────┼───────┼──────────────────────────────────────────┤
│ A. 静态  │ A1    │ 身份/角色/行为规则        │ 所有会话固定     │
│ 核心     │ A2    │ 诊断方法论（S0-S6 步骤）  │ 所有会话固定     │
│ (Core)   │ A3    │ HCI 机制知识（Skill 层） │ 所有会话固定     │
├──────────┼───────┼──────────────────────────────────────────┤
│ B. 动态  │ B1    │ SOP 精确命中（0 或 1 条）│ 每条消息按 query │
│ 注入     │ B2    │ KB 语义检索（0~N chunks）│ 每条消息按 query │
│(Inject.) │ B3    │ 当前工单诊断状态          │ 每轮更新         │
├──────────┼───────┼──────────────────────────────────────────┤
│ C. 工具  │ C1    │ 工具 Schema 定义          │ 按诊断阶段注册   │
│ 层       │ C2    │ 工具执行结果（Observation）│ ReAct 每步追加  │
│(Tool)    │       │                           │ (Phase 3+)      │
├──────────┼───────┼──────────────────────────────────────────┤
│ D. 对话  │ D1    │ 历史消息（最近 N 轮）     │ 每轮截取追加     │
│ 层       │ D2    │ 本轮用户消息              │ 每条消息         │
│(Conv.)   │       │                           │                 │
├──────────┼───────┼──────────────────────────────────────────┤
│ E. 自生  │ E1    │ LLM 推理步骤（CoT/Thought）│ ReAct 每步自生 │
│ 成推理   │       │                           │ (Phase 3+)      │
│(Reason.) │       │                           │                 │
└──────────┴───────┴──────────────────────────────────────────┘

当前 Phase（Phase 0-2 前）存在的分类：A1, A2, A3, B1, B2, B3, D1, D2
Phase 3+ 后新增：C1, C2, E1
```

---

### 10.3 方案一：结构化日志 → Loki（解决「每个分类完整内容可视」）

**这是解决「内容可见」最直接的方案，且零额外基础设施成本。**

利用现有 `StructuredLogger`，在 `_build_system_prompt` 组装每个 segment 时立即记录：

```python
# 在 _build_system_prompt 中，每个 tier 组装完后插入日志

# A1~A3: 静态核心（首次启动时记录一次即可，或每次 DEBUG 级）
logger.debug(
    event="context_segment",
    segment="A_STATIC_CORE",
    tier_breakdown={"A1_role": len(seg_identity), "A2_method": len(seg_method), "A3_hci": len(seg_hci)},
    total_chars=len(static_core),
    case_id=case_id,
)

# B1: SOP 命中
logger.debug(
    event="context_segment",
    segment="B1_SOP_MATCH",
    hit=sop_node is not None,
    sop_title=sop_node.get("title") if sop_node else None,
    content=sop_content[:2000] if sop_node else None,   # 截取防止单行过大
    chars=len(sop_content) if sop_node else 0,
    case_id=case_id,
)

# B2: KB Chunks
for i, chunk in enumerate(kb_chunks):
    logger.debug(
        event="context_segment",
        segment="B2_KB_CHUNK",
        chunk_index=i,
        source=chunk.get("source_title"),
        score=chunk.get("score"),
        content=chunk.get("content", "")[:1000],        # 截取防止单行过大
        chars=len(chunk.get("content", "")),
        case_id=case_id,
    )

# B3: 工单状态
logger.debug(
    event="context_segment",
    segment="B3_CASE_STATE",
    content=case_state_block,
    chars=len(case_state_block),
    case_id=case_id,
)

# D1: 历史消息摘要
logger.debug(
    event="context_segment",
    segment="D1_HISTORY",
    message_count=len(selected_messages),
    total_chars=sum(len(m.content) for m in selected_messages),
    case_id=case_id,
)
```

**在 Grafana / Loki 中查看**：

```logql
# 查看某次对话完整的 context 组装过程（通过 trace_id 关联）
{service="conversation-service"} | json | event="context_segment" | trace_id="<trace_id>"

# 查看某工单所有对话的 KB 命中情况
{service="conversation-service"} | json | event="context_segment" | segment="B1_SOP_MATCH" | case_id="<case_id>"

# 找出所有 SOP 未命中的对话
{service="conversation-service"} | json | event="context_segment" | segment="B1_SOP_MATCH" | hit="false"
```

**与 Tempo 的关联**：因为 `StructuredLogger._format_log` 已自动从 OTel 上下文读取并注入 `trace_id`，所以在 Grafana Tempo 中点击任意一条 trace，可以直接跳转到 Loki 过滤出同一个 `trace_id` 的所有 context segment 日志。这是**零额外工作**实现的关联。

---

### 10.4 方案二：OTel Span → Tempo（解决「组装过程可追踪」）

用 OpenTelemetry span 追踪 Prompt 组装的整个过程，尤其是 Phase 3+ 的 ReAct 循环：

```python
from opentelemetry import trace

tracer = trace.get_tracer("conversation-service")

async def _build_context_window(self, query: str, case_id: str, step: int = 0):
    """带链路追踪的 context 组装"""

    with tracer.start_as_current_span("context_assembly") as span:
        span.set_attribute("case_id", case_id)
        span.set_attribute("react_step", step)

        # A: 静态核心（计量 token）
        with tracer.start_as_current_span("segment.static_core"):
            static_tokens = count_tokens(static_core)
            span.set_attribute("tokens.static_core", static_tokens)

        # B1: SOP 匹配
        with tracer.start_as_current_span("segment.sop_match") as sop_span:
            sop_node = await self.kb_client.sop_match(query)
            sop_span.set_attribute("hit", sop_node is not None)
            sop_span.set_attribute("tokens", count_tokens(sop_content) if sop_node else 0)

        # B2: KB 检索
        with tracer.start_as_current_span("segment.kb_chunks") as kb_span:
            kb_chunks = await self.kb_client.search(query)
            kb_span.set_attribute("chunk_count", len(kb_chunks))
            kb_span.set_attribute("tokens", sum(count_tokens(c["content"]) for c in kb_chunks))

        # 汇总：context window 填充率
        total_tokens = sum([static_tokens, sop_tokens, kb_tokens, history_tokens])
        fill_ratio = total_tokens / MAX_CONTEXT_TOKENS
        span.set_attribute("context.total_tokens", total_tokens)
        span.set_attribute("context.fill_ratio", round(fill_ratio, 3))
        span.set_attribute("context.max_tokens", MAX_CONTEXT_TOKENS)
```

**在 Grafana Tempo 中看到的效果**：

```
context_assembly（总耗时 120ms）
  ├── segment.static_core        tokens=2,100   耗时 0ms（无 IO）
  ├── segment.sop_match          tokens=850     耗时 45ms   hit=true
  ├── segment.kb_chunks          tokens=3,200   耗时 60ms   chunk_count=3
  ├── segment.case_state         tokens=420     耗时 0ms
  └── segment.history            tokens=5,800   耗时 15ms
                                 ─────────────
                                 total=12,370   fill_ratio=0.39
```

Phase 3+ 的 ReAct 循环每一步都是一条独立 trace，可以在 Tempo 中看到完整的「Thought → Action → Observation」时序图，以及每步 context window 的增长情况。

---

### 10.5 方案三：PostgreSQL（解决「持久化统计分析」）

`prompt_audit` 表的定位保持不变（质量评分数据来源），但需要**增加分类统计字段**，让它从「字符数统计」升级为「token 分类统计」：

```sql
-- 增强版 prompt_audit 表（在原表基础上增加字段）
ALTER TABLE prompt_audit ADD COLUMN IF NOT EXISTS
    context_breakdown JSONB;   -- 分层 token 统计

-- context_breakdown 的结构示例：
-- {
--   "total_tokens": 12370,
--   "fill_ratio": 0.39,
--   "max_tokens": 32000,
--   "segments": {
--     "A_static_core": {"tokens": 2100, "ratio": 0.17},
--     "B1_sop_match":  {"tokens": 850,  "ratio": 0.07, "hit": true},
--     "B2_kb_chunks":  {"tokens": 3200, "ratio": 0.26, "count": 3},
--     "B3_case_state": {"tokens": 420,  "ratio": 0.03},
--     "D1_history":    {"tokens": 5800, "ratio": 0.47, "turns": 8}
--   },
--   "loki_trace_ref": "<trace_id>"   ← 指向 Loki 中的完整内容日志
-- }
```

**关键设计**：`context_breakdown` 只存**统计摘要**（每条约 500 bytes），不存全文内容。完整内容通过 `loki_trace_ref`（即 `trace_id`）到 Loki 中查询。

**这样 PostgreSQL 负责回答**：

```sql
-- "哪些工单从未命中 SOP？"
SELECT case_id FROM prompt_audit
WHERE (context_breakdown->'segments'->'B1_sop_match'->>'hit')::boolean = false;

-- "平均 context 填充率的趋势"
SELECT date_trunc('day', created_at),
       avg((context_breakdown->>'fill_ratio')::float)
FROM prompt_audit GROUP BY 1 ORDER BY 1;

-- "KB chunks 为空时，用户评分是否显著更低？"
SELECT
    (context_breakdown->'segments'->'B2_kb_chunks'->>'count')::int = 0 AS no_kb,
    avg(user_rating)
FROM prompt_audit WHERE user_rating IS NOT NULL GROUP BY 1;
```

---

### 10.6 三方案协作：以「一次诊断对话」为例

```
时序：用户发送「VM 启动失败，存储刚扩容过」
                │
                ▼
        _build_context_window()
                │
    ┌───────────┴───────────────────────────────────┐
    │                 同时发生                        │
    │  OTel Span: context_assembly 开始              │
    └───────────────────────────────────────────────┘
                │
    B1: SOP 匹配 → hit=true（VM启动失败-存储类）
        └─ logger.debug(event="context_segment", segment="B1_SOP_MATCH", content="...", trace_id="abc123")
        └─ span.set_attribute("B1_sop.hit", True); span.set_attribute("B1_sop.tokens", 850)
                │
    B2: KB 检索 → 3 chunks 命中
        └─ logger.debug(event="context_segment", segment="B2_KB_CHUNK", chunk_index=0, ..., trace_id="abc123")  × 3
        └─ span.set_attribute("B2_kb.count", 3); span.set_attribute("B2_kb.tokens", 3200)
                │
    context 组装完成
        └─ span.set_attribute("context.fill_ratio", 0.39)
        └─ OTel Span 结束 → 写入 Tempo
        └─ asyncio.create_task(_write_prompt_audit(..., context_breakdown={...}, loki_trace_ref="abc123"))
                │
                ▼
        LLM 调用（GLM-4）
                │
                ▼
        流式输出返回用户

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
事后查询举例：

  问题：「这次对话 B2_KB_CHUNK 注入了什么内容？」
  →  到 Grafana Loki 搜索：event="context_segment" AND trace_id="abc123"
  →  看到所有 segment 的完整内容文本

  问题：「SOP 命中后 KB 检索耗时多少？」
  →  到 Grafana Tempo 找 trace_id="abc123"
  →  看到 segment.sop_match 45ms, segment.kb_chunks 60ms

  问题：「上周 SOP 命中率是多少？」
  →  SELECT avg((context_breakdown->'segments'->'B1_sop_match'->>'hit')::boolean::int)
       FROM prompt_audit WHERE created_at > now() - interval '7 days'
```

---

### 10.7 现状评估与实施优先级

```
现有 prompt_audit 设计定位：质量评分数据源
现有缺口：                  上下文内容可视化 + 分类统计

分类体系（10.2节）         → Phase 2 立即定义，不依赖 ReAct
Loki 内容记录（10.3节）    → Phase 2 实现，改动极小（在 _build_system_prompt 加 logger.debug）
PostgreSQL 增强（10.5节）  → Phase 2 实现，ALTER TABLE 增加 context_breakdown 字段
Tempo 过程追踪（10.4节）   → Phase 3 实现，配合 ReAct 循环一起加
```

**一句话总结**：

> 「入库」解决持久化，但不解决实时可见和内容全文检索。  
> 项目已有 Loki + Tempo，三者组合才是完整答案：  
> **Loki = 内容台账（全文可搜索）；Tempo = 过程仪表盘（时序可追踪）；PostgreSQL = 统计账本（聚合可分析）**，  
> 三者通过 `trace_id` 天然打通。

---

## 参考资料

- **"Lost in the Middle"**: Liu et al., Stanford (2023)
- **KV Cache**: 广泛应用于 vLLM, TGI 等推理框架
- **MemGPT 内存管理**: Packer et al., Stanford (2023)
- **相关文档**:
  - [18_Agent技术原理深度讲解.md](18_Agent技术原理深度讲解.md)
  - [13_知识工程方案A_认知记忆架构.md](13_知识工程方案A_认知记忆架构.md)
  - [16_Claude式工作架构参考.md](16_Claude式工作架构参考.md)
