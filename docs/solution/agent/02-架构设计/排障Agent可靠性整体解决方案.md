# 排障 Agent 可靠性整体解决方案

> 本文面向 HCI 智能排障平台中长期 Agent 架构演进，目标不是修复某一个 `tool_call`、某一个交互卡片或某一个 Prompt，而是从第一性原理出发，系统性解决排障过程中信息不准确、工具调用不稳定、模型幻觉三类根问题。

## 1. 背景与问题定义

当前排障 Agent 的核心风险集中在三类问题：

1. 信息不准确：环境数据、用户描述、工具输出、模型总结和历史 SOP 变量混在同一段上下文里，缺少统一来源、时效性和置信度表达。
2. `tool_call` 不稳定：工具调用更像一次临时交互，而不是可恢复、可审计、可幂等、可回放的执行事务。
3. 模型幻觉：模型可以直接给出根因和修复建议，但系统没有强制它把结论绑定到事实、证据和可验证动作上。

这三类问题表面上分别属于数据、工具和模型，底层根因是一致的：系统没有把事实源、推理器、执行器、授权器、校验器、状态机的职责边界切清楚。

因此，整体目标应从“让模型更聪明”转为“让平台更可靠”。LLM 只负责提出假设、计划和解释，平台负责事实、工具、状态、权限、校验和审计。

## 2. 第一性原则

### 2.1 事实必须有来源

任何用于诊断的信息都必须落成可引用事实，而不是直接拼进 Prompt。

每条事实至少包含：

```text
Fact {
  fact_id
  case_id
  type
  source
  raw_ref
  normalized_value
  confidence
  freshness
  collected_at
  trace_id
}
```

模型不能直接相信“上下文文本”，只能引用事实集合。若不同来源给出冲突信息，系统应保留冲突并显式标注，而不是覆盖成单一值。

### 2.2 工具调用必须是事务

每一次工具调用都必须具备事务身份和生命周期。

```text
proposed -> schema_validated -> policy_checked -> authorized
-> leased -> executing -> observed -> verified -> committed | failed | cancelled
```

工具调用不能只靠 session 级 Redis key 或前端按钮维持状态。确认、执行、结果、审计、回放都必须按 `exec_id` 串联。

### 2.3 结论必须可证伪

所有关键诊断结论都必须输出：

- 假设是什么
- 支持证据是什么
- 反证是什么
- 仍缺失什么信息
- 下一步验证动作是什么

没有证据链的判断不能进入最终报告，只能作为“待验证假设”展示。

### 2.4 Agent 应是状态图，不是聊天循环

排障不是单轮问答，而是长事务流程。S0-S6 应成为显式状态图，并支持 checkpoint、恢复、人工介入和回放。

推荐将状态建模为：

```text
S0 Intent
S1 Evidence Plan
S2 Evidence Collection
S3 Hypothesis Generation
S4 Hypothesis Verification
S5 Remediation Plan
S6 Closure & Knowledge Capture
```

`tool_call`、`tool_result`、`interactive_request` 等事件不能污染诊断阶段状态。领域状态与 UI 事件必须分流。

### 2.5 模型输出必须结构化

所有可执行意图、分类结果、假设、工具参数、修复计划都应使用严格 schema。Prompt 只负责语义指导，schema 负责契约边界。

推荐所有 Agent 关键节点输出结构化对象：

```text
{
  hypotheses: [],
  evidence_needed: [],
  tool_requests: [],
  unsupported_claims: [],
  user_questions: [],
  next_state: ""
}
```

## 3. 目标架构

整体架构升级为 Evidence-Centric Troubleshooting Agent Platform，即以证据为中心的排障 Agent 平台。

```text
用户问题
  -> Evidence Builder
  -> Agent State Graph
  -> Structured Reasoner
  -> Tool Transaction Manager
  -> Claim Verifier
  -> Evidence-grounded Response
  -> Knowledge Capture
```

平台由 6 个平面组成。

## 4. Evidence Plane：事实与证据平面

### 4.1 职责

Evidence Plane 负责把所有输入转换为可追踪事实：

- 用户描述
- 工单元数据
- 环境采集数据
- 告警
- 任务日志
- 工具输出
- SOP 变量
- 历史 KBD 案例
- 人工反馈

### 4.2 核心模型

```text
Fact {
  fact_id: string
  case_id: string
  type: cluster | alert | task | metric | log | user_statement | tool_observation | sop_variable
  source: user | case_service | tool | kb | agent | manual
  raw_ref: string
  normalized_value: object
  confidence: float
  freshness: fresh | stale | unknown
  collected_at: datetime
  trace_id: string
}

EvidenceBundle {
  bundle_id: string
  case_id: string
  purpose: intent_classification | hypothesis_verification | remediation
  facts: FactRef[]
  conflicts: Conflict[]
  missing_required_facts: string[]
}
```

### 4.3 关键机制

1. 原始数据与标准化数据双轨保存。
2. Prompt 不直接注入大段 raw JSON，而是注入按目的检索出的 Evidence Bundle。
3. 所有环境变量注入从 Fact Store 查询，不直接读任意上下文字典。
4. 对过期事实做显式降权或重新采集。
5. 对同一字段多来源冲突做 conflict 标记，由 Agent 发起验证动作。

### 4.4 解决的信息不准确问题

| 问题 | 机制 |
|---|---|
| 原始环境数据丢关键信息 | raw_ref 保留原始数据 |
| 标准化字段映射错误 | normalized_value 可重算，raw_ref 可追溯 |
| Prompt 被噪声淹没 | Evidence Bundle 按目的裁剪 |
| 事实过期 | freshness 和 collected_at 显式参与推理 |
| 多来源冲突 | conflict 不被覆盖，进入验证流程 |

## 5. Tool Plane：工具事务平面

### 5.1 职责

Tool Plane 负责把工具调用从“模型生成的一次动作”升级为“平台管理的一次事务”。

### 5.2 工具定义

```text
ToolSpec {
  name: string
  input_schema: JSONSchema
  output_schema: JSONSchema
  risk_level: 0 | 1 | 2 | 3
  policy: auto | notify | confirm | block
  timeout_sec: int
  idempotency_key_template: string
  side_effect: none | read | write | destructive
  required_evidence: string[]
  postcondition_checker: string
}
```

### 5.3 执行实例

```text
ToolExecution {
  exec_id: string
  case_id: string
  conversation_id: string
  tool_name: string
  input_hash: string
  input_snapshot: object
  status: proposed | schema_validated | policy_checked | authorized | leased | executing | observed | verified | committed | failed | cancelled
  risk_level: int
  authorization_id: string | null
  result_snapshot: object | null
  error: string | null
  idempotency_key: string
  trace_id: string
  created_at: datetime
  updated_at: datetime
}
```

### 5.4 生命周期

```text
LLM ToolRequest
  -> schema validation
  -> policy gate
  -> authorization gate
  -> execution lease
  -> external execution
  -> result schema validation
  -> postcondition check
  -> observation fact creation
  -> verifier feedback
```

### 5.5 授权模型

授权必须绑定 `exec_id` 和输入快照。

```text
Authorization {
  auth_id: string
  exec_id: string
  actor: string
  decision: approved | rejected | expired
  risk_level: int
  tool_input_hash: string
  expires_at: datetime
  reason: string
  trace_id: string
}
```

自动执行也必须是服务端策略，而不是浏览器本地代点确认。前端可以选择模式，但后端必须根据用户、工单、工具风险、失败次数、环境和组织策略二次判定。

### 5.6 幂等与恢复

1. 读操作天然可重试。
2. 写操作必须声明 idempotency key。
3. 不可幂等写操作必须强制人工确认，并记录补偿建议。
4. 断线恢复时根据 `ToolExecution.status` 继续，不重复执行已提交副作用工具。
5. 工具结果必须进入 Fact Store，成为 `tool_observation`。

## 6. Agent Graph Plane：状态图平面

### 6.1 状态模型

```text
AgentRunState {
  run_id
  case_id
  conversation_id
  diagnostic_stage
  active_hypotheses
  evidence_bundle_refs
  pending_tool_executions
  pending_human_inputs
  verified_claims
  unresolved_conflicts
}
```

### 6.2 事件分流

必须拆分事件类型：

```text
DiagnosticStageEvent {
  from_stage
  to_stage
  reason
}

ToolEvent {
  exec_id
  tool_name
  status
  payload
}

InteractiveEvent {
  request_id
  kind
  status
  payload
}

EvidenceEvent {
  fact_id
  action
  payload
}
```

禁止把 `tool_call`、`tool_result`、`thinking` 当作诊断阶段写入 S0-S6。

### 6.3 节点职责

| 阶段 | 职责 | 允许输出 |
|---|---|---|
| S0 Intent | 分类和澄清 | 分类候选、用户问题、S1 进入条件 |
| S1 Evidence Plan | 制定证据采集计划 | evidence_needed、tool_requests |
| S2 Evidence Collection | 执行只读采集 | tool_requests、facts |
| S3 Hypothesis Generation | 形成候选根因 | hypotheses |
| S4 Hypothesis Verification | 验证或反驳假设 | verified_claims、missing_facts |
| S5 Remediation Plan | 生成修复计划 | remediation_steps、risk |
| S6 Closure | 总结与知识沉淀 | final_report、kb_candidate |

## 7. Reasoning Plane：受约束推理平面

### 7.1 推理输出 Schema

```text
ReasoningOutput {
  summary: string
  hypotheses: Hypothesis[]
  evidence_needed: EvidenceNeed[]
  tool_requests: ToolRequest[]
  unsupported_claims: Claim[]
  user_questions: UserQuestion[]
  next_state: string
}

Hypothesis {
  hypothesis_id: string
  statement: string
  confidence: float
  supporting_fact_ids: string[]
  contradicting_fact_ids: string[]
  verification_plan: string[]
}
```

### 7.2 推理约束

1. 不允许直接输出不可追踪的根因。
2. 不允许在缺少事实时伪造环境字段。
3. 不允许把工具失败解释成业务事实。
4. 不允许跳过验证直接进入修复。
5. 不允许推荐高风险操作而不标记风险。

### 7.3 反幻觉机制

模型输出后进入 Claim Verifier。

```text
ClaimVerification {
  claim_id
  claim_text
  status: supported | contradicted | insufficient_evidence
  supporting_fact_ids
  contradicting_fact_ids
  required_next_action
}
```

最终报告只允许包含 `supported` 和明确标注为 `insufficient_evidence` 的内容。

## 8. Policy & HITL Plane：策略与人工介入平面

### 8.1 策略职责

策略层统一处理：

- 工具风险等级
- 用户权限
- 自动执行边界
- 命令频率限制
- 高危操作拦截
- 人工审批
- 超时和取消

### 8.2 自动执行原则

自动执行只允许满足所有条件的工具：

1. `risk_level <= 1`
2. `side_effect = none`
3. input schema 校验通过
4. 工具定义允许自动执行
5. 当前会话未触发失败熔断
6. 当前环境允许自动执行
7. 服务端策略允许该用户/工单执行

`risk_level = 2` 的工具即使前端处于 aggressive 模式，也应进入服务端策略评估，并记录授权。

### 8.3 人工介入

人工介入不是异常路径，而是长流程 Agent 的正常控制点：

- 信息缺失时请求用户补充
- 工具风险较高时请求授权
- 事实冲突时请求用户确认
- 模型置信度不足时升级人工

所有人工输入都进入 Fact Store，类型为 `user_statement` 或 `manual_decision`。

## 9. Observability & Eval Plane：可观测与评测平面

### 9.1 Trace 结构

每个排障 run 应串联以下 span：

```text
case.created
conversation.message
agent.node.s0_intent
evidence.bundle.build
llm.invoke
tool.propose
tool.execute
tool.verify
claim.verify
response.render
knowledge.capture
```

### 9.2 核心指标

| 指标 | 含义 |
|---|---|
| intent_accuracy | S0 分类准确率 |
| evidence_coverage | 关键结论证据覆盖率 |
| unsupported_claim_rate | 无证据结论率 |
| tool_success_rate | 工具执行成功率 |
| tool_timeout_rate | 工具超时率 |
| authorization_failure_rate | 授权失败率 |
| hallucination_rate | 评测集幻觉率 |
| human_handoff_rate | 人工接管率 |
| mean_steps_to_resolution | 平均解决步数 |

### 9.3 回归评测集

建立黄金工单集，每条包含：

- 用户原始描述
- 环境事实
- 期望分类
- 期望工具调用序列
- 允许的根因集合
- 禁止出现的幻觉结论
- 期望最终报告结构

任何 Prompt、工具 schema、Agent Graph、变量策略变更，都必须跑回归评测。

## 10. 三大难题的对应解法

### 10.1 信息不准确

目标：从“文本上下文驱动”变成“事实证据驱动”。

关键改造：

1. 建 Fact Store。
2. 环境数据 raw/normalized 双轨。
3. 所有变量注入从 Fact Store 查询。
4. Evidence Bundle 按目的生成。
5. 结论引用 fact_id。
6. 冲突信息进入验证流程。

### 10.2 `tool_call` 不稳定

目标：从“一次临时调用”变成“可恢复事务”。

关键改造：

1. `exec_id` 成为唯一事务 ID。
2. `tool_execution` 表持久化生命周期。
3. 授权绑定 `exec_id + input_hash`。
4. Redis key 使用 `exec_id` 级隔离。
5. 工具 input/output schema 化。
6. 副作用工具幂等化或强制人工确认。
7. 工具结果进入 Fact Store。

### 10.3 模型幻觉

目标：从“模型直接回答”变成“模型提出可验证假设”。

关键改造：

1. 所有关键节点结构化输出。
2. Hypothesis 必须带 supporting/contradicting facts。
3. Claim Verifier 拦截无证据结论。
4. 最终报告区分已证实、强假设、弱假设、待采集信息。
5. 建黄金工单评测集，持续量化幻觉率。

## 11. 分阶段落地路线

### 阶段一：事件与工具事务地基

目标：先让工具链路稳定、可审计、可恢复。

任务：

1. 定义 `AgentEvent v2`，拆分诊断阶段事件、工具事件、交互事件、证据事件。
2. 新增 `tool_execution` 和 `authorization` 数据模型。
3. 所有工具调用改用 `exec_id` 级确认与结果回传。
4. `tool_confirm` 请求带 `exec_id`、`input_hash`、`expires_at`。
5. 自动执行从前端本地开关升级为服务端策略判定。
6. 工具结果落库并可回放。

验收：

- 刷新页面后 pending/running 工具状态可恢复。
- 同一 session 并发两个确认不会串线。
- 高风险命令不可被自动执行。
- 每次工具执行可通过 `trace_id + exec_id` 查全链路。

### 阶段二：事实与证据体系

目标：解决信息不准确和 Prompt 噪声问题。

任务：

1. 新增 Fact Store。
2. 环境数据采集后生成 raw fact 与 normalized fact。
3. 实现 Evidence Builder。
4. S0、SOP 变量注入、S3 假设生成改为使用 Evidence Bundle。
5. 对事实新鲜度和冲突进行显式标注。

验收：

- 每个自动注入变量可以追踪到事实来源。
- 同一字段多来源冲突不会被静默覆盖。
- S0 Prompt 长度受控且包含关键事实。

### 阶段三：结构化推理与反幻觉

目标：让模型输出可校验。

任务：

1. S0 分类输出 schema 化。
2. S3 假设输出 schema 化。
3. S4 验证输出 schema 化。
4. S5 修复计划输出 schema 化。
5. 实现 Claim Verifier。
6. 最终报告强制引用 evidence link。

验收：

- 无证据根因无法进入最终报告。
- 模型输出非法 schema 时自动重试或降级。
- 最终报告能显示每条关键结论的证据来源。

### 阶段四：评测与持续改进

目标：把可靠性从主观体验变成可量化指标。

任务：

1. 建黄金工单评测集。
2. 新增离线 Agent replay。
3. 增加工具稳定性压测。
4. 增加 hallucination regression。
5. 将关键指标接入 Grafana。

验收：

- 每次 Agent 改动能看到准确率、幻觉率、工具成功率变化。
- CI 可以拦截明显降低可靠性的 Prompt 或工具 schema 变更。

## 12. 数据模型建议

### 12.1 fact

```sql
CREATE TABLE fact (
  id UUID PRIMARY KEY,
  case_id TEXT NOT NULL,
  fact_type TEXT NOT NULL,
  source TEXT NOT NULL,
  raw_ref TEXT,
  normalized_value JSONB NOT NULL,
  confidence NUMERIC(4,3) NOT NULL DEFAULT 1.0,
  freshness TEXT NOT NULL DEFAULT 'unknown',
  collected_at TIMESTAMPTZ,
  trace_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 12.2 tool_execution

```sql
CREATE TABLE tool_execution (
  exec_id UUID PRIMARY KEY,
  case_id TEXT NOT NULL,
  conversation_id UUID NOT NULL,
  tool_name TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  input_snapshot JSONB NOT NULL,
  status TEXT NOT NULL,
  risk_level INT NOT NULL,
  authorization_id UUID,
  result_snapshot JSONB,
  error TEXT,
  idempotency_key TEXT,
  trace_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 12.3 claim_evidence_link

```sql
CREATE TABLE claim_evidence_link (
  id UUID PRIMARY KEY,
  case_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  fact_id UUID NOT NULL REFERENCES fact(id),
  relation TEXT NOT NULL,
  confidence NUMERIC(4,3) NOT NULL DEFAULT 1.0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 13. 与现有系统的兼容策略

1. 不一次性替换现有 S0-S6，而是在现有流程旁边引入 AgentEvent v2 和 ToolExecution。
2. 旧 `interactive_request` 保留一段过渡期，但 `tool_confirm` 必须迁移到 exec_id 级协议。
3. 旧环境上下文接口继续提供，新增 Fact Builder 消费同一数据源。
4. SOP 变量池继续使用，但变量来源从任意 env_context 改为 Fact Store 查询。
5. 最终报告先新增 evidence links，不立即删除旧报告字段。

## 14. 参考范式

业界生产级 Agent 系统的共识是：长流程需要持久化状态、人工介入、工具防护、结构化输出和可观测性。

可参考的范式包括：

- OpenAI Structured Outputs：用严格 schema 约束模型输出，降低 JSON 漂移和自由发挥风险。
- OpenAI Agents SDK Guardrails：在输入、输出和工具调用周围增加校验与拦截层。
- OpenAI Agents SDK Tracing：将 Agent、LLM、工具调用串成可观测 trace。
- LangGraph Persistence / Human-in-the-loop：用 checkpoint 支持长流程恢复、中断和人工审批。
- OpenTelemetry GenAI Semantic Conventions：统一记录 LLM、工具和 Agent 调用的可观测字段。

## 15. 总结

全面解决排障 Agent 的可靠性问题，不能继续围绕单个 Prompt、单个按钮或单个接口补洞。正确方向是把系统升级为以证据为中心、工具事务化、推理结构化、结论可验证的 Agent 平台。

最终目标：

```text
信息准确性来自事实系统，而不是模型记忆。
工具稳定性来自事务系统，而不是 SSE 时序。
反幻觉能力来自证据校验，而不是 Prompt 自律。
```

这套架构落地后，Agent 的角色会从“直接给答案的聊天机器人”转变为“受证据和工具事务约束的排障协作者”。这才是面向生产环境的可靠排障 Agent 范式。
