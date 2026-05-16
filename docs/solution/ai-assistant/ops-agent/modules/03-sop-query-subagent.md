# SOPQuerySubAgent 模块设计文档

> 本文档基于 `/aihci/ops-agent/ops_agent/agent/sop_query_subagent.py` 源码分析撰写

## 1. 模块概述

`SOPQuerySubAgent` 是专门负责 SOP（标准操作程序）路径检索的子 Agent。它采用 PageIndex 风格的结构化检索策略，通过多轮索引浏览、局部展开、候选比较来收敛到最匹配的 SOP 路径。

### 1.1 设计定位

| 维度 | 说明 |
|-----|------|
| **职责** | SOP 路径检索与收敛 |
| **交互对象** | 父 Agent（OpsAgent），不直接与用户交互 |
| **输出格式** | 严格 JSON，包含候选路径、高价值问题、检索轨迹 |
| **预算控制** | 独立的工具调用预算，强制收敛 |

### 1.2 核心特点

1. **独立配置**：独立的模型配置、步数限制、工具调用预算
2. **专注检索**：不处理用户交互，专注于候选比较与收敛
3. **结构化检索**：PageIndex 风格，先看索引再展开局部
4. **强制结束**：通过预算控制和 payload 验证强制产生结果

## 2. 类定义与属性

### 2.1 类定义

```python
class SOPQuerySubAgent(BaseAgent):
    """Sub-agent specialized in PageIndex-style SOP retrieval."""
    
    MAX_IN_PLACE_RETRIES: int = 5  # 最大原地重试次数（比 BaseAgent 更多）
```

### 2.2 核心属性

```python
class SOPQuerySubAgent(BaseAgent):
    project_path: str = ""                          # 工作目录
    sop_catalog_path: str                          # SOP 知识库路径
    agent_code: str = "SOPQuerySubAgent"           # Agent 标识码
    _is_sub_agent: bool = True                     # 标记为子 Agent
    
    _query_sub_agent_config: SOPQuerySubAgentConfig  # 子 Agent 配置
    _request: SOPQueryRequest | None               # 检索请求
    _session_state: SOPQuerySessionState | None    # 会话状态
    _validated_payload: dict[str, Any] | None      # 已验证的输出 payload
```

### 2.3 默认工具集

```python
DEFAULT_SOP_QUERY_SUBAGENT_TOOLS = [
    "sequentialthinking",           # 思维链：组织推理
    "sub_agent_task_done",          # 结束工具：返回最终 JSON
    "sop_query_open_index",         # 打开索引：浏览层级结构
    "sop_query_read_target",        # 读取目标：展开具体 node/branch
    "sop_query_compare_candidates", # 比较候选：多候选竞争时对比
    "sop_query_checkpoint",         # 检查点：记录当前状态
]
```

### 2.4 配置类

```python
@dataclass
class SOPQuerySubAgentConfig:
    model: ModelConfig            # 模型配置（独立于主 Agent）
    max_steps: int = 80           # 最大步数
    tools: list[str]              # 工具列表
    tool_call_budget: int = 40    # 工具调用预算
    budget_warning_threshold: int = 10  # 预算预警阈值
```

## 3. 检索请求模型

### 3.1 SOPQueryRequest

```python
@dataclass
class SOPQueryRequest:
    query_goal: str                    # 检索意图
    problem_statement: str             # 问题陈述
    latest_user_reply: str | None      # 用户最近回复
    confirmed_signals: list[str]       # 已确认信号
    rejected_signals: list[str]        # 已排除信号
    excluded_routes: list[str]         # 已排除路径
    current_route: str | None          # 当前路径
    output_mode: str                   # 输出模式
```

### 3.2 query_goal 取值

| 值 | 含义 | 起手式 |
|---|------|-------|
| `initial_route` | 首次定位 | 检查 prefetched hints → 打开索引 → 展开目标 |
| `refine_route` | 围绕当前路线收敛 | 展开当前 branch → 核对信号 → 同 node 内 compare |
| `fallback_route` | 当前路线被证伪后再选路 | 排除已失败路径 → 同 node 兄弟 → 回到 domain/root |
| `next_question` | 只产出最有价值的下一问 | 优先 compare → 没有候选时才 open_index |

### 3.3 output_mode 取值

| 值 | 输出内容 |
|---|---------|
| `routes_only` | 只返回候选路径 |
| `routes_and_questions` | 候选路径 + 高价值问题 |
| `full_report` | 完整报告（含检索轨迹） |

## 4. 会话状态管理

### 4.1 SOPQuerySessionState

```python
@dataclass
class SOPQuerySessionState:
    request: SOPQueryRequest                    # 检索请求
    visited_ids: list[str]                      # 已访问的目标 ID
    evidence_ledger: list[dict]                 # 证据账本
    candidate_table: list[dict]                 # 候选表
    high_value_questions: list[dict]            # 高价值问题列表
    retrieval_trace: list[dict]                 # 检索轨迹
    stop_reason: str | None                     # 停止原因
    retrieval_priors: dict[str, list[dict]]     # 预取的检索先验
```

### 4.2 状态快照

```python
def snapshot(self) -> dict[str, Any]:
    """生成当前状态的完整快照"""
    return {
        "request": self.request.to_dict(),
        "visited_ids": list(self.visited_ids),
        "evidence_ledger": list(self.evidence_ledger),
        "candidate_table": list(self.candidate_table),
        "high_value_questions": list(self.high_value_questions),
        "retrieval_trace": list(self.retrieval_trace),
        "stop_reason": self.stop_reason,
        "retrieval_priors": deepcopy(self.retrieval_priors),
    }
```

## 5. 初始化流程

### 5.1 构造函数

```python
def __init__(
    self,
    ops_agent_config: OpsAgentConfig,
    auto_approve_mode: str | None = None,
):
    # 1. 验证配置
    if ops_agent_config.query_sub_agent is None:
        raise ValueError("OpsAgentConfig.query_sub_agent must be configured")

    # 2. 初始化属性
    self.sop_catalog_path = self._resolve_sop_catalog_path(
        ops_agent_config.sop_catalog_path
    )
    self._is_sub_agent = True
    self._query_sub_agent_config = ops_agent_config.query_sub_agent

    # 3. 创建内部配置
    internal_config = _SOPQuerySubAgentInternalConfig(
        model=self._query_sub_agent_config.model,
        max_steps=self._query_sub_agent_config.max_steps,
        tools=self._query_sub_agent_config.tools,
        ...
    )

    # 4. 调用父类构造
    super().__init__(agent_config=internal_config)
    
    # 5. 设置预算
    self._tool_call_budget = self._query_sub_agent_config.tool_call_budget
    self._tool_call_budget_used = 0
```

### 5.2 set_request() 设置请求

```python
def set_request(self, request: SOPQueryRequest) -> None:
    """设置检索请求并初始化会话状态"""
    self._request = request
    self._session_state = SOPQuerySessionState(request=request)
```

### 5.3 new_task() 任务初始化

```python
@override
def new_task(
    self,
    task: str,
    extra_args: dict[str, str] | None = None,
    tool_names: list[str] | None = None,
):
    # 1. 验证请求已设置
    if self._request is None or self._session_state is None:
        raise ValueError("SOPQuerySubAgent request must be set before starting")

    # 2. 设置任务
    self._task = task

    # 3. 加载 SOP 索引
    index = load_sop_index(self.sop_catalog_path)
    
    # 4. 构建检索先验（预取的候选）
    self._session_state.retrieval_priors = build_retrieval_priors(
        index=index,
        request=self._request,
    )

    # 5. 初始化工具
    tool_names = self._query_sub_agent_config.tools
    self._tools = []
    for tool_name in tool_names:
        tool = tools_registry[tool_name](model_provider=provider)
        # 注入依赖
        if hasattr(tool, "set_sop_catalog_path"):
            tool.set_sop_catalog_path(self.sop_catalog_path)
        if hasattr(tool, "set_query_session_state"):
            tool.set_query_session_state(self._session_state)
        self._tools.append(tool)

    # 6. 构建初始消息
    self._initial_messages = self._get_initial_messages()
```

### 5.4 初始消息构建

```python
def _get_initial_messages(self) -> list[LLMMessage]:
    # 1. 加载系统提示词
    system_prompt = self._load_system_prompt(
        tool_call_budget=self._query_sub_agent_config.tool_call_budget,
        budget_warning_threshold=self._query_sub_agent_config.budget_warning_threshold,
    )

    # 2. 构建用户消息
    user_content = build_query_task_message(self._request)
    
    # 3. 添加预取的检索提示
    hint_block = build_prefetched_retrieval_hint_block(
        index=index,
        request=self._request,
        priors=self._session_state.retrieval_priors,
    )
    if hint_block:
        user_content = f"{user_content}\n\n{hint_block}"

    return [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_content),
    ]
```

## 6. 预取检索先验

### 6.1 build_retrieval_priors()

```python
def build_retrieval_priors(
    *,
    index: SOPIndex,
    request: SOPQueryRequest,
    domain_limit: int = 3,
    node_limit: int = 4,
    route_limit: int = 6,
) -> dict[str, list[dict[str, Any]]]:
    """基于 sparse retrieval 预取候选"""
    return {
        "domains": index.retrieve_domains(
            problem_statement=request.problem_statement,
            latest_user_reply=request.latest_user_reply,
            confirmed_signals=request.confirmed_signals,
            rejected_signals=request.rejected_signals,
            limit=domain_limit,
        ),
        "nodes": index.retrieve_nodes(...),
        "routes": index.retrieve_routes(...),
    }
```

### 6.2 Prefetched Retrieval Hints 格式

```
## Prefetched Retrieval Hints
这些是基于本地 sparse retrieval 自动召回的候选假设，只能当作检索起点...

top_domains:
- VPN | score=0.850 | matched=SSLVPN / 登录失败
- 网络连接 | score=0.720 | matched=超时 / 连接失败

top_nodes:
- AF-VPN-SSLVPN | score=0.920 | matched=Web 门户 / 登录失败 | sample_routes=...

top_routes:
- AF-VPN-SSLVPN > 升级后登录失败 | score=0.890 | matched=升级 / Web 门户
```

## 7. 执行与验证

### 7.1 execute_task()

```python
@override
async def execute_task(self) -> AgentExecution:
    # 1. 调用父类执行
    execution = await super().execute_task()
    
    # 2. 完成轨迹记录
    if self._trajectory_recorder:
        self._trajectory_recorder.finalize_recording(
            success=execution.success,
            final_result=execution.final_result,
        )
    
    return execution
```

### 7.2 llm_indicates_task_completed()

```python
@override
def llm_indicates_task_completed(self, llm_response: LLMResponse) -> bool:
    """检查是否调用了 sub_agent_task_done 且有有效的 direct_response"""
    if not llm_response.tool_calls:
        return False
    
    for tool_call in llm_response.tool_calls:
        if tool_call.name != "sub_agent_task_done":
            continue
        direct_response = tool_call.arguments.get("direct_response")
        if isinstance(direct_response, str) and len(direct_response.strip()) >= 2:
            return True
    
    return False
```

### 7.3 validate_completion_payload()

这是 SOPQuerySubAgent 最核心的验证逻辑，确保输出的 JSON 符合约定的结构：

```python
@override
def validate_completion_payload(
    self,
    llm_response: LLMResponse,
) -> CompletionValidationResult:
    # 1. 检查是否有工具调用
    if not llm_response.tool_calls:
        return CompletionValidationResult(
            is_valid=False,
            should_retry=True,
            error_message="SOPQuerySubAgent did not return a completion tool call.",
        )

    # 2. 查找 sub_agent_task_done 调用
    task_done_call = None
    for tool_call in llm_response.tool_calls:
        if tool_call.name == "sub_agent_task_done":
            task_done_call = tool_call
            break
    
    if task_done_call is None:
        return CompletionValidationResult(
            is_valid=False,
            should_retry=True,
            error_message="SOPQuerySubAgent did not call sub_agent_task_done.",
        )

    # 3. 验证 direct_response
    direct_response = task_done_call.arguments.get("direct_response")
    if not isinstance(direct_response, str) or not direct_response.strip():
        return CompletionValidationResult(
            is_valid=False,
            should_retry=True,
            error_message="SOPQuerySubAgent returned an empty direct_response.",
        )

    # 4. 解析 JSON
    try:
        payload = json.loads(direct_response)
    except json.JSONDecodeError:
        return CompletionValidationResult(
            is_valid=False,
            should_retry=True,
            error_message="SOPQuerySubAgent failed to emit valid JSON after retries.",
        )

    # 5. 验证 payload 结构
    validation_error = self._validate_payload_dict(payload)
    if validation_error:
        return CompletionValidationResult(
            is_valid=False,
            should_retry=True,
            error_message=f"SOPQuerySubAgent failed to emit valid JSON. {validation_error}",
        )

    # 6. 验证候选路径存在性
    route_validation_error = self._validate_candidate_routes(payload)
    if route_validation_error:
        return CompletionValidationResult(
            is_valid=False,
            should_retry=True,
            error_message=route_validation_error,
        )

    # 7. 保存已验证的 payload
    self._validated_payload = payload
    return CompletionValidationResult(is_valid=True)
```

### 7.4 _validate_payload_dict() 结构验证

```python
def _validate_payload_dict(self, payload: dict[str, Any]) -> str | None:
    """验证 payload 的结构完整性"""
    required_keys = {
        "assessment",
        "candidates",
        "high_value_questions",
        "retrieval_trace",
        "stop_reason",
        "next_actions",
    }
    
    # 检查必需字段
    missing = sorted(required_keys - set(payload))
    if missing:
        return f"Missing required keys: {', '.join(missing)}"

    # 验证 assessment
    assessment = payload.get("assessment")
    status = str(assessment.get("status", "")).strip()
    if status not in ASSESSMENT_STATUSES:
        return f"Invalid assessment.status: {status}"

    # 验证 candidates
    for candidate in payload.get("candidates", []):
        if not str(candidate.get("route", "")).strip():
            return "Each candidate must contain 'route'."
        if str(candidate.get("relevance", "")).strip() not in {"high", "medium", "low"}:
            return "Each candidate must contain valid 'relevance'."
        if not str(candidate.get("why", "")).strip():
            return "Each candidate must contain 'why'."

    return None
```

### 7.5 _validate_candidate_routes() 路径验证

```python
def _validate_candidate_routes(self, payload: dict[str, Any]) -> str | None:
    """验证候选路径真实存在于 SOP 索引"""
    index = load_sop_index(self.sop_catalog_path)
    invalid_candidates: list[str] = []

    for rank, candidate in enumerate(payload.get("candidates", []), start=1):
        route = str(candidate.get("route", "")).strip()
        parsed_node_path, parsed_branch_id = split_route(route)
        
        # 检查格式
        if not parsed_node_path or not parsed_branch_id:
            invalid_candidates.append(
                f"candidate #{rank}: route `{route}` 不是合法的 `node_path > branch_id` 格式。"
            )
            continue

        # 检查 node 存在
        if parsed_node_path not in index.node_map:
            invalid_candidates.append(
                f"candidate #{rank}: route `{route}` 的 node_path `{parsed_node_path}` 不存在于本地 SOP 索引。"
            )
            continue

        # 检查 branch 存在
        try:
            index.get_branch(parsed_node_path, parsed_branch_id)
        except KeyError:
            invalid_candidates.append(
                f"candidate #{rank}: route `{route}` 的 branch `{parsed_branch_id}` 不存在于 node `{parsed_node_path}` 下。"
            )

    if not invalid_candidates:
        return None

    return (
        "本地 SOP 路径校验失败：你返回的最终 candidates 中包含不存在或不一致的路径。\n"
        "问题候选：\n" + "\n".join(f"- {item}" for item in invalid_candidates[:5])
    )
```

## 8. 预算控制

### 8.1 _build_budget_notice_xml()

```python
@override
def _build_budget_notice_xml(self) -> str:
    """构建预算通知，使用渐近式提醒策略"""
    if self._tool_call_budget is None:
        return ""
    
    remaining = max(0, self._tool_call_budget - self._tool_call_budget_used)

    # 预算充足时不打扰
    if remaining > self.BUDGET_WARNING_THRESHOLD:
        return ""

    usage_ratio = self._tool_call_budget_used / self._tool_call_budget
    header = f"工具调用预算：剩余 {remaining}/{self._tool_call_budget}，已用 {self._tool_call_budget_used}（{usage_ratio:.0%}）。"

    if remaining == 0:
        # 预算耗尽：强制结束
        severity = "【预算耗尽】"
        tail = (
            "已无任何工具调用预算。**禁止**再调用任何检索类工具，"
            "立即基于已有 retrieval_trace 输出最终 JSON，并以 `sub_agent_task_done` 结束；"
            "`stop_reason` 写 `budget_exhausted`。"
        )
    else:
        # 预算预警：提示收敛
        severity = "【预算预警】"
        tail = (
            "已进入收敛阶段。优先把剩余预算花在能直接提升最终 JSON 质量的动作上..."
        )

    return f"<budget_notice>\n{header}\n{severity}{tail}\n</budget_notice>"
```

### 8.2 build_budget_guard_message()

```python
@override
def build_budget_guard_message(self, requested_calls: int, remaining: int) -> str:
    """预算不足时的拦截消息"""
    return (
        "<budget_guard>\n"
        "SOP 检索工具调用已被拦截：预算不足。\n"
        f"本轮请求 {requested_calls} 次工具调用，但仅剩 {remaining} 次预算。\n"
        "禁止继续打开新的索引或目标。请只基于已有 retrieval trace、候选和缺失证据输出最终 JSON，"
        "然后调用 `sub_agent_task_done` 结束。\n"
        "</budget_guard>"
    )
```

## 9. 输出契约

### 9.1 最终 JSON 结构

```json
{
  "assessment": {
    "status": "strong_match | need_more_evidence | weak_match | no_clear_match",
    "reason": "≤ 80 字，说明为什么是这个状态"
  },
  "candidates": [
    {
      "route": "node_path > branch_id",
      "node_path": "...",
      "branch_id": "...",
      "relevance": "high | medium | low",
      "matched_signals": ["..."],
      "conflicting_signals": ["..."],
      "missing_signals": ["..."],
      "why": "≤ 80 字"
    }
  ],
  "high_value_questions": [
    {
      "question": "用户能直接回答的自然语言问题",
      "why": "为何问",
      "helps_distinguish": "能区分哪些 route"
    }
  ],
  "retrieval_trace": [
    {
      "action": "open_index | read_target | compare_candidates",
      "target": "...",
      "reason": "..."
    }
  ],
  "stop_reason": "strong_match_ready | need_more_evidence | budget_exhausted | ...",
  "next_actions": [
    "调用 get_sop_context(mode=branch_graph, route=...)",
    "向用户追问首条 high_value_question"
  ]
}
```

### 9.2 assessment.status 含义

| 状态 | 含义 |
|-----|------|
| `strong_match` | 单一候选强匹配，无需追问 |
| `need_more_evidence` | 需要用户回答更多问题再收敛 |
| `weak_match` | 信号弱，需要大幅追问或重新路由 |
| `no_clear_match` | 无可靠候选 |

### 9.3 stop_reason 建议

| 值 | 含义 |
|---|------|
| `strong_match_ready` | 单一候选已强匹配 |
| `need_more_evidence` | 需要更多信息 |
| `ambiguous_signals` | 信号互相矛盾 |
| `exhausted_candidates` | 可见层级走完仍无可靠候选 |
| `budget_exhausted` | 预算用尽被迫收口 |
| `early_stop_high_confidence` | 证据已足提前结束 |

## 10. 便捷函数

### 10.1 run_sop_query_once()

```python
async def run_sop_query_once(
    request: SOPQueryRequest,
    ops_agent_config: OpsAgentConfig,
    *,
    sop_catalog_path: str | None = None,
    cli_console=None,
    trajectory_recorder=None,
    state_id: str | None = None,
    auto_approve_mode: str | None = None,
) -> dict[str, Any]:
    """一次性执行 SOP 检索并返回标准化 JSON"""
    
    # 1. 创建 Agent
    agent = SOPQuerySubAgent(
        ops_agent_config=ops_agent_config,
        auto_approve_mode=auto_approve_mode,
    )
    
    # 2. 设置请求
    agent.set_request(request)
    if state_id:
        agent.set_state_id(state_id)
    if cli_console:
        agent.set_cli_console(cli_console)
    if trajectory_recorder:
        agent.set_trajectory_recorder(trajectory_recorder)

    # 3. 初始化任务
    agent.new_task(
        task=f"SOP query: {request.query_goal}",
        extra_args={"sop_catalog_path": sop_catalog_path},
    )

    # 4. 执行
    execution = await agent.execute_task()
    if not execution.success:
        raise RuntimeError(execution.final_result or "SOPQuerySubAgent execution failed.")

    # 5. 解析并标准化输出
    raw_payload = json.loads(execution.final_result or "{}")
    index = load_sop_index(sop_catalog_path)
    return normalize_query_payload(raw_payload, index)
```

## 11. System Prompt 核心规则

### 11.1 检索原则

```
1. 先看索引，再决定展开哪里，不要一开始就读完整 SOP 正文
2. 每次只展开当前最可能有价值的局部目标
3. 只把用户原话 / problem_statement / signals 当作外部证据
4. 每轮读取后都要更新当前判断
5. 当证据已足够排序候选，立即停止并产出最终 JSON
6. Prefetched Retrieval Hints 是起点，不是结论
```

### 11.2 工具选择规则

```
- 多候选竞争（同 node ≥ 2 branch）→ compare_candidates
- 跨 node 未收敛 → open_index(view=siblings) 或回到 root
- 验证具体假设 → read_target
- 每次 read_target 或 compare_candidates 之后 → checkpoint
- 预算告急 → 停止开新远端，优先 compare 或直接结束
```

### 11.3 预算响应原则

```
- 越接近用尽，越倾向于"收口"而不是"扩散"
- 优先保留对最终输出最有用的预算
- 避免重复访问 retrieval_trace 中已出现过的 target
- 证据已足够时立即结束，不必等预算耗尽
```

## 12. 设计亮点

### 12.1 强制结构化输出

- 必须通过 `sub_agent_task_done` 返回
- JSON 结构严格验证
- 路径必须真实存在于 SOP 索引

### 12.2 预算驱动的收敛策略

- 渐近式预警（正常 → 预警 → 耗尽）
- 耗尽时强制结束，禁止继续调用工具
- 越接近用尽越倾向于收口

### 12.3 预取加速检索

- 基于 sparse retrieval 预取候选
- 优先验证高相关性候选
- 避免盲目广度搜索

### 12.4 路径验证闭环

- 输出前验证所有候选路径存在
- 发现无效路径时拒绝输出并要求重试
- 确保父 Agent 拿到可用的路径

## 13. 与 OpsAgent 的协作

```
OpsAgent
    │
    │ 调用 query_sop_candidates 工具
    │
    ▼
QuerySOPCandidatesTool
    │
    │ 创建 SOPQuerySubAgent 实例
    │ 设置 request
    │ 注入 trajectory_recorder（子记录器）
    │
    ▼
SOPQuerySubAgent
    │
    │ 执行 PageIndex 风格检索
    │ 预算控制强制收敛
    │ 验证并输出 JSON
    │
    ▼
normalize_query_payload()
    │
    │ 标准化输出
    │
    ▼
返回给 OpsAgent
```
