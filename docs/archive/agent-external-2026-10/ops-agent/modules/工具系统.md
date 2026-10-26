# 工具系统设计文档

> 本文档基于 `/aihci/ops-agent/ops_agent/tools/` 目录源码分析撰写

## 1. 模块概述

工具系统是 Agent 与外部世界交互的核心机制。每个工具封装了特定的能力，Agent 通过工具调用来执行操作、获取信息、更新状态。

### 1.1 工具分类

| 分类 | 工具 | 所属 Agent |
|-----|------|-----------|
| **推理工具** | sequentialthinking | 通用 |
| **状态管理** | ops_state_update | OpsAgent |
| **用户交互** | get_info_from_user | OpsAgent |
| **SOP 访问** | get_sop_context, get_sop_discriminators | OpsAgent |
| **步骤指引** | present_sop_step_instruction | OpsAgent |
| **SOP 检索** | query_sop_candidates | OpsAgent |
| **检索工具** | sop_query_open_index, sop_query_read_target, sop_query_compare_candidates, sop_query_checkpoint | SOPQuerySubAgent |
| **结束工具** | task_done, sub_agent_task_done | 各自 Agent |
| **记录工具** | case_intake | OpsAgent |

### 1.2 工具注册机制

```python
# ops_agent/tools/__init__.py
tools_registry: dict[str, type[Tool]] = {}

# 使用装饰器注册
@tools_registry.register("tool_name")
class MyTool(Tool):
    ...
```

## 2. 工具基类

### 2.1 Tool 抽象类

```python
class Tool(ABC):
    """所有工具的基类"""
    
    def __init__(self, model_provider: str | None = None):
        self._model_provider = model_provider

    @cached_property
    def name(self) -> str:
        return self.get_name()

    @cached_property
    def description(self) -> str:
        return self.get_description()

    @cached_property
    def parameters(self) -> list[ToolParameter]:
        return self.get_parameters()

    @abstractmethod
    def get_name(self) -> str:
        """获取工具名称"""
        pass

    @abstractmethod
    def get_description(self) -> str:
        """获取工具描述"""
        pass

    @abstractmethod
    def get_parameters(self) -> list[ToolParameter]:
        """获取参数定义"""
        pass

    @abstractmethod
    async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
        """执行工具"""
        pass

    def requires_user_approval(self, arguments: ToolCallArguments) -> bool:
        """是否需要用户审批，默认返回 True"""
        return True

    async def close(self):
        """清理资源"""
        return None
```

### 2.2 ToolParameter 数据类

```python
@dataclass
class ToolParameter:
    name: str                           # 参数名
    type: str | list[str]               # 类型
    description: str                    # 描述
    enum: list[str] | None = None       # 枚举值
    items: dict | None = None           # 数组元素定义
    required: bool = True               # 是否必需
```

### 2.3 ToolResult 数据类

```python
@dataclass
class ToolResult:
    call_id: str            # 调用 ID
    name: str               # 工具名
    success: bool           # 是否成功
    result: str | None      # 结果内容
    error: str | None       # 错误信息
    id: str | None = None   # OpenAI 特定字段
    display: str | None     # 显示内容（可不同于 result）
```

### 2.4 ToolExecResult 数据类

```python
@dataclass
class ToolExecResult:
    """工具执行的中间结果"""
    output: str | None      # 输出内容
    error: str | None       # 错误信息
    error_code: int = 0     # 错误码
    display: str | None     # 显示内容
```

### 2.5 ToolExecutor 执行器

```python
class ToolExecutor:
    """工具执行器，管理工具的发现与执行"""
    
    def __init__(self, tools: list[Tool]):
        self._tools = tools
        self._tool_map: dict[str, Tool] | None = None

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """执行单个工具调用"""
        normalized_name = self._normalize_name(tool_call.name)
        if normalized_name not in self.tools:
            return ToolResult(
                name=tool_call.name,
                success=False,
                error=f"Tool '{tool_call.name}' not found.",
                call_id=tool_call.call_id,
            )
        
        tool = self.tools[normalized_name]
        tool_exec_result = await tool.execute(tool_call.arguments)
        return ToolResult(
            name=tool_call.name,
            success=tool_exec_result.error_code == 0,
            result=tool_exec_result.output,
            error=tool_exec_result.error,
            call_id=tool_call.call_id,
            display=tool_exec_result.display,
        )

    async def parallel_tool_call(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """并行执行多个工具调用"""
        return await asyncio.gather(*[self.execute_tool_call(call) for call in tool_calls])

    async def sequential_tool_call(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """顺序执行多个工具调用"""
        return [await self.execute_tool_call(call) for call in tool_calls]
```

## 3. OpsAgent 核心工具

### 3.1 ops_state_update - 状态管理

**功能**：维护 OpsAgent 的内部状态备忘录

**参数定义**：
```python
ToolParameter(name="current_stage", type="string", description="当前阶段")
ToolParameter(name="current_route", type="string", description="当前路径")
ToolParameter(name="confirmed_signals", type="array", description="已确认信号")
ToolParameter(name="rejected_signals", type="array", description="已排除信号")
ToolParameter(name="unknown_signals", type="array", description="未知信号")
ToolParameter(name="candidate_routes", type="array", description="候选路径")
ToolParameter(name="excluded_routes", type="array", description="已排除路径")
ToolParameter(name="step_attempts", type="object", description="步骤尝试次数")
ToolParameter(name="risk_flags", type="array", description="风险标记")
ToolParameter(name="last_user_feedback", type="string", description="最近用户反馈")
```

**状态快照**：
```python
@dataclass
class OpsStateSnapshot:
    current_stage: str = "intake"
    current_route: str = ""
    confirmed_signals: list[str] = field(default_factory=list)
    rejected_signals: list[str] = field(default_factory=list)
    unknown_signals: list[str] = field(default_factory=list)
    candidate_routes: list[dict[str, Any]] = field(default_factory=list)
    excluded_routes: list[dict[str, str]] = field(default_factory=list)
    step_attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    last_user_feedback: str = ""
    notes: str = ""
    update_count: int = 0
    revision_history: list[str] = field(default_factory=list)
```

**执行逻辑**：
```python
async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
    # 1. 归一化阶段名称
    if "current_stage" in arguments:
        normalized = _normalize_stage(arguments["current_stage"])
        if normalized is None:
            return ToolExecResult(error=f"Invalid stage: {arguments['current_stage']}", error_code=1)
        arguments["current_stage"] = normalized

    # 2. 增量更新状态
    changed_fields = []
    self_warnings = []
    
    for key, value in arguments.items():
        if value is not None:
            old_value = getattr(self.state, key, None)
            setattr(self.state, key, value)
            if old_value != value:
                changed_fields.append(key)

    # 3. 自检警告
    self_warnings = self._run_self_checks()

    # 4. 返回完整快照
    return ToolExecResult(
        output=json.dumps({
            "changed_fields": changed_fields,
            "self_warnings": self_warnings,
            "snapshot": self.state.to_dict(),
        })
    )
```

**自检项**：
```python
def _run_self_checks(self) -> list[str]:
    warnings = []
    
    # 检查步骤重试次数
    for step_id, attempt in self.state.step_attempts.items():
        if attempt.get("count", 0) >= 2:
            warnings.append(f"Step {step_id} has been retried {attempt['count']} times.")
    
    # 检查路径回流
    current = self.state.current_route
    for excluded in self.state.excluded_routes:
        if current == excluded.get("route"):
            warnings.append(f"Current route {current} was previously excluded.")
    
    return warnings
```

### 3.2 get_info_from_user - 用户交互

**功能**：向用户提问并收集观察类信息

**参数定义**：
```python
ToolParameter(name="question", type="string", description="问题内容", required=True)
ToolParameter(name="context", type="string", description="问题背景")
ToolParameter(name="options", type="array", description="选项列表")
ToolParameter(name="allow_custom_input", type="boolean", description="是否允许自定义输入")
ToolParameter(name="multi_select", type="boolean", description="是否多选")
```

**执行逻辑**：
```python
async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
    question = arguments.get("question", "")
    options = arguments.get("options", [])
    
    # 1. 通过 CLI 控制台或 ACP 协议请求用户输入
    if self._cli_console:
        user_response = await self._cli_console.request_user_input(
            question=question,
            options=options,
            allow_custom_input=arguments.get("allow_custom_input", True),
        )
    else:
        # ACP 协议的 _ops/request_input 扩展
        user_response = await self._request_via_acp(...)
    
    # 2. 返回用户选择
    return ToolExecResult(output=json.dumps(user_response))
```

**适用场景**：
- 观察类问题：功能识别、报错原文、时间线、影响范围
- 决策确认：是否同意切换路径、是否确认执行高风险操作

### 3.3 query_sop_candidates - SOP 检索

**功能**：启动 SOPQuerySubAgent 执行 SOP 路径检索

**参数定义**：
```python
ToolParameter(name="query_goal", type="string", enum=["initial_route", "refine_route", "fallback_route", "next_question"])
ToolParameter(name="problem_statement", type="string", description="问题陈述", required=True)
ToolParameter(name="latest_user_reply", type="string", description="用户最近回复")
ToolParameter(name="confirmed_signals", type="array", description="已确认信号")
ToolParameter(name="rejected_signals", type="array", description="已排除信号")
ToolParameter(name="excluded_routes", type="array", description="已排除路径")
ToolParameter(name="current_route", type="string", description="当前路径")
ToolParameter(name="output_mode", type="string", enum=["routes_only", "routes_and_questions", "full_report"])
```

**执行逻辑**：
```python
async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
    # 1. 构建检索请求
    request = self._build_request(arguments)
    if isinstance(request, str):
        return ToolExecResult(error=request, error_code=1)

    # 2. 创建子 Agent
    child_name = f"SOPQuerySubAgent-{self._parent_agent._sop_query_subagent_call_count}"
    child_recorder = self._trajectory_recorder.create_child_recorder(child_name)

    # 3. 进入 CLI 子 Agent 上下文
    self._enter_cli_subagent_context(child_name)
    
    try:
        # 4. 执行检索
        payload = await run_sop_query_once(
            request=request,
            ops_agent_config=self._ops_agent_config,
            sop_catalog_path=self._sop_catalog_path,
            cli_console=self._cli_console,
            trajectory_recorder=child_recorder,
        )
    finally:
        self._exit_cli_subagent_context()

    # 5. 格式化输出
    return self._format_output(payload, request.output_mode)
```

**输出格式**：
```
<Route Assessment>
status: need_more_evidence
reason: 两个候选 branch 都与现象部分一致

<Candidate Route>
rank: 1
route: AF-VPN-SSLVPN > 升级后登录失败
relevance: high
matched-signals: 升级后出现 / Web 门户登录失败
why: 升级时间窗口与该 branch 的典型触发条件吻合

<High-Value Question>
question: 这次故障是只影响某些用户还是所有用户都登录失败？
why: 区分 branch-A（全量）与 branch-C（部分）
helps-distinguish: AF-VPN-SSLVPN > branch-A vs AF-VPN-SSLVPN > branch-C

<Next Actions>
- 向用户追问影响范围（首条 high_value_question）
- 若用户回答全量受影响，则直接进入 branch-A 的 validation
```

### 3.4 get_sop_context - SOP 上下文

**功能**：读取 SOP 知识库的详细内容

**参数定义**：
```python
ToolParameter(name="mode", type="string", enum=["node_flow", "branch_graph", "step_detail"], required=True)
ToolParameter(name="route", type="string", description="路径：node_path > branch_id")
ToolParameter(name="step_id", type="string", description="步骤 ID")
```

**模式说明**：
| 模式 | 输出内容 |
|-----|---------|
| `node_flow` | Node 的入口描述、分支结构概览 |
| `branch_graph` | Branch 的步骤列表、依赖关系、风险标记 |
| `step_detail` | 单个步骤的详细信息：action、command、expected_result |

### 3.5 present_sop_step_instruction - 步骤指引

**功能**：展示操作指导并收集用户反馈

**参数定义**：
```python
ToolParameter(name="route", type="string", description="当前路径", required=True)
ToolParameter(name="operation_goal", type="string", description="操作目标", required=True)
ToolParameter(name="execution_guidance", type="string", description="执行指引", required=True)
ToolParameter(name="expected_result", type="string", description="预期结果", required=True)
ToolParameter(name="reply_options", type="object", description="反馈选项", required=True)
ToolParameter(name="risk_notice", type="string", description="风险提示")
```

**输出示例**：
```
当前阶段：验证 xxx 路径
依据：你提到"xxx"，这与 SOP 中 xxx 现象一致
下一步：先确认 xxx，这一步不会修改配置

请这样操作：
1. 打开【对象】→【安全策略模板】
2. 找到当前策略，点击编辑
3. 查看 HTTP 自动识别 是否勾选

正常应看到：
- 已勾选 / 未勾选

请反馈：
A. 已勾选
B. 未勾选
C. 找不到该选项
```

## 4. SOPQuerySubAgent 检索工具

### 4.1 sop_query_open_index - 打开索引

**功能**：浏览 SOP 索引的层级结构

**参数定义**：
```python
ToolParameter(name="scope_id", type="string", description="范围 ID：root / domain:<name> / node:<path>", required=True)
ToolParameter(name="view", type="string", enum=["children", "siblings"], description="视图模式", required=True)
```

**输出示例**：
```
## Index: root (children)

### Domain: AF
- nodes: 45
- top nodes: AF-VPN-SSLVPN, AF-网络连接, AF-对象管理

### Domain: APP
- nodes: 32
- top nodes: APP-Web应用防护, APP-入侵防御
```

### 4.2 sop_query_read_target - 读取目标

**功能**：展开具体的 node 或 branch

**参数定义**：
```python
ToolParameter(name="target_id", type="string", description="目标 ID：node:<path> 或 branch:<path>:<id>", required=True)
```

**输出内容**：
- Node：symptoms、routing_signals、alerts_or_keywords、branch 列表
- Branch：triggers、check_steps、solution_steps、risk_flags

### 4.3 sop_query_compare_candidates - 比较候选

**功能**：比较多个候选 branch，产出对比轴和高价值问题

**参数定义**：
```python
ToolParameter(name="candidate_routes", type="array", description="候选路径列表", required=True)
ToolParameter(name="focus_signals", type="array", description="关注的信号")
```

**输出示例**：
```
## Candidate Comparison

### Axis: 影响范围
- branch-A: 全量受影响
- branch-C: 部分用户受影响

### Axis: 触发条件
- branch-A: 升级后出现
- branch-C: 证书过期告警

### High-Value Questions
1. 这次故障是只影响某些用户还是所有用户都登录失败？
   - helps distinguish: branch-A vs branch-C
```

### 4.4 sop_query_checkpoint - 检查点

**功能**：记录当前检索状态和证据

**参数定义**：
```python
ToolParameter(name="matched_signals", type="array", description="已匹配信号")
ToolParameter(name="conflicting_signals", type="array", description="冲突信号")
ToolParameter(name="missing_signals", type="array", description="缺失信号")
ToolParameter(name="leading_candidates", type="array", description="领先候选")
ToolParameter(name="next_intent", type="string", description="下一步意图")
```

**执行逻辑**：
```python
async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
    # 更新会话状态
    if self._session_state:
        self._session_state.evidence_ledger.append({
            "matched_signals": arguments.get("matched_signals", []),
            "conflicting_signals": arguments.get("conflicting_signals", []),
            "missing_signals": arguments.get("missing_signals", []),
            "leading_candidates": arguments.get("leading_candidates", []),
        })
    
    return ToolExecResult(output="Checkpoint recorded.")
```

### 4.5 sub_agent_task_done - 结束工具

**功能**：返回最终 JSON 并结束子 Agent

**参数定义**：
```python
ToolParameter(name="direct_response", type="string", description="最终 JSON 字符串", required=True)
```

**验证要求**：
- 必须是合法 JSON 字符串
- 必须包含所有必需字段
- 候选路径必须真实存在

## 5. 结束工具

### 5.1 task_done - OpsAgent 结束

**功能**：输出总结并结束排障

**参数定义**：
```python
ToolParameter(name="summary", type="string", description="排障总结", required=True)
```

### 5.2 case_intake - 案例记录

**功能**：记录排障案例供后续复盘

**参数定义**：
```python
ToolParameter(name="outcome_tag", type="string", description="结果标签", required=True)
ToolParameter(name="record_markdown", type="string", description="案例记录 Markdown")
```

**outcome_tag 取值**：
| 标签 | 含义 |
|-----|------|
| `resolved` | 已解决或方案已给出 |
| `user_operation_blocked` | 用户拒绝或无法执行检查 |
| `insufficient_information` | 信息不足无法推进 |
| `sop_uncovered` | SOP 未覆盖 |
| `user_terminated` | 用户主动终止 |

## 6. 工具设计原则

### 6.1 单一职责

每个工具只做一件事：
- `get_sop_context` 只读取 SOP 内容
- `present_sop_step_instruction` 只展示步骤指引
- `ops_state_update` 只更新状态

### 6.2 显式依赖注入

```python
# 工具通过 setter 方法注入依赖
if hasattr(tool, "set_sop_catalog_path"):
    tool.set_sop_catalog_path(self.sop_catalog_path)
if hasattr(tool, "set_ops_agent_config"):
    tool.set_ops_agent_config(self._ops_agent_config)
if hasattr(tool, "set_cli_console"):
    tool.set_cli_console(self._cli_console)
```

### 6.3 错误处理

```python
# 工具返回 ToolExecResult，包含错误码
return ToolExecResult(
    output=result,
    error=error_message,
    error_code=0 if success else 1,
)
```

### 6.4 输出格式化

```python
# 使用 sop_markup 模块格式化输出
from ops_agent.runtime.sop_markup import section, key_value_lines, bullet_lines

output = join_sections(
    section("Route Assessment", "route-assessment", ...),
    section("Candidate Route", "candidate-route", ...),
)
```

## 7. 工具审批机制

### 7.1 requires_user_approval()

```python
def requires_user_approval(self, arguments: ToolCallArguments) -> bool:
    """默认所有工具都需要审批"""
    return True
```

### 7.2 审批管理器

```python
class ApprovalManager:
    def needs_approval(self, tool_name: str, arguments: dict) -> tuple[bool, str]:
        """检查是否需要审批"""
        # 检查是否已批量批准
        action = self._parse_action(tool_name, arguments)
        if action in self._approved_actions:
            return False, ""
        return True, f"Tool {tool_name} requires approval"
    
    def approve_action(self, action: ToolAction):
        """批量批准某个动作"""
        self._approved_actions.add(action)
```

### 7.3 白名单工具

以下工具通常不需要审批：
- `sequentialthinking`：纯思考工具
- `ops_state_update`：内部状态更新
- `get_info_from_user`：向用户提问
- `sub_agent_task_done` / `task_done`：结束工具
