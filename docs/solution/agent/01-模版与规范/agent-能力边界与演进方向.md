# Agent 能力边界与演进方向

> 本文档分析 Tool / Skill / SOP / ReAct / Agent 五个层面的当前能力边界、
> 不支持的功能、替代方案和扩展优先级 Roadmap。

---

## 一、Tool 能力边界与扩展方向

#### 现状：只支持单条命令

当前 `BridgeRelayExecutor` 的执行链路：

```
usage_template → TemplateInterpolator 插值 → CommandSanitizer 净化 → SSH 执行
```

`CommandSanitizer` 明确拦截以下模式（`executor.py:117-126`）：

```python
_FORBIDDEN_PATTERNS = [
    (r"\$\([^)]*\)", "命令替换 $(...)"),   # 禁止
    (r"`[^`]*`",    "命令替换 `...`"),    # 禁止
    (r"\s*&&\s*",    "命令链 &&"),         # 禁止
    (r"\s*\|\|\s*",  "命令链 ||"),         # 禁止
    (r"\s*;\s*",     "命令链 ;"),           # 禁止
]
```

| 模式 | 当前是否支持 | 拦在哪里 |
|------|:--:|------|
| **单条命令 + 参数插值** | ✅ | `usage_template: "acli plugins netdoctor --node {node_ip}"` → `TemplateInterpolator` 渲染 → 通过净化 |
| **多条命令串联**（`cmd1; cmd2; cmd3` 或 `cmd1 && cmd2`） | ❌ | Sanitizer 拦截 `;` `&&` `\|\|` |
| **Bash heredoc / 多行脚本** | ❌ | `<<EOF ... EOF` 语法被拦截；无脚本文件注入容器机制 |
| **Python 代码** | ❌ | 目标宿主机**没有 Python 环境**（代码中已硬编码纠错提示：`bash: python3: command not found`）；无文件注入机制 |

#### 如果未来要支持，需要改三处

**1. Sanitizer —— 放开单条命令约束，允许可控的脚本注入**

```
现状: 正则拦截 ; && || $() `
目标: 对 usage_template 渲染产物走白名单通道，跳过净化（因为模板本身是 Git 版本受控的）
      或者引入 script_content 参数专门承载脚本，走单独的注入管道
```

**2. ContainerCommandBuilder —— 支持脚本文件注入**

```
现状: 只构建单条命令的容器执行包装
目标: 支持 base64 编码脚本 → 写入容器临时文件 → 执行 → 清理
      echo "<base64>" | base64 -d > /tmp/diag_script.sh && bash /tmp/diag_script.sh
```

**3. ToolRegistry / Tool Definition —— 新增 `script_mode` 字段**

```yaml
# tool_definition 新增字段
script_mode: "none" | "bash" | "python"
  # none  = 默认，单条命令模式
  # bash  = usage_template 是 bash 脚本模板，Sanitizer 走白名单通道
  # python = usage_template 是 Python 脚本模板，需确保宿主机有 Python 环境
```

#### 改之前的替代方案

在 Sanitizer 放开之前，复杂逻辑有三种迂回方式：

**方式一：拆成多个 Skill 串联**（当前可用）

```
不改 Tool，把复杂逻辑表达为 Skill 的 instructions_md：
  步骤 1: 调 tool_A 获取数据
  步骤 2: 调 tool_B 基于步骤1结果查询
  步骤 3: LLM 按规则综合判断输出结论
```

**方式二：用 acli 插件封装**（当前可用）

```
把复杂脚本封装为 acli 插件（如 acli_plugin_netdoctor），
Tool 只负责传参数，脚本逻辑在 acli 插件侧实现。
```

**方式三：用 `jq` / `grep` / `awk` 替代 Python**（当前可用）

```
当前宿主机有 jq/grep/awk/sed，绝大多数 JSON 解析和文本处理可以用它们完成。
executor.py 甚至硬编码了 Python 不可用时的纠错提示，引导 LLM 改用 jq。
```

#### 当前模版建议：保持单条命令，复杂逻辑放 Skill

结合现状，Tool 模版的设计原则应该是：

| 复杂度 | 放哪里 | 为什么 |
|--------|--------|--------|
| 单条命令 + 参数化 | Tool (`usage_template`) | `TemplateInterpolator` 原生支持 |
| 多步骤串联 | Skill (`instructions_md`) | LLM 按步骤多次调用 Tool，Skill 控制推理流程 |
| 复杂脚本处理 | acli 插件 或 Skill | 插件在节点侧执行不受 Sanitizer 限制；Skill 用 jq/grep/awk 替代 |
| 跨步骤逻辑判断 | SOP 决策树 | `prerequisite_items` + `## 诊断方法` 定义分支条件


---

## 二、Skill 能力边界与扩展方向

#### 现状：纯 LLM 推理，无工具调用，无状态

`DynamicSkillRunner.execute()` 的完整流程：

```
SOP 触发 skill_call → DynamicSkillRunner.get_active_skill()
  → 从 DB 加载 skill_definition 行
  → 校验 allowed_tools 仍有效
  → 构建 prompt: system="你是动态 Skill 运行器..." + user={instructions_md, context_variables}
  → LLM 推理（response_format=json_object）
  → extract_output_value() 提取变量值
  → 写回 conversation context_variables
```

| 能力 | 当前是否支持 | 说明 |
|------|:--:|------|
| **纯文本推理**（规则匹配、分类、判断） | ✅ | `instructions_md` 中的规则表驱动 LLM 输出结构化 JSON |
| **调用其他 Skill** | ❌ | Skill 执行期间不能调用其他 Skill，只能通过 SOP 的 `depends_on` 串联 |
| **调用 Tool** | ❌ | Skill 的 LLM 调用没有 tool 列表，`allowed_tools` 只用于声明依赖关系，不在执行时传入 LLM |
| **多轮推理** | ❌ | 一次 `execute()` = 一次 LLM 调用 = 一个 JSON 输出，不支持 ReAct 式的多步推理 |
| **访问外部数据** | ⚠️ | `context_variables` 通过 prompt 传入，但 Skill 不能主动查询数据库或调用 API |
| **引用资源文件** | ❌ | `assets_json` 和 `references_json` 存入 DB 但 `DynamicSkillRunner` **未将它们注入 LLM prompt** |
| **有状态执行** | ❌ | 每次调用独立，无 session 记忆 |

**当前 Skill 本质上是"带指令模板的单次 LLM 推理调用"**——不是一个能自主行动的 Agent。

#### 如果要扩展，按优先级

**P0：资源文件注入**（代码改动最小，收益明确）

```python
# dynamic_runner.py 的 messages 构建中增加 assets 和 references
user_content = json.dumps({
    "skill_name": snapshot.skill_name,
    "description": snapshot.description,
    "instructions_md": snapshot.instructions_md,
    "allowed_tools": snapshot.allowed_tools,
    "assets": snapshot.assets_json,        # ← 新增：规则表、配置数据
    "references": snapshot.references_json, # ← 新增：参考文档
    "context_variables": context_variables,
})
```

**P1：Tool 调用能力**（Skill 内部可以调 `bash_exec` 获取实时数据）

```python
# 给 Skill 的 LLM 调用加上 tools 参数
invoke_result = await ai_client.invoke(
    messages=messages,
    tools=allowed_tool_schemas,  # ← 从 allowed_tools 解析出 Function Calling schema
    response_format={"type": "json_object"},
)
# 如果 LLM 返回 tool_calls → 执行 → 把 tool_result 追加到 messages → 再次调用 LLM
```

**P2：多步推理**（Skill 内部 ReAct 循环）

```
当前: Skill = 1 次 LLM 调用
目标: Skill = N 次 LLM 调用 + M 次 Tool 调用（在 allowed_tools 范围内）
      类似一个小型 ReAct 循环，有最大步数限制
```

**P3：Skill 间调用**（组合 Skill）

```
允许 instructions_md 中声明 "调用其他 Skill" 的步骤，
由 DynamicSkillRunner 递归执行，类似函数调用栈。
```

#### 改之前的替代方案

| 需求 | 替代方案 |
|------|---------|
| 需要多步推理 | 拆成多个 Skill，通过 SOP `variable_schema` 的 `depends_on` 串联调用 |
| 需要调用 Tool | 在 SOP 中声明为独立的 `tool_call` 变量，通过 `depends_on` 排在 Skill 之前 |
| 需要访问外部数据 | 把数据采集拆成独立的 Tool 变量，让 LLM 在 ReAct 循环中先调 Tool 再调 Skill |
| 需要复杂脚本处理 | 见 §1.5——拆成 acli 插件或用 `jq` 替代 |


---

## 三、SOP 能力边界与扩展方向

#### 现状：导航工具化 + 信任型变量门禁 + 静态决策树

SOP 执行依赖 LLM 在 ReAct 循环中主动调用三个导航工具：

```
LLM → get_sop_node(node_id)     # 获取节点内容 + 子节点列表
    → sop_request_variable(...) # JIT 获取变量（可能触发 Tool/Skill/用户输入）
    → sop_advance(target, ...)  # 推进到子节点，检查变量门禁
    → 叶子节点执行诊断命令 / 展示解决方案
```

| 能力 | 当前是否支持 | 说明 |
|------|:--:|------|
| **决策树导航**（分步获取节点、逐层推进） | ✅ | `get_sop_node` + `sop_advance`，避免一次性注入完整 SOP 文档 |
| **变量 JIT 懒加载**（用到才获取） | ✅ | `sop_request_variable` 按 `acquisition_strategy` 自动决策获取路径 |
| **依赖链解析**（depends_on 递归获取） | ✅ | `engine.py` 自动递归解析依赖，缺失时返回 `missing_dependencies` |
| **路由锁定**（多轮对话保持 SOP 轨道） | ✅ | 检测到 `sop_resume_context` 时绕开三轨路由，直接锁定 SOP |
| **变量门禁——强阻断** | ✅ | `env_injection` / `user_input` / `user_confirm` 未就绪时阻断 `sop_advance` |
| **变量门禁——软推荐** | ⚠️ | `skill_call` / `tool_call` 只在 `preferred_next_steps` 中提示，不强制阻断 |
| **动态树修改** | ❌ | `tree_json` 在 approve 时静态生成，运行时不能增删节点 |
| **并行分支探索** | ❌ | 一次只能在一个节点上，不能同时探索多个分支再比较 |
| **子 SOP 调用** | ❌ | 不能从一个 SOP 跳转到另一个 SOP（如"磁盘故障"→ 自动进入"磁盘更换 SOP"） |
| **前置条件可执行** | ⚠️ | `content_type: command` 的前置条件会被提取但 LLM **可能跳过不执行** |
| **节点回退** | ❌ | 一旦 `sop_advance` 推进，无法回到父节点重新评估 |
| **运行时校验** | ⚠️ | 只在 `sop_advance` 时检查变量门禁，`get_sop_node` 不校验 |

#### 核心问题：信任型架构 vs 强制型架构

当前 SOP 是 **Trust-based**（信任依赖型）：依赖 LLM 阅读 prompt 中的约束后**主动遵守**。这在模型版本切换、上下文压缩、存在低阻力替代路径时可能失效。

最典型的例子（来自项目 CHANGELOG）：
> `skill_call` 变量不在硬门禁范围内，LLM 天然选择最短路径（`bash_exec` 直接解读 SMART 数据），完全绕过 `sop_request_variable` → Skill 触发链路。

#### 如果要扩展，按优先级

**P0：变量门禁升级——`skill_call` / `tool_call` 纳入硬阻断**

```
当前: skill_call/tool_call 缺失 → preferred_next_steps 提示（LLM 可忽略）
目标: 作为可配置的硬门禁选项，SOP 级别声明 gate_mode: strict | soft
      strict 模式: 所有非自动策略（user_input/env_injection/skill_call/tool_call）
                  缺失时硬阻断 sop_advance
```

**P1：前置条件可执行校验**

```
当前: ### 前置条件 → prerequisite_items → 纯文本给 LLM 看
目标: content_type: command 的前置条件 → sop_advance 前自动执行并校验结果
      不满足条件时返回具体原因，而非让 LLM 自己判断
```

**P2：子 SOP 调用**

```
当前: 一个 SOP 一条路走到黑
目标: 叶子节点可以声明 next_sop: "sop-disk-replacement"
      到达叶子节点后自动触发目标 SOP 的根节点
```

**P3：运行时决策树动态裁剪**

```
当前: tree_json 固定
目标: 根据变量池中的值（如 alert_type=network_error）自动隐藏不相关的分支，
      减少 LLM 需要理解的决策树大小
```

#### 改之前的替代方案

| 需求 | 替代方案 |
|------|---------|
| 防止 LLM 绕过 Skill | 在 System Prompt 中强化 `sop_request_variable` 使用规范（当前已做，P2 级） |
| 前置条件自动检查 | 在 `## 诊断方法` 的 `acli 命令` 中列出检查命令，LLM 调用前必须执行 |
| 跨 SOP 跳转 | 在当前 SOP 的 solution 中写"建议走 SOP: xxx"，由 S0 阶段重新做意图路由 |
| 并行分支 | 在当前节点列出所有子节点条件，LLM 逐一评估后选择最匹配的 |


---

## 四、ReAct 能力边界与扩展方向

当前 ReAct 实现基于 `react_engine.py`，是一个标准的 Thought→Action→Observation 循环。

| 能力 | 是否支持 | 说明 |
|------|:--:|------|
| **多步推理循环** | ✅ | 最多 `MAX_STEPS=40` 步，每步 LLM 决定调哪个 Tool |
| **工具并行调用** | ❌ | 一次只能调一个 Tool，不支持 Function Calling 的原生并行 tool_calls |
| **动态工具发现** | ❌ | 工具列表在 Agent 初始化时固定，不能在推理过程中动态注册新 Tool |
| **反思与自我纠错** | ⚠️ | 仅依赖 LLM 在 Thought 中自觉纠错，无独立的 Reflection 阶段 |
| **子树探索** | ❌ | 不能在某个步骤"分叉"探索多个方案后再综合比较（类似 Tree-of-Thought） |
| **工具结果缓存** | ⚠️ | 工具结果持久化到 `message` 表（`tool_call` + `tool_result`），但 LLM 在同一个 session 内可能重复调用相同工具 |
| **上下文压缩** | ⚠️ | 滑动窗口：最近 10 步完整保留，更早的工具输出截断为 200 字符。长会话可能丢失关键 Observation |
| **SOP 模式切换** | ✅ | 检测到 `sop_resume_context` 时自动切换到 SOP 锁定模式 |
| **流式输出** | ✅ | `_event_stream` 通过 SSE 实时推送到前端 |
| **手动继续** | ✅ | 用户发送"继续"触发下一轮推理 |

**核心限制**：

1. **线性推理** — 不能在推理中"先分叉探索两个分支，再综合选择"。一旦选错方向，靠 LLM 自觉回头。
2. **步数上限** — 40 步对复杂排障可能不够（项目 CHANGELOG 中记录过"复杂排障或重连重新执行命令时步骤极易超限"）。
3. **工具选择无约束** — LLM 可以选择任何已注册 Tool，即使跟当前 SOP 节点无关。`preferred_next_steps` 只是软推荐。
4. **Observation 截断风险** — 历史工具输出被截断为 200 字符，后续推理可能基于不完整信息。

**如果要扩展**：

| 优先级 | 扩展方向 | 说明 |
|:--:|------|------|
| P0 | **工具选择约束** | SOP 模式下强制限制可用工具为当前节点声明的 `acli_methods` + SOP 导航工具 |
| P1 | **Observation 摘要替代截断** | 用 LLM 对历史工具输出生成结构化摘要（保留关键数值），替代简单截断 |
| P1 | **并行工具调用** | 支持 Function Calling 原生 `tool_calls` 数组，Leaf 节点的多个诊断命令可并行执行 |
| P2 | **Reflection 阶段** | 每 N 步插入一次主动反思：当前路径是否正确？是否遗漏关键信息？是否应回退？ |
| P3 | **探索-利用平衡** | 允许 SOP 模式下在多个高概率子节点间"预探索"（只看 prerequisites 不执行诊断），选择最佳分支 |
| P3 | **步数动态调整** | 根据 SOP 树深度动态调整 `MAX_STEPS`，浅树少步数、深树多步数 |


---

## 五、Agent 整体能力边界与扩展方向

前面四节分别从 Tool、Skill、SOP、ReAct 四个层面分析了各自的能力边界。这一节从 Agent 整体架构视角总结跨层问题和扩展方向。

### 5.1 当前 Agent 架构全景

```
用户 → S0 Triage（意图识别 + 4+1 分类）
         │
         ├─ SOP 命中 → ReAct 循环（SOP 锁定模式）
         │                ├─ get_sop_node
         │                ├─ sop_request_variable → Tool/Skill/用户输入
         │                └─ sop_advance → 诊断 → 解决方案
         │
         └─ SOP 未命中 → ReAct 循环（通用推理模式）
                          ├─ KBD 检索
                          └─ 自由工具调用
```

### 5.2 跨层问题

以下是无法归到单个模块的架构级问题：

| 问题 | 涉及模块 | 影响 |
|------|---------|------|
| **S0 路由漂移** | TriageAgent + SOP + ReAct | 多轮对话中 S0 分类可能改变，导致已锁定的 SOP 被替换为新的 SOP（或退出 SOP 模式） |
| **上下文通货膨胀** | ReAct + Skill + SOP | 工具调用历史 + SOP 树节点 + Skill 指令 + 变量值持续累积，逼近 LLM 上下文窗口上限 |
| **静默失败** | ReAct + Tool Executor | 工具返回 `exit_code=0` 但数据不完整时，LLM 可能基于不完整数据给出自信的错误结论 |
| **知识过期** | KBD + Skill | KBD 案例库和 Skill 规则表可能因软件升级、硬件换代而过期，但没有自动检测机制 |
| **多工单并发** | 全栈 | 多个工单共享同一个 Agent 实例，一个工单的长时间工具调用可能阻塞其他工单 |
| **冷启动盲区** | S0 + SOP | 新类型故障在 KBD 和 SOP 中都没有覆盖，Agent 只能退化为自由推理，质量不可控 |

### 5.3 如果要扩展，按优先级

**P0：静默失败检测**

```
问题: 工具执行成功但数据不完整 → LLM 基于残缺数据自信输出错误结论
方案:
  1. Tool output_schema 声明必填字段
  2. 执行器校验输出是否完整（required 字段缺失 → 标记为 partial）
  3. partial 结果注入 LLM prompt 时附带 "⚠️ 此结果不完整，缺失字段: [...]"
  4. Rubric 评分器增加 "数据完整性检查" 维度
```

**P0：SOP 锁定强化**

```
问题: 多轮对话中 LLM 可能在 SOP 模式和通用模式间反复横跳
方案:
  1. 一旦 sop_advance 推进到诊断节点 → SOP 锁定不可退出
  2. 只有到达 solution 节点或用户明确"取消排障"才解锁
  3. ReAct 引擎在 SOP 锁定模式下只用 SOP 导航工具 + 当前节点的 acli_methods
```

**P1：上下文窗口管理**

```
问题: 工具调用 + SOP 树 + Skill 指令累积 → 超出 LLM 窗口
方案:
  1. SOP 模式: 只保留当前节点 + 父节点链，不加载完整树
  2. 工具历史: 滑动窗口（当前已做）+ Observation 摘要（§4.8.6 P1）
  3. Skill 指令: 执行完立即释放，不保留在后续 prompt 中
  4. 窗口预警: 接近上限时通知 LLM "需尽快收束到结论"
```

**P1：多工单隔离**

```
问题: 多个 conversation 共享同一 Agent
方案:
  1. 每个 conversation 独立 context_variables 命名空间
  2. 工具执行结果按 conversation_id 隔离存储
  3. 长时间工具调用不阻塞其他 conversation（当前 Redis blpop + 超时已部分实现）
```

**P2：冷启动知识推荐**

```
问题: 未知故障没有匹配的 SOP/KBD
方案:
  1. 自由推理模式下，LLM 输出诊断过程后，自动生成 "SOP 草稿建议"
  2. 草稿发送给管理员审核 → 一键转化为 SOP 草稿
  3. 持续积累 → 冷启动问题逐步消失
```

**P2：跨案例学习**

```
问题: 每个工单独立排障，不积累经验
方案:
  1. 高频故障模式自动聚类（如 "过去 30 天 60% 的开机失败是磁盘 I/O 超时"）
  2. 聚类结果反馈到 KBD 标签权重，影响 S0 意图排序
  3. 每个 SOP 节点的 hit_count 上升 → 提升在 variable_schema 中推荐优先级
```

**P3：多 Agent 协作**

```
问题: 单一 Agent 模型难以精通所有领域（存储/网络/计算/安全）
方案:
  1. 按领域拆分子 Agent（StorageAgent / NetworkAgent / ComputeAgent）
  2. 主 Agent 在 S0 阶段路由到子 Agent
  3. 子 Agent 有独立的 SOP 库和 Skill 库
  4. 评测维度增加: 路由准确率、协作效率、结论一致性
```

### 5.4 能力边界总览

```
                         当前支持      近期可支持    远期规划
                         (v2.x)      (v2.17-v2.18) (v3.x)
──────────────────────────────────────────────────────────
Tool: 单条命令            ✅           -             -
Tool: Bash/Python脚本     ❌           -             ❌(需Sanitizer+容器改造)
Skill: 单次LLM推理        ✅           -             -
Skill: 调用Tool           ❌           P1            -
Skill: 多步推理           ❌           -             P2
SOP: 决策树导航           ✅           -             -
SOP: 硬变量门禁           ⚠️(软推荐)   P0            -
SOP: 子SOP调用            ❌           -             P2
ReAct: 线性推理           ✅           -             -
ReAct: 并行工具调用        ❌           P1            -
ReAct: Reflection        ❌           -             P2
Agent: SOP路由锁定        ⚠️(可漂移)   P0            -
Agent: 静默失败检测        ❌           P0            -
Agent: 上下文管理          ⚠️(仅截断)   P1            -
Agent: 多Agent协作         ❌           -             P3
Agent: 跨案例学习          ❌           -             P2
```

---

## 六、相关文档

- **[Agent 资源定义模版](./agent-resource-模版.md)**：Tool / Skill YAML 字段说明、SOP Markdown 写作规范、变量声明表列说明、表达式用法
- **[Agent 测评与 GitOps 全生命周期方案](../03-测评与GitOps/agent-测评与GitOps方案.md)**：七维评分体系、CI 门禁触发矩阵、GitOps 全生命周期衔接

