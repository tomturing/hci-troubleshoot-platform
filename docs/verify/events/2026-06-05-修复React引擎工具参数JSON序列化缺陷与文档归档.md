# HTP-Agent SOP 诊断链路全面根因分析报告

> 工单示例：Q2026060505671  
> 分析时间：2026-06-05 18:36  
> 问题现象：进入 SOP 诊断后 AI 持续报 `AI_UPSTREAM_ERROR` 400 / `AI_TIMEOUT`，无法走完任何一轮推理

---

## 🔴 核心结论：目前只有 1 个真正的代码 Bug，PR #397 可以修复它

截图中所有三条错误（17:55、18:28、18:30）的根因**完全相同**：

> `react_engine.py` 第 199 行将工具参数序列化为 Python 单引号格式字符串，导致上游 API 400 拒绝。

**PR #397 已经修复了这个 Bug，合并后即可解决所有错误。**

---

## 一、链路全图（从用户点击到 LLM 调用）

```
用户点击"硬盘寿命到期"分类确认
    ↓
ConversationService → AgentRouter → InvestigationAgent.process()
    ↓
kb_client.route_by_category(category="硬件-024")
    → track="sop", sop_document_id=xxx
    ↓
InvestigationAgent._process_sop_mode()
    ├─ ConversationSopClient.create()        → 写 sop_execution 表 ✅
    ├─ get_sop_node("n-1")                  → 获取根节点 ✅
    ├─ _build_sop_react_prompt()            → 构建 system prompt ✅
    └─ ReactEngine.execute(sop_mode=True)
           ↓
        第 1 步：ai_client.invoke(messages, tools)    ← LLM 返回 tool_calls ✅
        第 2 步（循环）：
            assistant_msg["arguments"] = str(tc.arguments)   ← ❌ BUG DC-02
            ai_client.invoke(messages_with_bad_json, tools)  ← ❌ 400 BadRequest
```

**REACT_ENABLED=false 只影响全局 RemediationAgent 的 ReactEngine，  
InvestigationAgent 的 SOP 模式在内部独立创建 ReactEngine，不受该配置影响。**

---

## 二、所有已识别问题的完整清单

### ✅ 已修复（PR #397，待合并）

| 编号 | 问题 | 文件 | 影响 |
|------|------|------|------|
| DC-02 | `str(tc.arguments)` 产生非法 JSON，导致第二步起每次 LLM 调用返回 400 | `react_engine.py:203` | **SOP 模式完全无法工作** |

修复内容：
```python
# 修复前（非法 JSON）
"arguments": str(tc.arguments)        # => "{'key': 'val'}"

# 修复后（合法 JSON）
"arguments": json.dumps(tc.arguments or {})  # => '{"key": "val"}'
```

---

### ⚠️ 待验证（PR #397 合并后需要人工验证的逻辑）

#### V-01：`context_variables` 环境变量注入是否生效

**链路**：
```
sop_execution.create() → conversation-service 返回 context_variables
    ↓
InvestigationAgent._process_sop_mode() L468:
    context_variables = create_result.get("context_variables", {})
    ↓
_build_sop_react_prompt(context_variables=context_variables) → 注入到 system prompt
```

**验证方法**：
```sql
-- 1. 查 sop_execution 表，确认 context_variables 字段有值
SELECT id, context_variables, status 
FROM sop_execution 
WHERE conversation_id = '<your_conversation_id>';

-- 2. 查 audit_log 表，确认 system_prompt 中包含"【已知变量】"
SELECT system_prompt_content 
FROM audit_log 
WHERE conversation_id = '<your_conversation_id>'
ORDER BY created_at
LIMIT 1;
```

**潜在风险**：如果 `conversation-service` 的 `/api/conversations/{id}/sop/create` 接口返回 `context_variables: {}` 或空，则变量注入静默失败。需要确认 conversation-service 侧的 `sop_create_execution` 接口是否正确将 env_context 写入 `context_variables`。

---

#### V-02：`env_context` 是否正确传递给 sop_execution.context_variables

**已确认：conversation-service 侧 `sop_create_execution` 接口已完整实现此逻辑：**

```python
# sop_execution.py L480-506：
# 1. 调 _environment_client.get_context_info(case_id) 获取环境信息
# 2. 遍历 variable_schema，找 env_injection 策略的变量
# 3. 调 _resolve_env_variable() 从 alert_logs/env_info 解析值
# 4. 将解析结果存入 initial_variables，创建 sop_execution 时传入
```

`_resolve_env_variable()` 支持：
- `node_ip`：从 alert_logs[0].node_ip/host/ip/target 提取
- `disk_sn`：从 alert_logs[0] 或 description 正则提取 SN 号
- `hci_version`：从 env_info 直接查找

**此路径代码正确，无需修改。**

验证方法（PR #397 合并后）：
```sql
SELECT context_variables 
FROM sop_execution 
WHERE conversation_id = '<conversation_uuid>';
-- 期望：{"node_ip": {"value": "10.x.x.x", "source": "env_injection"}, ...}
```

---

#### V-03：`sop_request_variable` 中的 `env_injection` 降级逻辑

`engine.py:182` 处：
```python
if strategy in ("env_injection", "env_context") or strategy.startswith("env:"):
    logger.warning("...降级为 user_input")
    strategy = "user_input"
```

如果 `context_variables` 里没有提前注入环境变量，LLM 调用 `sop_request_variable(variable_name="node_ip")` 时，会发现变量不存在缓存，然后去查 variable_schema，发现 strategy=`env_injection`，**降级为向用户要求手动输入**。这就是之前截图里出现"请提供 node_ip"弹窗的原因。

---

### 🟡 已知潜在隐患（不影响当前流程，但需要关注）

#### P-01：`tool_result` 内容序列化为 `str(tool_result)`

`react_engine.py:241`:
```python
"content": str(tool_result),
```

如果 `tool_result` 是一个复杂的 dict（如 `get_sop_node` 返回值），`str()` 会产生 Python 单引号格式，传给 LLM 可能造成解析困难，但不会导致 400（`tool` 角色的 `content` 字段是自由字符串，无需 JSON 格式）。**低风险，不需要立即修复。**

#### P-02：`sop_request_variable` 工具在 SOP mode 下未被注册到 TOOL_REGISTRY

查看 `tool_registry.py` 的测试环境预设（L47-48）：
```python
("get_sop_node", "sop", 1),
("sop_advance", "sop", 1),
# ← sop_request_variable 未在此处定义！
```

**生产环境的 `tool_definition` 表中需要有 `sop_request_variable` 工具定义。**  
如果没有，LLM 调用该工具时会走到 `_execute_tool_call` 第 300 行返回"未知工具"，进而造成 SOP 变量请求失败。

**验证方法**：
```sql
SELECT tool_name, description, is_active 
FROM tool_definition 
WHERE tool_name IN ('get_sop_node', 'sop_advance', 'sop_request_variable');
```

---

## 三、一次性全量修复行动清单

```
优先级 P0（阻塞性，必须先做）：
[x] 合并 PR #397 - 修复 str(tc.arguments) JSON 序列化缺陷
    → 预期效果：17:55/18:28/18:30 的所有 AI_UPSTREAM_ERROR 消失

优先级 P1（功能性，影响变量注入）：
[ ] 确认 conversation-service 的 sop_create_execution 接口
    是否将 env_context 写入 context_variables
    → 接口：POST /api/conversations/{id}/sop/create
    → 验证：查 sop_execution.context_variables 字段

优先级 P2（功能性，影响工具可用性）：
[ ] 确认生产数据库 tool_definition 表有 sop_request_variable 工具记录
    → SQL: SELECT * FROM tool_definition WHERE tool_name='sop_request_variable'
    → 若无，需插入记录

优先级 P3（观测性）：
[ ] 合并 PR #397 后，完整测试工单 Q2026060505671
    按 docs/verify/agent/htp-agent全流程测试及数据库内容校验.md 逐步验证
```

---

## 四、为什么感觉"一直在修 Bug"？

这是因为**每个 Bug 都掩盖了下一个 Bug**：

```
之前：audit_log 无数据
  → 修复了 PromptAuditService 回调注入
  → 暴露：sop_execution 无数据
  → 修复了 ConversationSopClient.create() 调用路径
  → 暴露：DC-02（str vs json.dumps）
     因为第一步 invoke() 成功（没有历史 tool_calls）
     第二步才崩溃（有了历史 tool_calls 且格式非法）
```

**DC-02 之后，链路应该已经可以跑通了。**  
`env_context` 注入问题（V-02）和 `sop_request_variable` 工具注册问题（P-02）是独立的功能性问题，不会导致崩溃，只会影响 AI 是否需要向用户追问变量。

---

## 五、合并 PR #397 后的预期行为

1. 选择分类确认 → SOP 模式启动 → `sop_execution` 表创建记录 ✅
2. 第 1 步 `invoke()` → LLM 返回 `tool_calls: [get_sop_node]` ✅  
3. 第 2 步 `invoke()` → 历史 arguments 格式正确（JSON）→ 上游不再 400 ✅
4. AI 正常推进 SOP 节点树，多轮对话完成诊断 ✅
5. `audit_log` 每轮有记录 ✅，`tool_result` 每个工具调用有记录 ✅
