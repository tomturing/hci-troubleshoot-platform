# 虚拟机开机失败 — AI 交互全流程验证方案

> 创建日期：2026-03-31
> 最后更新：2026-04-04（v6.3 更新：CP-10 补充 S6 三选项流程；CP-11 细化关单验证；新增两个状态机约束验证）
> 目的：以"虚拟机开机失败"为实例，定义覆盖全链路的检查点，明确每个节点的验证方式（数据库 + 可观测性日志）。
> 关联文档：[架构 01](../architecture/01_系统架构.md) | [AI 层 05](../architecture/05_AI助手层设计.md) | [技术方案 11](../architecture/11_完整技术方案.md) | [S0 重构方案 22](../architecture/22_S0意图识别与分类基线重构方案.md) | [状态机设计 01§9](../architecture/01_系统架构.md#9-ai-诊断状态机设计-v63)
> 问题整改：[T-流程验证问题整改任务.md](./T-流程验证问题整改任务.md)

---

## 一、全流程鸟瞰

```
用户输入 → [CP-00 工单创建] → [CP-01 会话初始化]
  → S0 [CP-02 意图识别 + 分类确认 + 工具调用] → [CP-03 S0→S1 阶段跳转]
  → S1 [CP-04 故障定位确认]
  → S2 [CP-05 假设生成]
  → S3 [CP-06 acli 工具诊断] → [CP-07 写操作人工确认]
  → S4 [CP-08 根因确认]
  → S5 [CP-09 方案输出]
  → S6 [CP-10 验证 VM 开机 + S6 三选项推送] → [CP-10A/10B/10C 用户选择处理] → [CP-11 工单关闭/回退/升级]
```

---

## 二、各检查点详情

---

### CP-00：工单创建

**触发节点**：用户在前端填写"虚拟机 vm-prod-001 开机失败"并提交。

**预期行为**：`case` 表新增一条记录，`category='vm'`，`status='created'`，`trace_id` 不为 null。

**DB 验证**：
```sql
SELECT case_id, title, status, category, assistant_type, trace_id, created_at
FROM "case"
WHERE category = 'vm'
ORDER BY created_at DESC
LIMIT 1;
```

**日志验证（Loki）**：
```logql
{service="case-service"} | json | event="case_created"
| line_format "case_id={{.case_id}} trace_id={{.trace_id}} status={{.status}}"
```

**完成标准**：
- [ ] `case` 表有记录，`category='vm'`，`status='created'`
- [ ] `trace_id` 不为 null，格式为 32 位 hex
- [ ] Loki 中有对应 `case_created` 日志，`trace_id` 与 DB 一致

> ✅ **已由云端修复**（#88）：`conversation.category_id` / `category_l1` / `category_l2` 由 S0 分类确认流程自动写入，不再依赖前端传入 `case.category`。T-FIX-05 BUG-01 **已无需实现**。

---

### CP-01：会话初始化

**触发节点**：工单确认后，前端发起第一条消息，`conversation` 记录创建。

**预期行为**：`conversation` 表新增记录，`diagnostic_stage='S0'`，`assistant_type` 与工单一致。

**DB 验证**：
```sql
SELECT conversation_id, case_id, diagnostic_stage, assistant_type, trace_id, created_at
FROM conversation
WHERE case_id = '{上一步的 case_id}';
```

**日志验证（Loki）**：
```logql
{service="conversation-service"} | json | event="conversation_created"
| line_format "conv_id={{.conversation_id}} case_id={{.case_id}} assistant={{.assistant_type}}"
```

**Trace 验证（Tempo）**：用 `trace_id` 在 Tempo 检索，应看到：
```
api-gateway POST /api/conversations/
  └─ conversation-service 路由处理
      └─ SQLAlchemy INSERT conversation
```

**完成标准**：
- [ ] `conversation.diagnostic_stage = 'S0'`
- [ ] `conversation.trace_id` 与 `case.trace_id` 属于同一条 Trace（前 16 位相同）
- [ ] Tempo 瀑布图中有 INSERT conversation 的 Span

> ⚠️ 当前阻塞：`REACT_ENABLED` 默认 False，ReactExecutor 从未初始化 → 见整改任务 **T-FIX-01**（BUG-02）

---

### CP-02：S0 意图识别 — 分类确认 + 工具调用

**触发节点**：AI 收到第一条消息，进入 S0 专用 Prompt 路径（`_build_s0_system_prompt`）。S0 阶段**禁止** KB/SOP 检索，改为注入 198 个分类列表 + 环境/告警/任务上下文，由 LLM 输出「已确认故障分类：{code} {name}」标记。`ReactExecutor.run()` 同时自动调用三个只读工具获取环境快照。

**工具调用序列**：
| 步骤 | 工具名 | 参数示例 | risk_level | policy |
|------|--------|----------|------------|--------|
| 1 | `get_active_alerts` | `{"limit": 10}` | 1 | auto |
| 2 | `get_failed_tasks` | `{"task_type": "启动虚拟机"}` | 1 | auto |
| 3 | `get_vm_list` | `{"name_filter": "vm-prod-001"}` | 1 | auto |

**DB 验证**：
```sql
SELECT tool_name, risk_level, policy, duration_ms, error, started_at
FROM tool_audit_log
WHERE session_id = '{conversation_id}'
ORDER BY started_at;
-- 预期：3 条记录，全部 risk_level=1，error=NULL
```

**日志验证（Loki）**：
```logql
{service="conversation-service"} | json | event=~"tool_called|tool_result"
| line_format "step={{.step_no}} tool={{.tool_name}} duration={{.duration_ms}}ms error={{.error}}"
```

**完成标准**：
- [ ] `tool_audit_log` 有 3 条记录，`error=NULL`
- [ ] `get_failed_tasks` 返回结果包含 vm-prod-001 相关失败任务
- [ ] `get_vm_list` 返回结果确认 VM 存在
- [ ] 所有工具 `duration_ms < 5000`
- [ ] LLM 回复中出现「已确认故障分类：虚拟机-003 虚拟机开机失败」标记
- [ ] `conversation.category_id = '虚拟机-003'`，`category_l1 = '虚拟机'`（S0→S1 跳转后异步写入）

**新增 DB 验证 — 分类写入**：
```sql
SELECT conversation_id, category_id, category_l1, category_l2, diagnostic_stage
FROM conversation WHERE case_id = '{case_id}';
-- 预期：category_id='虚拟机-003', category_l2='虚拟机开机失败'

SELECT code, label, hit_count FROM kb_category WHERE code = '虚拟机-003';
-- 预期：hit_count 较上次 +1
```

> ⚠️ 整体依赖 T-FIX-01（BUG-02）修复；`step_no` 字段依赖 T-FIX-05（BUG-03）

---

### CP-03：S0 → S1 阶段跳转

**触发节点**：AI 回复命中 `STAGE_TRIGGERS[("S0","S1")]` 中的正则，`ConversationManager.detect_stage_transition` 返回 `"S1"`。

**触发机制**（#88 重构后）：S0→S1 跳转使用 `detect_stage_transition_with_category()` 增强版，同时提取分类信息：
- 原正则触发词（`STAGE_TRIGGERS["S0","S1"]`）仍然有效
- **新增**：LLM 回复出现「已确认故障分类：虚拟机-003 虚拟机开机失败」时，`extract_category()` 解析 `{code, name}`，后台异步写入 `conversation.category_id` / `category_l1` / `category_l2`

**DB 验证**：
```sql
SELECT diagnostic_stage, updated_at
FROM conversation
WHERE case_id = '{case_id}';
-- 预期：diagnostic_stage = 'S1'
```

**日志验证（Loki）**：
```logql
{service="conversation-service"} | json | event="diagnostic_stage_transition"
| line_format "from={{.from_stage}} to={{.to_stage}} db_committed={{.db_committed}}"
```

**完成标准**：
- [ ] `conversation.diagnostic_stage = 'S1'`
- [ ] Loki 中有 `diagnostic_stage_transition` 事件，`db_committed=true`
- [ ] Loki 中有 `s0_category_extracted` 事件，`code` 字段非空
- [ ] `conversation.category_id` 已被写入（异步后台任务完成）

> ⚠️ `db_committed` 字段依赖 T-FIX-03（BUG-04）修复后才存在

---

### CP-04：S1 故障定位 — 追问对话轮次

**触发节点**：AI 向用户追问 VM 所在集群、故障发生时间等信息，用户补充回答。

**预期行为**：`message` 表出现 `role='assistant'` 追问消息和 `role='user'` 补充消息各至少一条。AI 在 S1 阶段的 Prompt 中能看到已收集的已知信息（`{known_info}` 已被替换）。

**DB 验证**：
```sql
SELECT role, LEFT(content, 200) AS content_preview, created_at
FROM message
WHERE conversation_id = '{conversation_id}'
ORDER BY created_at;
```

**完成标准**：
- [ ] 至少一条 assistant 消息含明显疑问句（含"哪个"/"是否"/"什么时间"等）
- [ ] 用户补充消息保存成功，`role='user'`
- [ ] AI 追问中不出现 `{known_info}` 原始字符串

> ✅ **BUG-05 已由 #88 修复**：`_segment_methodology` 现已执行 `.format()` 替换，`known_info` 从 `session_state` dict 填充，S1 阶段 Prompt 不再出现原始占位符。

---

### CP-05：S2 假设生成

**触发节点**：AI 回复列出 2-3 个带概率的根因假设，阶段跳转至 S3。

**预期假设内容**（基于 `_MECHANISM_KNOWLEDGE` 硬编码知识）：
| 假设 | 预估概率 | 验证工具 |
|------|---------|---------|
| 宿主节点内存不足 | 50% | `acli_system_top` |
| 存储路径不可访问 | 30% | `acli_storage_path_list` |
| License 授权问题 | 20% | `get_cluster_detail` |

**DB 验证**：
```sql
SELECT content FROM message
WHERE conversation_id = '{conversation_id}'
AND role = 'assistant'
AND content LIKE '%假设%'
ORDER BY created_at DESC
LIMIT 1;
```

**完成标准**：
- [ ] AI 回复包含至少 2 个假设，每个假设有概率标注
- [ ] `diagnostic_stage` 随后跳转到 `S3`

> ✅ **BUG-05 已由 #88 修复（S1/S2 的 known_info/category_path 已正常替换）**。
> ⚠️ **BUG-06 部分残留**：`hypothesis` 写入 `conversation.metadata` 的时机（S2 完成后）尚未实现，S3 阶段 `{hypothesis}` 仍会被填入 `[]`（列表 str）而非结构化文本 → 见整改任务 **T-FIX-02**

---

### CP-06：S3 验证执行 — acli 深度诊断

**触发节点**：AI 按假设概率依次调取 acli 工具收集证据。

**典型工具序列**（内存不足假设优先）：
1. `acli_system_top`（查 node-02 CPU/内存实时占用）
2. `acli_vm_get`（确认 vm-prod-001 的 memory 申请量）

**DB 验证**：
```sql
SELECT tool_name, tool_args, step_no, LEFT(result::text, 300) AS result_preview,
       duration_ms, error
FROM tool_audit_log
WHERE session_id = '{conversation_id}'
AND tool_name LIKE 'acli%'
ORDER BY step_no;
```

**日志验证（Loki）**：
```logql
{service="conversation-service"} | json | event="tool_called"
| where category = "acli"
| line_format "step={{.step_no}} tool={{.tool_name}} args={{.tool_args}}"
```

**Trace 验证（Tempo）**：acli 工具每次调用应产生独立 Span，可观测 SSH/SCP 调用耗时。

**完成标准**：
- [ ] `tool_audit_log` 有 `acli_system_top` 记录，`result` 包含节点内存数据
- [ ] `step_no` 字段正确递增，与 ReAct 步骤一致
- [ ] 无 `error` 记录

> ⚠️ 依赖 T-FIX-01（BUG-02，ReactExecutor 启动）；`{hypothesis}` 替换依赖 T-FIX-02（BUG-06）；`step_no` 依赖 T-FIX-05（BUG-03）

---

### CP-07：写操作人工确认（vm_migrate）

**触发节点**：AI 判断需迁移 VM，调用 `vm_migrate`（`risk_level=2`），触发人工确认流程。

**预期行为**：
1. `ReactExecutor._execute_tool_call` 推送 `confirm_request` SSE 事件
2. 前端弹出确认弹窗，用户点击"确认"
3. Redis BRPOP 收到确认信号，继续执行工具
4. `tool_audit_log.authorized_by` 记录操作用户 ID

**DB 验证**：
```sql
SELECT tool_name, risk_level, authorized_by, error,
       started_at, completed_at, duration_ms
FROM tool_audit_log
WHERE session_id = '{conversation_id}'
AND risk_level >= 2
ORDER BY started_at;
```

**日志验证（Loki）**：
```logql
{service="conversation-service"} | json | event=~"confirm_requested|confirm_received|tool_confirm_declined"
| line_format "tool={{.tool_name}} authorized_by={{.authorized_by}} reason={{.reason}}"
```

**完成标准**：
- [ ] 确认通过：`authorized_by` 非 null，`error` 为 null，`duration_ms` > 5s
- [ ] 用户拒绝：`error='user_rejected'`，工具结果为拒绝提示
- [ ] 等待超时：`error='confirm_timeout'`，`authorized_by='system-timeout'`

> ⚠️ 拒绝/超时时审计字段缺失 → 见整改任务 **T-FIX-04**（BUG-07）

---

### CP-08：S4 根因确认

**触发节点**：AI 收集到决定性证据，回复包含"根因确认："关键词，阶段跳转至 S4。

**DB 验证**：
```sql
SELECT m.content, c.diagnostic_stage
FROM message m
JOIN conversation c ON m.conversation_id = c.conversation_id
WHERE m.conversation_id = '{conversation_id}'
AND m.role = 'assistant'
ORDER BY m.created_at DESC
LIMIT 1;
-- 预期：content 含"根因确认："，diagnostic_stage='S4'
```

**完成标准**：
- [ ] AI 回复明确包含 `根因确认：` 格式表述
- [ ] `conversation.diagnostic_stage = 'S4'`

---

### CP-09：S5 方案输出

**触发节点**：AI 输出结构化修复方案，区分快速恢复和彻底解决两条路径。

**预期内容**：
```
快速恢复方案：
  1. 将 vm-prod-001 迁移至 node-03（内存剩余 12GB）[需人工确认]
  2. 迁移完成后，重新执行开机

彻底解决方案：
  1. 扩容 node-02 内存，或迁移低优先级 VM 释放资源
```

**DB 验证**：
```sql
SELECT LEFT(content, 500) FROM message
WHERE conversation_id = '{conversation_id}'
AND role = 'assistant'
AND content LIKE '%快速恢复%'
ORDER BY created_at DESC
LIMIT 1;
```

**完成标准**：
- [ ] 方案包含"快速恢复"和"彻底解决"两段
- [ ] Level-2 操作有明确风险提示
- [ ] AI 回复中不出现 `{root_cause}` 原始字符串

> ✅ **BUG-05/06 中的 root_cause 替换已由 #88 修复**：`_segment_methodology` 现已执行 `.format(root_cause=...)`。
> ⚠️ `root_cause` 的写入时机（S4 确认后回写 `conversation.metadata`）**仍需 T-FIX-02 补充**，否则 S5 阶段看到的 root_cause 为 "待确认"。

---

### CP-10：S6 验证 VM 开机 + 三选项推送

**触发节点**：用户执行方案后回报结果，AI 主动调用 `get_vm_list` 验证 VM 状态，验证通过后向用户推送三选项。

**DB 验证（工具调用）**：
```sql
SELECT result, started_at
FROM tool_result
WHERE conversation_id = '{conversation_id}'
AND tool_name = 'get_vm_list'
ORDER BY started_at DESC
LIMIT 1;
-- 预期：result 中 vm-prod-001 的 power_state='on'
```

**DB 验证（三选项推送）**：
```sql
SELECT pending_resolution, diagnostic_stage
FROM conversation
WHERE conversation_id = '{conversation_id}';
-- 预期：pending_resolution IS NOT NULL，stage='S6'
-- 格式示例：{"stage":"S6","sent_at":"2026-04-04T10:00:00Z","options":["A","B","C"]}
```

**约束验证（pending 互斥）**：
```sql
-- 验证 pending_confirm 和 pending_resolution 不会同时存在
SELECT pending_confirm, pending_resolution
FROM conversation WHERE conversation_id = '{conversation_id}';
-- 预期：两者至多一个非 NULL
```

**日志验证（Loki）**：
```logql
{service="conversation-service"} | json | event="s6_resolution_options_sent"
| line_format "conv={{.conversation_id}} sent_at={{.sent_at}}"
```

**完成标准**：
- [ ] `tool_result` 有 `get_vm_list` 记录，`result` 中 vm-prod-001 `power_state='on'` 或等同字段
- [ ] AI 回复包含"问题是否已解决？"和 A/B/C 三个选项
- [ ] `conversation.pending_resolution` 非 NULL，包含正确的 `sent_at` 和 `options`
- [ ] `conversation.pending_confirm` 为 NULL（两者互斥）
- [ ] Loki 中有 `s6_resolution_options_sent` 事件

---

### CP-10A：用户选 A（已解决）

**触发节点**：前端收到 S6 三选项后用户点击"A. 是，问题已解决"。

**DB 验证**：
```sql
SELECT status, resolved_at, pending_resolution,
       EXTRACT(EPOCH FROM (resolved_at - confirmed_at))/60 AS ai_diagnostic_minutes
FROM "case" c
JOIN conversation cv ON c.case_id = cv.case_id
WHERE c.case_id = '{case_id}';
-- 预期：status='resolved'，resolved_at 非 NULL，pending_resolution=NULL，ai_diagnostic_minutes > 0
```

**完成标准**：
- [ ] `case.status = 'resolved'`，`resolved_at` 有值（SLA 指标可计算）
- [ ] `conversation.pending_resolution = NULL`（等待快照已清空）
- [ ] Scheduler 队列中有 Pod 回收任务（Scheduler Service 日志）

---

### CP-10B：用户选 B（未解决 + 新报错）

**触发节点**：用户点击"B. 否，还有新报错"，AI 重新进入 S1。

**DB 验证**：
```sql
-- 验证旧 diagnostic_item 已归档
SELECT type, status, COUNT(*) FROM diagnostic_item
WHERE conversation_id = '{conversation_id}'
GROUP BY type, status;
-- 预期：之前的 hypothesis/root_cause/solution 状态全部为 'archived'

-- 验证阶段已回退
SELECT diagnostic_stage, pending_resolution
FROM conversation WHERE conversation_id = '{conversation_id}';
-- 预期：diagnostic_stage='S1'，pending_resolution=NULL
```

**完成标准**：
- [ ] 本次诊断周期的所有 `diagnostic_item` 状态已更新为 `archived`
- [ ] `conversation.diagnostic_stage = 'S1'`
- [ ] AI 回复提示用户描述新报错："请描述新出现的错误信息"
- [ ] `case.status` 仍为 `confirmed`（B 不改变业务状态）

---

### CP-10C：用户选 C（升级人工）

**触发节点**：用户点击"C. 需要人工支持"。

**DB 验证**：
```sql
SELECT status, close_reason, pending_resolution
FROM "case" WHERE case_id = '{case_id}';
-- 预期：status='in_progress'，close_reason='escalated'，pending_resolution=NULL
```

**完成标准**：
- [ ] `case.status = 'in_progress'`
- [ ] `case.close_reason = 'escalated'`
- [ ] `conversation.pending_resolution = NULL`
- [ ] AI 停止推理（SSE 推送结束消息）

---

---

### CP-11：工单关闭（用户选 A 后 Pod 回收完成）

**触发节点**：Scheduler Service 完成 Pod 回收，回调 case-service，`case.status` 自动流转至 `closed`。

**DB 验证**：
```sql
SELECT case_id, status, resolved_at, closed_at,
       EXTRACT(EPOCH FROM (resolved_at - confirmed_at))/60 AS ai_diagnostic_minutes,
       EXTRACT(EPOCH FROM (closed_at - resolved_at))/60 AS pod_cleanup_minutes
FROM "case"
WHERE case_id = '{case_id}';
-- 预期：status='closed'，resolved_at 和 closed_at 均非 NULL
```

**日志验证（Loki）**：
```logql
{service="case-service"} | json | event="case_status_changed"
| where new_status = "closed"
| line_format "case={{.case_id}} trace={{.trace_id}} ai_minutes={{.ai_diagnostic_minutes}}"
```

**完成标准**：
- [ ] `case.status = 'closed'`（终态）
- [ ] `case.resolved_at` 和 `case.closed_at` 均有值
- [ ] `ai_diagnostic_minutes = (resolved_at - confirmed_at) / 60` 合理（> 0，< 120）
- [ ] 状态转换由 Scheduler 触发（Loki 中 trace 来源为 scheduler-service）

**注意**：`resolved → closed` 只允许 Scheduler Service 发起（内部 token 鉴权），case-service 对外 API 禁止此转换。

---

## 三、检查点汇总表

| 编号 | 检查点 | 阶段 | 主要数据源 | 核心验证字段 | 整改任务 |
|------|--------|------|-----------|-------------|---------|
| CP-00 | 工单创建 | 前置 | `case` 表 | `status='created'`, `trace_id` | ~~T-FIX-05 BUG-01~~ ✅ #88；T-FIX-05 BUG-03/08 |
| CP-01 | 会话初始化 | 前置 | `conversation` 表 | `diagnostic_stage='S0'` | T-FIX-01 |
| CP-02 | S0 分类确认 + 工具调用 | S0 | `tool_audit_log` + `conversation` + Loki | 3 条 `risk_level=1`，`category_id`，`hit_count` | **T-FIX-01**（阻塞） |
| CP-03 | S0→S1 跳转 | S0→S1 | `conversation` 表 + Loki | `diagnostic_stage='S1'`，`s0_category_extracted=true`，`category_id` 已写入 | T-FIX-03 |
| CP-04 | 故障定位对话 | S1 | `message` 表 | AI 追问，无原始占位符 | ~~T-FIX-02 BUG-05~~ ✅ #88 |
| CP-05 | 假设生成 | S2 | `message` 表 | AI 回复含假设+概率 | **T-FIX-02**（BUG-06 hypothesis 写回残留） |
| CP-06 | acli 工具诊断 | S3 | `tool_audit_log` + Tempo | acli 有结果，`step_no` 递增 | **T-FIX-01 + T-FIX-02(BUG-06)** |
| CP-07 | 写操作人工确认 | S3 | `tool_audit_log` + Loki | `authorized_by` / `error` 字段完整 | 🔄 T-FIX-04 本地已完成 |
| CP-08 | 根因确认 | S4 | `message` + `conversation` | 消息含"根因确认："，`stage='S4'` | — |
| CP-09 | 方案输出 | S5 | `message` 表 | 含"快速恢复"，无原始占位符 | **T-FIX-02**（BUG-06 root_cause 写回残留） |
| CP-10 | S6 验证 + 三选项推送 | S6 | `tool_result` + `conversation` | `get_vm_list` 结果，`pending_resolution` 非 NULL | — |
| CP-10A | 用户选 A（已解决）| S6 后 | `case` + `conversation` | `status='resolved'`，`resolved_at` 有值，`pending_resolution=NULL` | — |
| CP-10B | 用户选 B（未解决回退）| S6 后 | `diagnostic_item` + `conversation` | `diagnostic_item` 全 archived，`stage='S1'` | — |
| CP-10C | 用户选 C（升级人工）| S6 后 | `case` | `status='in_progress'`，`close_reason='escalated'` | — |
| CP-11 | 工单关闭（Pod 回收后）| 后置 | `case` + Loki | `status='closed'`，`closed_at` 有值，由 Scheduler 触发 | — |

---

## 四、整改优先顺序

详细任务定义见 [T-流程验证问题整改任务.md](./T-流程验证问题整改任务.md)。

| 优先级 | 任务 | 状态 | 影响检查点 | 说明 |
|--------|------|------|-----------|------|
| **P0** | T-FIX-01：REACT_ENABLED 注入 | ⬜ 待实现 | CP-02、CP-06 整体失效 | Helm chart 未注入环境变量 |
| **P0** | T-FIX-02：Prompt 占位符写回 | ⬜ 范围缩小 | CP-05、CP-06、CP-09 | #88 已修复 BUG-05；剩余 BUG-06：hypothesis(S2↓S3)/root_cause(S4↓S5) 未写回 `conversation.metadata` |
| P1 | T-FIX-03：diagnostic_stage 一致性 | ⬜ 待实现 | CP-03 | DB 提交前不更新内存状态 |
| P1 | T-FIX-04：审计日志完整性 | 🔄 本地已完成（未提交） | CP-07 | ConfirmResult 枚举 + 拒绝/超时审计写入 |
| P2 | T-FIX-05：可观测性补全 | ⬜ 范围缩小 | CP-00、CP-02、CP-06 | #88 已修复 BUG-01；剩余 BUG-03(step_no) + BUG-08(audit_meta 持久化) |
