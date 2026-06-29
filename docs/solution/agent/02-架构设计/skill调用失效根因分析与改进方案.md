# HCI 智能排障平台 — Skill 调用失效根因分析与改进方案

> **第一性原理分析报告**
>
> 以工单 `Q2026062036731`（对话 ID：`def2eceb-c745-4a06-90f6-1dba08745238`）为实例，对 SOP 排障过程中 `hci-alert-parsing` 与 `hci-disk-vendor-lifetime` 两个核心 Skill 未被触发的全链路根因进行深度剖析，并给出可落地的分层改进方案。

---

## 一、事件背景与现象复原

### 1.1 工单基本信息

- **工单号**：Q2026062036731
- **会话 ID**：`def2eceb-c745-4a06-90f6-1dba08745238`
- **报障描述**：节点硬盘故障（磁盘寿命检测/SMART 分析场景）
- **匹配 SOP**：硬盘类故障处置 SOP（含 `hci-alert-parsing` 与 `hci-disk-vendor-lifetime` 两个关键 Skill）

### 1.2 观测到的异常现象

通过 `dynamic_resource_usage_audit` 表查询（`conversation_id = 'def2eceb-...'`），本次完整的 ReAct loop 中：

| 资源类型 | 名称 | 是否触发 |
| :--- | :--- | :--- |
| `sop` | 硬盘故障 SOP | ✅ 已触发 |
| `tool` | `bash_exec` / `acli_exec` / `get_active_alerts` | ✅ 已触发 |
| **`skill`** | **`hci-alert-parsing`** | ❌ **未触发** |
| **`skill`** | **`hci-disk-vendor-lifetime`** | ❌ **未触发** |

同时，对 `message` 表的 `tool_call` 类消息分析确认：**全程没有任何对 `sop_request_variable` 工具的调用记录**。

### 1.3 LLM 实际行为路径

LLM 通过以下路径完成了等价的诊断任务，但完全绕过了 Skill 系统：

```
get_sop_node(n-1-2-2-2)
  → 读取 required_variables（含 skill_call 类型的 check_meth）
  → [LLM 自行决策：忽略 sop_request_variable，直接执行 bash_exec]
  → bash_exec: smartctl -a /dev/sda
  → bash_exec: acli_exec get_disk_info
  → 手动解读 SMART 输出，自行判定磁盘健康状况
  → sop_advance(target_node_id=...)
```

---

## 二、根因分析（Why-Why 五层追问）

### 2.1 表象层（What）：两个 Skill 未触发

事实如上，不再赘述。

### 2.2 L1 原因：LLM 未调用 `sop_request_variable`

`sop_request_variable` 是触发 `skill_call` 类变量获取的唯一入口。LLM 跳过了它。

### 2.3 L2 原因：LLM 有等价的低阻力替代路径

在 ReAct 框架下，**LLM 的核心决策逻辑是"用已知有效路径最快解决当前问题"**，而非"寻找系统设计者期望的最优路径"。

本次案例中：
- `bash_exec: smartctl -a /dev/sda` 可以直接返回 SMART 数据
- LLM 读懂了 SMART 数据，并能给出判断
- 没有任何机制阻止它用这条路径

因此，LLM 选择了摩擦力最小的路径，Skill 系统形同虚设。

### 2.4 L3 原因：变量门禁（Variable Gate）设计存在盲区

查阅 [`_find_missing_guarded_variables`](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/sop/nav.py) 的实现逻辑：

```python
# 当前受 Guard 覆盖的策略（会产生硬阻断）
GUARDED_STRATEGIES = {"env_injection", "user_input", "user_confirm"}

# skill_call / tool_call / llm_inference 不在 Guard 范围内
# → 即使变量未就绪，LLM 依然可以继续调用任何工具
```

`skill_call` 类变量被设计为"自动获取、无需阻断"，但其自动触发依赖 **LLM 主动调用 `sop_request_variable`**。当 LLM 不调用时，"自动"永远不会发生。

这是一个**静默的死循环**：
```
skill_call 不在 Gate → LLM 直接执行 raw 命令 → 变量永远不进池 → Skill 永远不触发
```

### 2.5 L4 原因：系统内容层与行为约束层混淆

`get_sop_node` 的响应体中，`required_variables` 包含了变量的 `acquisition_strategy`：

```json
{
  "required_variables": [
    {
      "name": "check_meth",
      "acquisition_strategy": "skill_call",
      "acquisition_tool": "hci-disk-vendor-lifetime"
    }
  ]
}
```

这段信息对 LLM 而言是**元数据注释**，LLM 不会自动把 `"acquisition_strategy": "skill_call"` 理解为"必须先调用 `sop_request_variable`"。这是**信息展示（Content）与行为约束（Enforcement）的经典混淆错误**。

### 2.6 L5 根因（系统设计层）：架构依赖 LLM 的"善意配合"

当前 Skill 触发路径的完整依赖链是：

```
[设计意图] SOP Skill 应被自动执行
       ↓
[实现机制] LLM 主动调用 sop_request_variable
       ↓
[隐含前提] LLM 理解 skill_call 语义 AND 没有更简单的替代路径
       ↓
[现实] 上述两个前提均不成立
```

这是一个 **Trust-based（信任依赖型）** 架构，而非 **Enforce-based（强制约束型）** 架构。

> **第一性原理断言**：任何只靠 Prompt/内容暗示建立的行为规范，都会在以下场景失效：
> - 模型版本切换后的行为漂移
> - 多轮对话上下文压缩后的遗忘
> - 存在低阻力替代路径时的路径偏好
>
> **可靠的行为约束必须在系统层实施，而非在内容层期望。**

---

## 三、问题结构全景图（Why-Why 树）

```
❌ hci-alert-parsing / hci-disk-vendor-lifetime 未触发
│
├─ 直接原因：LLM 未调用 sop_request_variable（零次工具调用记录）
│
├─ L2 原因：LLM 存在等价低阻力替代路径（bash_exec + 自行解读）
│   └─ ReAct 范式下，LLM 天然选择摩擦力最小的可行路径
│
├─ L3 原因：变量门禁架构对 skill_call 类变量缺乏有效约束
│   ├─ 问题 A：skill_call 不在 GUARDED_STRATEGIES 中，无硬阻断
│   └─ 问题 B：LLM 可以完全绕过 skill 体系，门禁形同虚设
│
├─ L4 原因：required_variables 的 skill_call 信息仅是内容层注释
│   └─ 内容层信息不等于行为层约束
│
└─ L5 根因（系统层）：Skill 触发机制依赖 LLM 善意配合（Trust-based）
    └─ 缺乏 Enforce-based 的系统级触发保障
```

---

## 四、业界最佳实践对标

| 机制 | 描述 | 本项目现状 |
| :--- | :--- | :--- |
| **强制 Gate（Pre-condition check）** | 每次工具调用前检查前置变量，缺失则硬拒绝并强制路由到获取工具 | Gate 只覆盖 `user_input/user_confirm/env_injection`，`skill_call` 不在其中 |
| **工作流 DAG 约束** | 通过有向无环图强制节点的依赖执行顺序，工具调用不可乱序 | 用了 ReAct+SOP 决策树，但无 DAG 级别的强执行顺序 |
| **Contextual Nudge（上下文时机引导）** | 在 LLM 最需要决策的时刻（tool_result 返回时），附带最相关的行动提示 | 当前 tool_result 不含任何 `preferred_next_actions` 字段 |
| **Prompt Engineering** | 全局提示词中声明行为规范 | 现有系统提示词未提及 `sop_request_variable` 的使用场景 |

**排序与权重**（可靠性由高到低）：

```
强制 Gate > 工作流 DAG > Contextual Nudge > Prompt Engineering
```

---

## 五、分层改进方案

### 方案 A（核心改进）：`sop_advance` 返回体嵌入 `preferred_next_steps`

**改动位置**：[`backend/agent-service/app/tools/sop/nav.py`](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/sop/nav.py)

**改动说明**：

当 `sop_advance` 或 `get_sop_node` 到达目标节点后，检查 `required_variables` 中是否存在 **未就绪的 `skill_call`/`tool_call` 类变量**。若存在，在返回的 JSON 中附加 `preferred_next_steps` 字段，明确告知 LLM 下一步的推荐行动。

```python
# nav.py 中 sop_advance 的返回体扩展

def _build_preferred_next_steps(
    node: dict, 
    variable_schema: list[dict],
    context_variables: dict
) -> list[dict]:
    """
    检测节点的 required_variables 中未就绪的 skill_call/tool_call 变量，
    生成推荐的下一步行动提示，嵌入到工具返回结果中，引导 LLM 优先
    通过 sop_request_variable 触发 Skill/Tool 自动获取，而非手动 bash_exec。
    """
    hints = []
    for var in variable_schema:
        if var.get("name") not in context_variables:
            strategy = var.get("acquisition_strategy")
            if strategy in ("skill_call", "tool_call"):
                hints.append({
                    "tool": "sop_request_variable",
                    "args": {"variable_name": var["name"]},
                    "reason": (
                        f"变量 '{var['name']}' 需通过专属 "
                        f"{'Skill' if strategy == 'skill_call' else 'Tool'} "
                        f"（{var.get('acquisition_tool', '未指定')}）自动采集，"
                        f"建议先调用 sop_request_variable 完成采集，"
                        f"避免手动执行命令引入解析偏差。"
                    ),
                    "priority": "high"
                })
    return hints

# 在 sop_advance 的返回 JSON 中追加
return {
    "ok": True,
    "current_node_id": next_node_id,
    "node_summary": ...,
    "is_leaf": is_leaf,
    # [新增字段] 当节点有未就绪的 skill/tool 变量时，显式列出推荐行动
    "preferred_next_steps": _build_preferred_next_steps(
        node, variable_schema, context_variables
    )
}
```

**效果**：当 LLM 收到 `sop_advance` 的 `tool_result` 时，`preferred_next_steps` 作为**最近行动上下文中的显式指令**出现，相比全局 Prompt 中的说明，在 LLM 决策时的权重和时效性显著更高（Contextual Nudge 原则）。

---

### 方案 B（补充强化）：将 `skill_call` 纳入变量门禁的「软推荐」层

**改动位置**：[`backend/agent-service/app/tools/sop/nav.py`](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/sop/nav.py) — `_find_missing_guarded_variables` 函数

**改动说明**：

当前变量门禁分为两层：
- **硬门禁（Guarded）**：`user_input` / `user_confirm` / `env_injection` — 缺失时强制阻断
- **无约束（Unguarded）**：其余策略 — 完全放行

建议在两者之间新增「**软推荐（Preferred）**」层，专门覆盖 `skill_call` 和 `tool_call` 类型：

```python
# 变量门禁策略分层设计

# 第一层：硬门禁（保持不变）
HARD_GUARDED_STRATEGIES = {"env_injection", "user_input", "user_confirm"}

# 第二层：软推荐（新增）
# skill_call/tool_call 类变量缺失时，不阻断执行，但在工具返回结果中注入提示
SOFT_PREFERRED_STRATEGIES = {"skill_call", "tool_call"}

def _find_missing_guarded_variables(
    node: dict,
    variable_schema: list,
    context_variables: dict
) -> tuple[list, list]:
    """
    返回 (hard_blocked, soft_preferred_hints)
    hard_blocked: 硬阻断变量列表（保持原有行为）
    soft_preferred_hints: 推荐优先采集的变量列表（新增）
    """
    hard_blocked = []
    soft_hints = []
    
    for var in variable_schema:
        if var["name"] not in context_variables:
            strategy = var.get("acquisition_strategy")
            if strategy in HARD_GUARDED_STRATEGIES:
                hard_blocked.append(var)
            elif strategy in SOFT_PREFERRED_STRATEGIES:
                soft_hints.append(var)
    
    return hard_blocked, soft_hints
```

---

### 方案 C（基础保障）：系统提示词增加 `sop_request_variable` 使用规范

**改动位置**：`database/seeds/` 中的 `s1_sop_react_new_v1` 和 `s2_sop_react_resume_v1` 提示词

**改动说明**：

在 SOP 执行阶段的系统提示词中，明确加入以下段落：

```markdown
## 变量采集规范

当你通过 `get_sop_node` 或 `sop_advance` 看到节点的 `required_variables` 中包含
`acquisition_strategy` 为 `skill_call` 或 `tool_call` 的变量时：

**必须优先调用 `sop_request_variable(variable_name=<变量名>)`** 来触发系统内置的
专属技能（Skill）或工具采集。

禁止用 `bash_exec` / `acli_exec` 等通用命令手动采集这些变量。原因：
1. 专属 Skill 包含特定厂商/型号的解析逻辑，通用命令无法覆盖所有场景
2. 手动解读输出容易引入 LLM 解析偏差，降低诊断置信度
3. 变量不进池将导致后续节点判断逻辑失效

只有当 `sop_request_variable` 返回错误（如 Skill 不可用）时，才允许降级为手动采集。
```

**注意**：此方案是最脆弱的约束手段，**必须配合方案 A 使用**，单独使用不具备可靠性。

---

## 六、改进方案优先级与实施路线

### 优先级矩阵

| 方案 | 约束强度 | 实施复杂度 | 推荐优先级 | 改动文件 |
| :--- | :--- | :--- | :--- | :--- |
| **A：`preferred_next_steps` 嵌入** | ★★★★（Contextual Nudge） | ★★（中等） | 🔴 P0 首选 | `nav.py` |
| **B：软推荐门禁层** | ★★★（补充保障） | ★★（中等） | 🟡 P1 | `nav.py` |
| **C：提示词规范** | ★★（最弱，易漂移） | ★（简单） | 🟢 P2 配合 A | seeds SQL |

### 实施路线（建议顺序）

```
Step 1: 实施方案 A — 最高性价比，直接在 tool_result 上下文中引导
       ↓
Step 2: 实施方案 C — 提示词补充，加强方案 A 的背景理解
       ↓
Step 3: 实施方案 B — 架构层完善，建立分层门禁体系
       ↓
[可选] Step 4: 未来演进 — 将 skill_call 变量引入强制 DAG 节点依赖
               （参考 LangGraph Checkpointer 范式，完全去除对 LLM 善意配合的依赖）
```

---

## 七、验证方法

改进后，可通过以下方式验证 Skill 是否被正确触发：

```sql
-- 验证 SQL：检查 dynamic_resource_usage_audit 表是否有 skill 类型记录
SELECT 
    resource_type, 
    resource_name, 
    status, 
    created_at
FROM dynamic_resource_usage_audit
WHERE conversation_id = '<目标会话ID>'
  AND resource_type = 'skill'
ORDER BY created_at ASC;
```

预期结果：
- `hci-alert-parsing` 应在 SOP 开始阶段出现（`status = 'success'`）
- `hci-disk-vendor-lifetime` 应在到达磁盘诊断叶节点后出现（`status = 'success'`）

---

## 八、延伸思考：架构演进方向

当前 Skill 系统的核心矛盾是：**Skill 是强能力（Capability），但调用机制是弱约束（Suggestion）**。

长期演进方向应遵循 **Capability vs. Permission 分离原则**（参考 OS 设计）：

```
+---------------------------+      +---------------------------+
|   SOP 决策树（Navigation） |      |   Variable Pool（变量池）  |
|                           |      |                           |
|  [节点到达]                |  →   |  [缺失变量扫描]            |
|  [节点跃迁]                |      |  [策略路由]                |
+---------------------------+      +-----------+---------------+
                                               │
                    ┌──────────────────────────┼─────────────────────────┐
                    ▼（硬门禁）                 ▼（软推荐）               ▼（自由）
             [user_input]              [skill_call]              [llm_inference]
             [user_confirm]            [tool_call]               [sop_default]
             [env_injection]
             强制 interrupt             返回 preferred_hints      直接执行，不干预
             阻断 LLM 推理              LLM 可选择遵循
```

最终理想态：`skill_call` 类变量应与 `user_input` 一样具备强制 interrupt 能力——
节点到达时自动异步触发 Skill，结果写入变量池后再解锁 LLM 下一步推理。
这样才能从根本上消除"LLM 善意配合"这一脆弱假设。

---

*文档生成时间：2026-06-20*  
*分析基准实例：工单 Q2026062036731，会话 def2eceb-c745-4a06-90f6-1dba08745238*  
*分析方法：Why-Why 5层追问 + 数据库审计表实证 + 业界范式对标*
