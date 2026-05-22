# SOP 知识消费阶段设计确认

> **定位**：SOP 导入（知识生成）阶段与知识消费阶段（Pydantic AI ReAct Agent Loop）的设计对齐文档。
> 核心命题：**导入阶段的数据结构决定消费阶段能力的上限**，一旦大量 SOP 入库，改结构成本极高。
>
> 本文档逐项给出设计决策建议，需在编码前确认。

---

## 背景：消费阶段的 ReAct 完整循环

> **前置说明**：会话到达 ReAct 循环时，SOP 路由已由对话服务完成：
> S0（意图识别）→ `category_id` → S1（FK 查询）→ `sop_document_id` 已写入 `conversation` 表。
> Agent 不需要搜索 SOP，**直接从已确定的 SOP 根节点开始遍历**。

```
[S1 完成] conversation.sop_document_id = 42（已确定）
  │
  ▼ [会话前置] collect_session_prerequisites()
    → 向用户收集 entry.prerequisites（acli 权限、SSH 权限、故障日期）
    → 一次性，整个会话有效
  │
  ▼ [Think] 当前处于树的哪个分支？
  ▼ [Tool]  get_window(root_node_id) → 当前节点完整信息 + 子节点预览（滑动窗口）
  │
  ▼ [Think] 哪个子节点的 prerequisites 条件与当前情况匹配？
  ▼ [Tool]  collect_evidence(check) → 询问用户 / 执行 acli 命令
  │
  ▼ [Repeat 上面 2 步，直到到达叶节点]
  │
  ▼ [Think] 到达叶节点，执行诊断
  ▼ [Tool]  run_diagnosis_steps(node_id) → 执行 page_methods / acli_methods
  ▼ [Tool]  confirm_diagnosis() → 用户确认是否匹配
  │
  ▼ [Think] 诊断确认，输出处置方案
  ▼ [Tool]  get_solution(node_id, mode) → quick_recovery 或 thorough_fix
  │
  ▼ [Record] record_traversal(node_id, outcome)
```

---

## 一、`keywords` 字段的必要性（S0 阶段视角）

> ⚠️ **本节经过重新评估（2026-05-19）**
> 初版分析错误地将 sop-agent 离线流水线阶段（clean.py / build_tree.py 等）当作对话服务 S0，导致结论方向错误。以下为基于第一性原理和真实数据库结构的重新分析。

### 对话服务 S0 的真实含义

对话服务的 S0 是**意图识别阶段**，与 sop-agent 的知识生成流水线完全无关：

```
用户输入："虚拟机无法启动"
  │
  ▼ [S0 意图识别]
  ▼  LLM 注入 198 个 kb_category 分类列表
  ▼  LLM 输出 category_id = "虚拟机-003"
  │  写入 conversation.category_id
  │
  ▼ [S1 三轨路由：第1轨]
  ▼  SELECT * FROM sop_document
     WHERE category_id = '虚拟机-003'
     AND status = 'published'
```

**S0 完成后，已通过 `category_id` 精确关联到 `sop_document`，不需要关键词匹配。**

### 数据库真实结构（实测）

查询结果（`kubectl exec -n hci-dev postgres-0 -- psql -U hci_admin -d hci_troubleshoot -c "\d sop_document"`）：

```
sop_document 表字段：id, source_id, category_id, title, content_md,
                     docx_hash, status, reviewer_id, reviewed_at,
                     published_at, created_at, updated_at, review_note, hit_count

索引：idx_sop_document_category_published (category_id WHERE status='published')
```

**`sop_document` 表当前没有 `keywords` 字段。** 路由路径已由 `category_id` FK 完整覆盖。

### keywords 的实际价值边界

| 场景 | keywords 是否有价值 |
|------|-------------------|
| 一个 category_id 下只有一个 SOP（当前现状，已验证） | ❌ 无价值，category_id 已唯一确定 SOP |
| 一个 category_id 下有多个 SOP（未来可能） | ✅ 可做二级精选（但 sop_chunk 的语义搜索更优） |
| 绕过 S0 做全文搜索兜底 | ✅ 有价值，但应建在 sop_chunk.tsv 上（已有 GIN 索引） |

真正的检索基础设施在 **`sop_chunk` 层**（已实测存在）：
```
sop_chunk 字段：
  embedding  vector(1536)  → ivfflat 向量索引（语义搜索）
  tsv        tsvector      → GIN 全文索引（关键词检索）
```

### 结论

**✅ 结论：`sop_document` 层的 `keywords` 字段当前没有必要**。路由已由 S0(category_id) → S1(FK 查询) 闭环，无需关键词匹配。若将来需要二级精选或兜底检索，应使用已有的 `sop_chunk.embedding`（语义）或 `sop_chunk.tsv`（全文），而非在文档层新增 keywords 字段。

**D1 更新：取消 `keywords` 字段设计，复用 `sop_chunk` 已有的 embedding + tsv 检索基础设施。**

---

## 二、`prerequisites` 的两种语义 — 本质区别

### 两种方案

**方案 A — 路由条件（判断条件，声明式）**
```
prerequisites: ["CPU使用率 > 90%", "出现内存 OOM 日志", "VM 状态为 stopped"]
```
- 描述**世界的状态**（What is true）
- Agent 行为：先收集证据 → 再比对条件 → 布尔判断
- 本质：**谓词 / 过滤器**（Predicate / Filter）
- 类比：`WHERE cpu_usage > 90 AND has_oom_log = true`

**方案 B — 检查步骤（执行步骤，命令式）**
```
prerequisites: ["登录控制台查看资源面板", "执行 acli vm show {vm_id}"]
```
- 描述**要执行的行动序列**（What to do）
- Agent 行为：按序执行 → 收集输出 → 判断
- 本质：**行动序列**（Action Sequence）
- 类比：`DO A; DO B; CHECK result`

### 本质区别

| 维度 | 方案 A（判断条件） | 方案 B（检查步骤） |
|------|------------------|------------------|
| **知识表达** | 状态描述（声明式） | 行为描述（命令式） |
| **Agent 推理负担** | 需要收集证据后比对 | 按步骤执行，观察结果 |
| **可测试性** | 高（条件明确，可单测） | 低（依赖外部执行结果） |
| **SOP 文档来源** | 通常是「判断条件」章节 | 通常是「排查步骤」章节 |
| **在树中的位置** | 中间节点的选路逻辑 | 叶节点的验证执行逻辑 |

### 真实 SOP 数据验证（vmstart_node_sops.jsonl，实测）

真实 SOP 数据中存在**三层**不同语义的「先决条件」，与方案 A/B 都不完全重叠：

**层级 1：`entry.prerequisites`（会话启动前置）**
```json
"prerequisites": [
  "具备acli命令行工具访问权限",
  "具备SSH登录集群节点权限",
  "故障发生日期"
]
```
→ 本质：**环境/权限/信息准备**（启动排障会话前需收集的内容）
→ 与方案 A/B 都不符：既不是布尔路由条件，也不是 acli 命令序列

**层级 2：`flow[].branches[].when`（分支路由条件）**
```json
"branches": [
  {"when": "任务记录中存在错误码0x0CFFFFFF", "goto_branch_id": "branch-A"},
  {"when": "任务记录中包含'虚拟机未运行'", "goto_branch_id": "branch-B"}
]
```
→ 本质：**声明式路由判断**（方案 A）
→ 这才是 `SOPNode.prerequisites` 应该映射的真实数据

**层级 3：`flow[].action`（执行步骤）**
```json
"action": "查询失败任务详情（命令：acli task get -k '...' -t <故障日期> -s -1）"
```
→ 本质：**命令式执行步骤**（方案 B）
→ 对应 `Diagnosis.acli_methods`

### 三层 vs 单一语义 — 第一性原理分析

**为什么分三层？不分层会有什么问题？**

核心问题是：排障会话是一个**时序状态机**，不同层级的信息在**不同时间点**被不同的**行为主体**以**不同方式**处理。把三层压成一层的代价是：Agent 面对同一个 `prerequisites` 列表，必须猜测每一条该怎么处理。

#### 不分层的具体问题

假设把三层都合并到 `SOPNode.prerequisites: list[str]`，会出现：

**问题1：执行时序模糊**

三层有完全不同的执行时机：

| 层级 | 执行时机 | 执行次数 |
|------|---------|---------|
| 会话前置（层级1） | S1 路由完成后，遍历**开始前** | **一次**（整个会话有效） |
| 路由判断（层级2） | 每次到达中间节点时 | **N 次**（每个决策点一次） |
| 执行步骤（层级3） | 到达**叶节点后**按序执行 | 每个叶节点一次 |

不分层的后果：Agent 每次到达一个中间节点，都会把「具备 acli 访问权限」当作路由条件再问一遍用户。用户在第 3 个节点会看到「请问您是否具备 acli 访问权限？」

**问题2：Agent 行为模式歧义**

三层对应三种完全不同的 Agent 行为：

```python
# 层级1：向用户收集信息
ask_user("请确认是否具备 acli 命令行访问权限")
ask_user("请提供故障发生日期")

# 层级2：对已有信息做布尔判断（不需要交互）
"0x0CFFFFFF" in task_output  →  goto branch-A
"虚拟机未运行" in task_output →  goto branch-B

# 层级3：执行 acli 命令并观察输出
run_acli("acli task get -k '服务不可用' -t {date} -s -1")
```

把这三种行为合并成一个 `list[str]`，Agent 必须逐条猜测：这一条是要问用户？还是自己判断？还是去执行命令？**这不是 LLM 理解能力的问题，而是信息结构不完备**——即使是人读这份列表，也无法确定第一条"具备acli权限"和第二条"任务包含0x0CFFFFFF"该怎么处理。

**问题3：归属主体不同，存储位置也不同**

```
层级1 → 文档级（整个 SOP 的会话前置要求）
         应存在 sop_document 或 SOPTree 的根节点上
         一份 SOP 只有一份

层级2 → 节点级（每个决策分支的选路条件）
         应存在 SOPNode 上
         树上有多少中间节点，就有多少份

层级3 → 诊断级（叶节点的验证执行序列）
         应存在 Diagnosis 上
         只有叶节点才有
```

如果都塞到 `SOPNode.prerequisites`，要么非叶节点上出现执行命令（还没到执行阶段就跑命令），要么叶节点上出现会话前置检查（重复询问权限）。

#### 结论与统一设计

三层语义分开存储：

```
entry.prerequisites → 会话前置条件（新增）
                      会话开始时，Agent 向用户收集这些信息（一次性）

flow[].branches[].when → SOPNode.prerequisites（路由判断条件，方案 A）
                          用于中间节点选路：我该走哪个分支？（多次，每节点一次）

flow[].action → Diagnosis.page_methods / acli_methods（执行步骤，方案 B）
                用于叶节点验证：如何确认是这个问题？（按序执行）
```

分层的本质不是为了「设计优雅」，而是因为**三层信息的时序、主体、行为模式根本不同**，合并会导致 Agent 行为歧义。

**✅ 结论：`SOPNode.prerequisites` 映射到 `flow[].branches[].when`（声明式路由条件，方案 A）。`entry.prerequisites` 作为会话级前置信息，在 S1 阶段完成 SOP 路由后、正式遍历前由 Agent 向用户收集确认（一次性）。检查步骤放在 `Diagnosis.acli_methods`（方案 B，叶节点时按序执行）。三层语义分开存储，原因是执行时序、行为主体、归属对象三者都不同。**

---

## 三、`acli_methods` 格式设计

### 用户明确的约束

1. `acli_methods` 是**使用说明**（命令 + 说明），不是纯命令字符串
2. 需要 `{placeholder}` 参数占位符约定
3. ReAct 全过程维护一个**参数上下文队列**（host, ip, vm, time, object, keyword 等）
4. 参数来源两种方式：
   - 直接执行不带参数的命令，acli 报错会返回参数列表
   - acli 文档预先转成结构化知识注入 system_prompt

### acli 参数队列设计

```python
class AcliContext(BaseModel):
    """ReAct 循环中维护的参数上下文，贯穿整个诊断过程"""
    host: str | None = None       # 目标主机 IP/域名
    vm_id: str | None = None      # 虚拟机 ID
    object_name: str | None = None  # 操作对象名称
    time_range: str | None = None   # 时间范围（如 "last 1h"）
    keyword: str | None = None      # 搜索关键词
    # 可扩展：{param_name: value}
    extra: dict[str, str] = Field(default_factory=dict)
```

### `acli_methods` 字段格式决策

**方案 A — 保持 `list[str]`（自由文本 + 占位符约定）**
```
["acli vm show {vm_id} --format json  # 查看虚拟机详情，需要 vm_id"]
```
- 优点：导入简单，与现有 SOP 文本兼容
- 缺点：Agent 需要自己解析参数名

**方案 B — 结构化命令对象**
```python
class AcliCommand(BaseModel):
    template: str           # "acli vm show {vm_id} --format json"
    description: str        # "查看虚拟机详细状态"
    required_params: list[str]  # ["vm_id"]
```
- 优点：参数明确，Agent 可直接知道需要哪些参数
- 缺点：导入解析复杂度增加

**当前推荐：方案 A + 约定规范**（降低导入复杂度）：
- 格式：`{param}` 作为占位符标准
- Agent 通过正则提取参数名，从 `AcliContext` 查找值
- 缺少时询问用户或执行 `acli <command> --help` 自动发现

> **关键发现（基于 vmstart_node_sops.jsonl 真实数据）**：SOP 原始数据中 `command_or_path` 字段已与自然语言 `action` 字段**分开存储**，命令本身是干净格式，不是"自然语言混合"的状态。
>
> 占位符格式不一致：真实 SOP 使用 `<故障日期>` 格式，代码约定为 `{date}` 格式。
> **导入时需规范化**：`<param>` → `{param}`（正则替换，成本低，但必须在 `sop_parser.py` 中统一处理，不能依赖 LLM 在运行时解析）。

**✅ 结论：`acli_methods` 保持 `list[str]`，但约定 `{param}` 占位符格式。acli 文档以结构化知识形式注入 system_prompt，Agent 按参数上下文队列自动填充。`sop_parser.py` 导入时统一将 `command_or_path` 中的 `<param>` 格式规范化为 `{param}`。**

---

## 四、模型命名简化：`DiagnosisDetail → Diagnosis`

### 分析

`DiagnosisDetail` 中的 `Detail` 后缀没有携带额外语义：
- `SOPNode.diagnosis: DiagnosisDetail` ≡ `SOPNode.diagnosis: Diagnosis`
- `SOPNode.solution: SolutionDetail` ≡ `SOPNode.solution: Solution`

遵循奥卡姆剃刀原则：**在语义无损失的前提下，用更短的名称**。

**✅ 结论：重命名为 `Diagnosis` 和 `Solution`。这是破坏性变更，需同步更新 `sop_template.py` / `sop_parser.py` / `test_sop_parser.py`。**

### 补充：两个模型都包含 acli 可执行命令

`Diagnosis.acli_methods` — 诊断阶段的命令（如 `acli vm show {vm_id}`，用于确认问题）
`Solution.quick_recovery` / `thorough_fix` — 如果步骤中包含 acli 命令，格式与 `acli_methods` 统一，同样使用 `{param}` 占位符。

---

## 五、症状关键词的反向索引 — 作用解释

> ⚠️ **本节经过重新评估（2026-05-19）**
> 初版分析基于 sop_document 存在 keywords 字段的错误前提。以下为修正后的分析。

### 反向索引的前提不存在

如 Section 一所分析，**`sop_document` 表没有 `keywords` 字段**，在该字段上建 GIN 索引的方案无从实施。

更根本的问题是：S0(意图识别) → `category_id` → S1(FK 查询) 已经完成了 symptom → SOP 的精确路由，反向索引要解决的问题（「从自然语言症状找到 SOP」）在架构层已经被解决。

### 真正的检索基础设施在 `sop_chunk` 层

实测数据库表结构（`\d sop_chunk`）：

```
sop_chunk 字段：
  embedding  vector(1536)  — 索引：ivfflat (embedding vector_cosine_ops)
  tsv        tsvector      — 索引：gin (tsv)
```

这是完整的双路检索基础设施，已在 chunk 级别（段落粒度）实现：
- `embedding`：语义相似度搜索，处理自然语言表述差异
- `tsv`：GIN 全文检索，精确关键词匹配

**这两个字段服务的是 S2 阶段的精确证据定位（在已确定的 SOP 内找到最相关节点），而不是 S0/S1 的 SOP 路由。**

### 两个阶段的检索职责划分

| 阶段 | 职责 | 实现机制 |
|------|------|----------|
| **S0 意图识别** | 症状 → category_id | LLM + 198 分类注入 |
| **S1 第1轨路由** | category_id → sop_document | FK 精确查询 |
| **S2 节点定位** | 用户描述 → 最相关节点 | sop_chunk.embedding（语义）/ sop_chunk.tsv（全文） |

**✅ 结论：`sop_document` 层的反向索引没有必要。真正的检索基础设施（sop_chunk.embedding + tsv）已存在，服务 S2 阶段精确证据定位，不需要重复建设。D1（反向索引）从决策矩阵中移除。**

---

## 六、两种 tool_definition 集成范式的本质区别

### 范式 A — SOP 作为知识库（当前方向）

```
用户问题 → Agent 读取 SOP 文本 → Agent 理解并决策 → Agent 调用通用工具执行

工具集（固定、通用）：
  search_sop(symptom) → 候选 SOP
  get_window(node_id) → 树遍历窗口
  run_acli(command, context) → 命令执行
  ask_user(question) → 人机交互
  record_result(node_id, outcome) → 结果记录
```

### 范式 B — SOP 步骤动态生成 tool_definition

```
SOP 导入时 → 每个 acli_methods 条目 → 生成一个 ToolDefinition

生成的工具（动态、SOP 相关）：
  check_vm_status(vm_id: str) → "acli vm show {vm_id}"
  restart_vm(vm_id: str) → "acli vm restart {vm_id}"
  check_cpu_usage(host: str) → "acli host show {host}"
  ...（每个 SOP 生成 10-30 个工具）
```

### 本质区别

| 维度 | 范式 A（知识库） | 范式 B（动态工具） |
|------|---------------|-----------------|
| **理解/执行边界** | LLM 理解 + 通用工具执行 | LLM 选工具 + 工具内含逻辑 |
| **SOP 的角色** | 被动知识（LLM 阅读） | 主动行为（工具生成来源） |
| **工具集大小** | 固定 5～10 个通用工具 | 随 SOP 数量线性增长（可能数千个） |
| **SOP 变更成本** | 仅重新导入数据 | 需重新生成工具定义 + 重新注册 |
| **执行可靠性** | 依赖 LLM 理解准确性 | 结构化，执行路径明确 |
| **上下文开销** | 较大（需读取节点内容） | 较小（工具签名简洁） |
| **适用场景** | SOP 结构多变，内容丰富 | SOP 高度规范化，命令固定 |

### 如何判断选范式 A 还是范式 B？

不是根据「效果」判断，而是根据**当前系统的成熟度和约束条件**判断。以下是一个决策框架，按优先级排序：

#### 决策树

```
Q1: acli_methods 规范化率是否 ≥ 90%？
    （即：绝大多数 action 都是纯模板命令，可自动解析为工具定义）
    │
    ├─ 否 → 只能选范式 A（技术门槛未达到，范式 B 无法实施）
    │
    └─ 是 → Q2: 是否有强审计/合规要求？
             （生产环境故障处理记录、质量回溯、责任认定等）
             │
             ├─ 是 → 范式 B（审计需求驱动，即使有工程成本也值得）
             │
             └─ 否 → Q3: 预计工具总数是否可控？
                      （SOP 数量 × 每个 SOP 平均工具数，目标 < 300 个工具）
                      │
                      ├─ 是 → 范式 B（规模可控，享受工程属性收益）
                      │
                      └─ 否 → 范式 A（工具集过大，LLM 选工具反而出错）
```

#### 基于 acli 文档和真实 SOP 数据的具体评估

**acli 命令体系本身（http://acli.sangfor.com.cn:6888/commandList）：规范化 100%**

acli 提供约 100 个命令，分布在 11 个命名空间（alert / hardware / log / network / platform / service / storage / system / task / vm 等），每个命令都有完整 schema：

```
命令格式：acli {namespace}+ {cmd}+ [options]
参数格式：-k|--keyword=string   （短名+长名+类型，统一）
          -t|--time=string       （必填标注："必要参数"）
          -s|--status=string     （枚举值明确）
          -l|--limit=integer     （类型约束）
结果格式：每个命令都有"结果示例"（key: value 结构）
```

acli 命令体系完全满足自动生成工具定义的条件——参数类型、必填性、枚举值都已文档化。

**SOP 的 command_or_path 字段（vmstart_node_sops.jsonl，7 步样本）：**

> ⚠️ 关键发现：`command_or_path` 字段与自然语言 `action` 字段是**分开存储**的，命令本身已经是干净格式，不是之前分析的"自然语言混合"。

```python
# flow 步骤的实际结构（真实数据）
{
    "action": "查询失败任务详情，确认错误码...",  # 自然语言描述
    "command_or_path": "acli task get -k '可能由于系统繁忙导致' -t <故障日期> -s -1",  # 干净命令
    "command_example": "acli task get -k '可能由于系统繁忙导致' -t 2024-08-15 -s -1"  # 具体示例
}
```

| 分类 | 数量 | 比例 | 范式 B 可行性 |
|------|------|------|-------------|
| 纯 acli 命令（无管道） | 5/7 | 71% | ✅ 可直接映射为工具调用 |
| 含管道的组合命令 | 2/7 | 29% | ⚠️ 需封装为复合工具（`acli system ps auxf \| grep <id>`） |
| 占位符格式 `<param>` | 6/7 | 86% | ⚠️ 需统一为 `{param}` |

**当前的实际规范化率：约 71%（未达到 90% 门槛）**

主要阻碍不是"结构混乱"，而是两个具体的技术问题：
1. **管道命令（~29%）**：`acli system ps auxf | grep <id>` 不能直接映射为单一工具调用，需要封装
2. **占位符格式不统一**：SOP 使用 `<故障日期>`，代码约定是 `{date}`，需要统一（成本低）

#### 关键维度解释

**Q1：规范化率是最先判断的门槛**

修正之前的悲观结论：acli 命令体系本身规范化率 100%，`command_or_path` 字段已与自然语言分离。真正的瓶颈是管道命令的封装问题（约 29%），而非整体结构问题。

**Q2：审计要求是选范式 B 的最强驱动力**

范式 A 的诊断过程发生在 LLM 内部：`SOP文本 + 用户输入 → LLM → 决策`。出了问题无法追溯「LLM 为什么这么判断」。

范式 B 每一步都有记录：`工具调用 → 写 tool_result → trace_id 串联`。可以精确回放「第 3 步执行了什么命令、返回了什么、LLM 据此选了哪个分支」。

acli 的"命令执行可审计"（官方技术特点）正是这个方向的佐证。

**Q3：工具数量上限是范式 B 的隐性约束**

acli 约 100 个命令，但排障 SOP 实际使用的是高频子集（`task get`, `vm status get`, `platform check`, `system ps` 等约 15-20 个）。若只工具化这个高频子集，工具总数远低于 300 的上限。

#### 渐进式混合策略（基于 acli 文档的具体路径）

```
阶段 1（当前）：纯范式 A
  command_or_path → 注入 system_prompt 作为知识
  run_acli(template, context) 通用工具执行

阶段 2（完成占位符统一后，可较快启动）：部分范式 B
  高频核心命令先工具化（来自 acli 文档的完整 schema）：

  check_task_error(keyword: str, date: str, status: str = "-1")
    → acli task get -k {keyword} -t {date} -s {status}

  get_vm_status(vm_id: str)
    → acli vm status get -v {vm_id}

  check_platform(check_type: str)
    → acli platform check -p {check_type}

  get_vm_config(vm_id: str)
    → acli vm config get -v {vm_id}

  管道命令封装为复合工具（不依赖纯 acli）：
  find_process_by_vm(vm_id: str)
    → 内部执行 "acli system ps auxf | grep {vm_id}"

阶段 3（管道命令封装完成，规范化率达 90%）：纯范式 B
  全量 acli 命令自动生成工具定义（基于在线文档 schema）
```

**✅ 结论：当前选范式 A（规范化率 ~71%，未达门槛）。阶段 2 的启动条件：完成占位符统一（`<param>` → `{param}`）+ 管道命令封装，成本低，可尽早规划。acli 命令体系本身已完全规范化（有完整在线文档和 schema），是范式 B 的坚实基础，演进路径比预想清晰。**

---

## 七、SOP 遍历路径追踪的存储设计

### 需要追踪的数据

| 追踪点 | 数据类型 | 时效性 |
|--------|---------|--------|
| 走过的节点路径 | `["n-1", "n-1-2", "n-1-2-3"]` | 对话生命周期 |
| 每个节点的评估结果 | `{node_id, prerequisites_match: bool, evidence: {}}` | 对话生命周期 |
| 最终选中的叶节点 | `node_id: str` | 持久化 |
| 诊断是否确认 | `confirmed: bool` | 持久化 |
| 处置结果 | `outcome: resolved/unresolved/escalated` | 持久化 |

### 复用 `diagnostic_item` vs 新增表

**现有 `diagnostic_item` 表的设计**：
```
stage: S2/S3/S4/S5（固定阶段）
type:  hypothesis/verification_step/root_cause/solution
content: JSONB（灵活内容）
status: pending/in_progress/confirmed/rejected/skipped
```

**评估：复用 `diagnostic_item`**

SOP 树遍历本质上是一种「结构化假设 → 验证 → 确认」过程，与现有 diagnostic_item 的 S2-S4 阶段语义高度重叠：

| SOP 遍历步骤 | 对应 diagnostic_item |
|------------|---------------------|
| 选路到中间节点（prerequisites 匹配） | `type=hypothesis, stage=S2` |
| 执行 diagnosis 步骤 | `type=verification_step, stage=S3` |
| 确认到达叶节点（根因） | `type=root_cause, stage=S4` |
| 输出 solution 步骤 | `type=solution, stage=S5` |

**需要在 `content` JSONB 中增加的字段：**
```jsonc
// type=hypothesis 时（中间节点选路）
{
  "description": "CPU资源不足",
  "sop_node_id": "n-1-2",          // ← 新增：关联 SOP 节点
  "sop_document_id": 42,           // ← 新增：关联 SOP 文档
  "prerequisites_evidence": {},    // ← 新增：prerequisites 的证据收集
  "probability": 0.8,
  "evidence": [                    // ← D9 证据链（必填）
    {"type": "sop_node", "source_id": "n-1-2", "quote": "此主机剩余可配置CPU不足"}
  ]
}

// type=root_cause 时（叶节点确认）
{
  "description": "CPU资源不足导致虚拟机无法调度",
  "sop_node_id": "n-1-2-3",       // ← 新增
  "sop_traversal_path": ["n-1", "n-1-2", "n-1-2-3"],  // ← 新增：完整路径
  "confidence": 0.95,
  "evidence": [                    // ← D9 证据链（必填）
    {"type": "sop_node", "source_id": "n-1-2-3", "quote": "CPU可用量不足1核时调度失败"},
    {"type": "tool_call", "source_id": "tool-uuid-xxx", "quote": "available_cpu: 0.2"}
  ]
}
```

**✅ 结论：复用 `diagnostic_item`，在 `content` JSONB 中增加 `sop_node_id`、`sop_document_id`、`sop_traversal_path` 字段，无需新增表，无 schema 破坏性变更。**

> 对于高频查询（如"某个 SOP 节点被命中了多少次"），通过 `sop_hit` 表（已有）聚合即可，不需要 diagnostic_item 级别的查询。

---

## 八、滑动窗口上下文控制设计

### 问题定义

SOP 决策树可能有 100+ 节点，若将完整树注入 Agent 上下文：
- 大量不相关节点造成 LLM 注意力分散（幻觉风险）
- Token 消耗随树规模线性增长
- 节点间相似名称导致 LLM 混淆选路

### 滑动窗口核心设计

**窗口内容 = 当前节点（完整）+ 直接子节点（仅预览）+ 历史面包屑（仅名称）**

```python
class SopNodePreview(BaseModel):
    """子节点预览 — 仅展示选路所需的最少信息"""
    node_id: str
    name: str
    prerequisites: list[str]      # 进入条件（用于 Agent 判断是否匹配）

class SopTraversalWindow(BaseModel):
    """ReAct 循环中每一步传给 Agent 的上下文窗口"""
    # 当前位置（完整信息）
    current_node: SOPNode         # 含 diagnosis/solution（叶节点时完整展示）

    # 向前看（仅预览，不展开孙子节点）
    children_preview: list[SopNodePreview]

    # 历史面包屑（仅名称，不含详情）
    breadcrumb: list[str]         # ["虚拟机启动失败", "CPU资源不足"]

    # 状态
    is_at_leaf: bool
    can_backtrack: bool           # 是否允许回溯到父节点
    excluded_node_ids: list[str]  # 本次已排除的分支（防止循环）
```

### 窗口大小估算（固定上限）

| 内容 | Token 估算 |
|------|-----------|
| 当前节点（含 diagnosis/solution） | ~300-500 tokens |
| 子节点预览（每个节点 50 tokens × 最多 8 个） | ~400 tokens |
| 面包屑路径（每级 20 tokens × 最多 6 级） | ~120 tokens |
| **合计** | **~1000 tokens（固定上限）** |

与完整树（可能 5000+ tokens）相比，**节省 80% 上下文开销**。

### 滑动机制

```
初始化：window = get_window(root_node_id)

─── 向前滑动（正常路径）───────────────────────────────
Agent 选择子节点 n-1-2
  → window = get_window("n-1-2")
  → breadcrumb 追加 "n-1-2 名称"
  → excluded_node_ids 清空（进入新层级）

─── 向后滑动（回溯）──────────────────────────────────
Agent 判断当前层所有子节点都不匹配
  → window = get_window(parent_node_id)
  → excluded_node_ids 记录当前节点（防止重复进入）
  → breadcrumb 弹出最后一项

─── 到达叶节点 ────────────────────────────────────
window.is_at_leaf = True
  → children_preview = []（无子节点）
  → current_node 展示完整 Diagnosis + Solution
  → Agent 执行 diagnosis 步骤，确认后输出 Solution

─── 无匹配（全树遍历失败）──────────────────────────
所有路径都在 excluded_node_ids 中
  → 返回 "未找到匹配 SOP，升级处理"
  → diagnostic_item 记录 outcome=escalated
```

### 子节点数量上限设计

当某个中间节点有超过 8 个子节点时，分批展示（分页）：

```python
children_preview: list[SopNodePreview]  # 单次最多 8 个
children_total: int                     # 子节点总数
children_page: int                      # 当前页（从 0 开始）

# Agent 工具：next_children_page(node_id, page) → 下一批子节点预览
```

**✅ 结论：实现 `SopTraversalWindow` 模式，每轮只传当前节点 + 子节点预览 + 面包屑，固定上下文开销 ~1000 tokens。`get_window(node_id)` 作为核心导航工具，替代「传整棵树」的暴力方案。**

---

---

## 九、证据链设计 — 每条 AI 回复必须标注来源

### 问题动机

AI 诊断助手的每条回复内容有两种来源：
- **有来源**：基于 SOP 节点内容、工具调用结果、用户确认信息
- **无来源**：AI 自身推理（无外部依据）

当前设计中两者混合，用户无法判断「这个判断是 SOP 里说的还是 AI 自己猜的」。缺少证据链会直接影响运维人员对诊断结果的信任度。

### 证据类型定义

```python
class EvidenceType(str, Enum):
    SOP_NODE     = "sop_node"    # 来自 SOP 节点内容（sop_node_id 可追溯）
    TOOL_CALL    = "tool_call"   # 来自工具调用结果（tool_result_id 可追溯）
    USER_CONFIRM = "user"        # 来自用户明确确认
    INFERENCE    = "inference"   # AI 自身推理，无外部来源（需显式标注）

class EvidenceRef(BaseModel):
    type: EvidenceType
    source_id: str | None = None   # sop_node_id / tool_result_id / message_id
    quote: str | None = None       # 引用的原文片段（不超过 100 字）
```

### 与 diagnostic_item 的集成

在 `diagnostic_item.content` JSONB 的**所有 type** 中增加 `evidence` 字段：

```jsonc
// type=hypothesis（中间节点选路）
{
  "description": "CPU资源不足",
  "sop_node_id": "n-1-2",
  "evidence": [
    {
      "type": "sop_node",
      "source_id": "n-1-2",
      "quote": "此主机剩余可配置CPU不足"
    }
  ]
}

// type=verification_step（执行诊断命令后）
{
  "description": "acli task get 返回错误码 0x0CFFFFFF",
  "evidence": [
    {
      "type": "tool_call",
      "source_id": "tool-result-uuid-xxx",
      "quote": "状态：失败，描述：服务不可用，错误码：0x0CFFFFFF"
    }
  ]
}

// type=root_cause（确认根因）
{
  "description": "Redis OOM 导致虚拟机开机失败",
  "evidence": [
    {
      "type": "sop_node",
      "source_id": "branch-A-leaf",
      "quote": "Redis内存不足导致服务返回0x0CFFFFFF"
    },
    {
      "type": "tool_call",
      "source_id": "tool-result-uuid-yyy",
      "quote": "redis-cli info memory: used_memory_human:2.99G, maxmemory_human:3.00G"
    },
    {
      "type": "user",
      "source_id": "msg-uuid-zzz",
      "quote": "确认，Redis确实在故障前有内存告警"
    }
  ]
}
```

### AI 自身推理的显式标注规则

当 AI 的某个判断没有外部依据时，**必须标注为 `inference` 类型**：

```jsonc
{
  "description": "综合以上信息，最可能的根因是 Redis OOM",
  "evidence": [
    {"type": "inference", "source_id": null, "quote": "基于错误码和内存使用率的综合判断"}
  ]
}
```

### 前端展示设计

```
┌─────────────────────────────────────────────────┐
│ AI: 根据诊断结果，确认根因为 Redis OOM            │
│     ▼ 3 条证据来源 [展开]                         │
│     ├─ 📋 SOP节点: "Redis内存不足导致0x0CFFFFFF"  │
│     ├─ 🔧 工具输出: "used_memory: 2.99G/3.00G"   │
│     └─ 👤 用户确认: "Redis确实有内存告警"          │
└─────────────────────────────────────────────────┘
```

**✅ 结论：所有 `diagnostic_item.content` 增加 `evidence: list[EvidenceRef]` 字段。AI 推理类内容必须显式标注 `inference` 类型。前端提供可折叠的「证据来源」面板，让运维人员可验证每条诊断结论的依据。**

---

## 汇总：设计决策矩阵

| 编号 | 决策项 | 结论 | 影响文件 |
|------|-------|------|---------|
| D1 | `keywords` 字段 | **取消**：路由已由 category_id FK 闭环；二级检索用 `sop_chunk.embedding`/`tsv` | `desired_schema.sql`（移除 keywords） |
| D2 | `prerequisites` 三层语义 | entry.prerequisites=会话前置；branches.when=路由条件→SOPNode.prerequisites；action=执行步骤→Diagnosis | `sop_template.py`, `sop_parser.py` |
| D3 | `acli_methods` 格式 | `list[str]` + `{param}` 占位符约定 | `sop_template.py` |
| D4 | AcliContext 参数队列 | 新增 `AcliContext` 模型，贯穿 ReAct 循环 | 新增 schema |
| D5 | 模型命名 | `DiagnosisDetail → Diagnosis`, `SolutionDetail → Solution` | `sop_template.py`, `sop_parser.py`, `test_sop_parser.py` |
| D6 | tool_definition 范式 | 范式 A（当前：规范化率 ~71%，未达 90% 门槛）；阶段 2 启动条件：占位符统一 + 管道命令封装；acli 命令体系已完全规范化，演进路径清晰 | — |
| D7 | 遍历路径追踪 | 复用 `diagnostic_item`，`content` JSONB 增加 `sop_node_id` 等字段 | `desired_schema.sql` |
| D8 | 上下文控制 | `SopTraversalWindow` 滑动窗口，~1000 tokens 固定上限 | 新增 schema + service |
| D9 | 证据链 | `diagnostic_item.content` 增加 `evidence: list[EvidenceRef]`；AI 推理必须显式标注 `inference` | `sop_template.py`, 新增 `EvidenceRef` schema |

---

*文档版本: 1.1 | 创建日期: 2026-05-19 | 更新日期: 2026-05-19（修正 S0 理解错误；基于真实 DB 结构重新评估 D1；更新 D2 为三层语义；深化 D6 原因分析；新增 D9 证据链）*
