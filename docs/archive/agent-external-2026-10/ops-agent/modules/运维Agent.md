# OpsAgent 模块设计文档

> 本文档基于 `/aihci/ops-agent/ops_agent/agent/ops_agent.py` 源码分析撰写

## 1. 模块概述

`OpsAgent` 是面向用户的主 Agent，负责端到端的 SOP（标准操作程序）引导式故障排查流程。它是用户与 SOP 知识库之间的智能桥梁。

### 1.1 核心职责

| 职责领域 | 具体内容 |
|---------|---------|
| **任务初始化** | 解析用户问题、加载 SOP 知识库、初始化工具集 |
| **流程编排** | 信号抽取与路由决策、SOP 检索与收敛、状态管理与更新 |
| **用户交互** | 信息收集、步骤指引、结果展示、风险确认 |
| **轨迹记录** | 完整记录执行过程，支持复盘与审计 |

### 1.2 设计理念

OpsAgent 的设计遵循以下原则：

1. **SOP 优先**：诊断路径、检查项、处理动作必须来自 SOP
2. **证据驱动**：每个判断都必须有用户原话、SOP 信号或工具依据
3. **先验证后处理**：除非 SOP 明确允许，否则不得跳过 check 直接进入 solution
4. **动态收敛**：用户反馈与当前路径不匹配时，及时调整方向

## 2. 类定义与属性

### 2.1 类定义

```python
class OpsAgent(BaseAgent):
    """Top-level troubleshooting agent driven by local SOP catalogs."""
```

### 2.2 核心属性

```python
class OpsAgent(BaseAgent):
    project_path: str = ""                    # 工作目录
    sop_catalog_path: str                    # SOP 知识库路径
    agent_code: str = "OpsAgent"             # Agent 标识码
    
    _ops_agent_config: OpsAgentConfig        # Agent 配置
    _auto_approve_mode: str | None           # 自动审批模式
    _case_intake_root_path: str | None       # 案例记录根目录
    _sop_query_subagent_call_count: int = 0  # 子 Agent 调用计数
```

### 2.3 默认工具集

```python
DEFAULT_OPS_AGENT_TOOLS = [
    "sequentialthinking",           # 思维链工具：组织推理过程
    "ops_state_update",             # 状态更新：维护排障状态机
    "get_info_from_user",           # 用户交互：收集观察类信息
    "case_intake",                  # 案例记录：记录排障过程
    "task_done",                    # 任务完成：输出总结并结束
    "query_sop_candidates",         # SOP 检索：启动子 Agent 执行检索
    "get_sop_discriminators",       # 判别条件：获取 SOP 区分问题
    "get_sop_context",              # SOP 上下文：读取 SOP 详细内容
    "present_sop_step_instruction", # 步骤指引：展示操作指导
]
```

## 3. 初始化流程

### 3.1 构造函数

```python
def __init__(
    self,
    ops_agent_config: OpsAgentConfig,
    auto_approve_mode: str | None = None,
    debug_llm: bool = False,
    case_intake_root_path: str | None = None,
):
    # 1. 解析 SOP 目录路径
    self.sop_catalog_path = self._resolve_sop_catalog_path(
        ops_agent_config.sop_catalog_path
    )
    
    # 2. 初始化 BaseAgent
    super().__init__(
        agent_config=ops_agent_config,
        debug_llm=debug_llm,
    )
```

### 3.2 new_task() 任务初始化

```python
@override
def new_task(
    self,
    task: str,
    extra_args: dict[str, str] | None = None,
    tool_names: list[str] | None = None,
):
    # 1. 设置任务与工作目录
    self._task = task
    self.project_path = extra_args.get("project_path", os.getcwd())

    # 2. 初始化工具集
    provider = self._model_config.model_provider.provider
    self._tools = []
    for tool_name in tool_names:
        tool = tools_registry[tool_name](model_provider=provider)
        # 注入依赖
        if hasattr(tool, "set_sop_catalog_path"):
            tool.set_sop_catalog_path(self.sop_catalog_path)
        if hasattr(tool, "set_ops_agent_config"):
            tool.set_ops_agent_config(self._ops_agent_config)
        if tool_name == "query_sop_candidates":
            # 为子 Agent 工具注入父 Agent 引用
            if hasattr(tool, "set_parent_agent"):
                tool.set_parent_agent(self)
        self._tools.append(tool)

    # 3. 构建 ToolExecutor
    self._tool_caller = ToolExecutor(self._tools)

    # 4. 构建初始消息
    self._initial_messages = self._get_initial_messages()

    # 5. 开始轨迹记录
    if self._trajectory_recorder:
        self._trajectory_recorder.start_recording(...)
```

### 3.3 初始消息构建

```python
def _get_initial_messages(self) -> list[LLMMessage]:
    system_prompt = load_agent_prompt_template("ops_agent_prompt").render()
    
    first_user_prompt = f"""## 用户原始问题
{self._task}

## 启动动作（最小增量，详细规则参见 system prompt）
1. 先做信号抽取，按 `<routing_decision>` 决定——能直接 `query_sop_candidates` 就直接调用...
2. 第一次写入 `ops_state_update`...
3. 一次只推进一个动作。

请开始。"""
    
    return [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=first_user_prompt),
    ]
```

## 4. 执行流程

### 4.1 execute_task() 执行入口

```python
@override
async def execute_task(self) -> AgentExecution:
    # 1. 显示 Agent 启动信息
    if self.cli_console:
        self.cli_console.print_agent_intro({
            "Agent Type": "OpsAgent: SOP-guided troubleshooting",
            "Project Path": str(self.project_path),
            "SOP Catalog": str(self.sop_catalog_path),
        })

    # 2. 调用 BaseAgent.execute_task()
    execution = await super().execute_task()

    # 3. 完成轨迹记录
    if self._trajectory_recorder:
        self._trajectory_recorder.finalize_recording(
            success=execution.success,
            final_result=execution.final_result,
        )

    return execution
```

### 4.2 典型执行流程

```
用户问题
    │
    ▼
┌─────────────────────────────────┐
│  信号抽取与路由决策            │
│  (sequentialthinking)          │
└─────────────────────────────────┘
    │
    ├─ 强信号 → 直接检索 SOP
    │
    └─ 弱信号 → 询问用户补充信息
    │
    ▼
┌─────────────────────────────────┐
│  调用 query_sop_candidates     │◄───────────┐
│  (启动 SOPQuerySubAgent)       │            │
└─────────────────────────────────┘            │
    │                                         │
    ▼                                         │
┌─────────────────────────────────┐           │
│  子 Agent 执行 SOP 检索         │           │
│  （多轮比较与收敛）             │           │
└─────────────────────────────────┘           │
    │                                         │
    ▼                                         │
┌─────────────────────────────────┐           │
│  获取选中 SOP 的上下文          │           │
│  (get_sop_context)              │           │
└─────────────────────────────────┘           │
    │                                         │
    ▼                                         │
┌─────────────────────────────────┐           │
│  检查判别条件                  │           │
│  (get_sop_discriminators)      │           │
└─────────────────────────────────┘           │
    │                                         │
    ├─ 需要更多信息 → get_info_from_user      │
    │                                         │
    └─ 信息充分 → 进入排障步骤                │
              │                              │
              ▼                              │
┌─────────────────────────────────┐           │
│  展示排障步骤指引              │           │
│  (present_sop_step_instruction)│           │
└─────────────────────────────────┘           │
    │                                         │
    ▼                                         │
┌─────────────────────────────────┐           │
│  状态更新                      │           │
│  (ops_state_update)            │           │
└─────────────────────────────────┘           │
    │                                         │
    ├─ 继续下一步？ ──────────────────────────┘
    │
    ▼
┌─────────────────────────────────┐
│  记录案例与总结                │
│  (case_intake + task_done)     │
└─────────────────────────────────┘
```

## 5. 任务完成判断

### 5.1 llm_indicates_task_completed()

```python
@override
def llm_indicates_task_completed(self, llm_response: LLMResponse) -> bool:
    """检查 LLM 是否调用了 task_done 工具"""
    if llm_response.tool_calls is None:
        return False
    return any(tool_call.name == "task_done" for tool_call in llm_response.tool_calls)
```

### 5.2 _extract_final_result()

```python
@override
async def _extract_final_result(self, llm_response: LLMResponse, step: AgentStep) -> str:
    """提取最终结果：执行 task_done 工具并返回总结"""
    if not llm_response.tool_calls:
        return llm_response.content or "Troubleshooting completed."

    # 查找 task_done 和 case_intake 工具调用
    task_done_call = None
    case_intake_calls = []
    for tool_call in llm_response.tool_calls:
        if tool_call.name == "case_intake":
            case_intake_calls.append(tool_call)
        if tool_call.name == "task_done" and task_done_call is None:
            task_done_call = tool_call

    if not task_done_call:
        return llm_response.content or "Troubleshooting completed."

    # 执行工具调用
    summary = task_done_call.arguments.get("summary", "")
    if summary.strip():
        # 执行 case_intake 和 task_done
        step.state = AgentStepState.CALLING_TOOL
        step.tool_calls = [*case_intake_calls, task_done_call]
        self._update_cli_console(step)
        
        # 执行工具
        tool_results = []
        for case_intake_call in case_intake_calls:
            tool_results.append(await self._tool_caller.execute_tool_call(case_intake_call))
        tool_results.append(await self._tool_caller.execute_tool_call(task_done_call))
        step.tool_results = tool_results
        
        # 显示最终总结
        if self.cli_console:
            self.cli_console.print_final_summary(summary, title="排障总结", success=True)
        
        return summary

    return llm_response.content or "Troubleshooting completed."
```

## 6. 与子 Agent 的协作

### 6.1 协作入口：query_sop_candidates 工具

```python
# 在 new_task() 中为 query_sop_candidates 工具注入父 Agent 引用
if tool_name == "query_sop_candidates":
    if hasattr(tool,, "set_parent_agent"):
        tool.set_parent_agent(self)
    if hasattr(tool, "set_parent_agent_state_id"):
        tool.set_parent_agent_state_id(self.get_state_id())
    if hasattr(tool, "set_approval_manager"):
        tool.set_approval_manager(self._approval_manager)
```

### 6.2 子 Agent 调用计数

```python
# 在 QuerySOPCandidatesTool 中
if self._parent_agent and hasattr(self._parent_agent, "_sop_query_subagent_call_count"):
    self._parent_agent._sop_query_subagent_call_count += 1
    child_name = f"SOPQuerySubAgent-{self._parent_agent._sop_query_subagent_call_count}"
```

### 6.3 轨迹记录嵌套

```python
# 创建子 Agent 的轨迹记录器
child_recorder = self._trajectory_recorder.create_child_recorder(child_name)
```

## 7. System Prompt 核心规则

### 7.1 角色定义

```
你是 OpsAgent，一名资深产品运维排障专家，基于当前接入的本地 SOP 知识库
协助用户完成故障收敛、信息确认、分步验证、操作引导和结果闭环。

核心原则：
1. SOP 优先：诊断路径、检查项、处理动作、风险判断必须来自 SOP
2. 证据驱动：每个问题、判断、切换和操作建议都必须有依据
3. 先验证后处理：除非 SOP 明确允许，否则不得跳过 check 直接进入 solution
4. 不猜测不编造：不补全用户未提供的事实
5. 动态收敛：用户反馈与当前路径不匹配时，及时调整方向
6. 面向普通用户：用自然、清楚、低门槛的语言解释步骤
7. 安全闭环：高风险操作必须提示风险并获得确认
```

### 7.2 路由决策规则

```
直接调用 query_sop_candidates：
- 用户提到具体功能 / 页面 / 模块
- 用户提供错误码 / 告警名 / 日志关键字
- 用户给出明确现象类型
- 用户给出影响范围 / 时间线 / 最近变更

先追问再调用：
- 用户输入是真弱信号（"用不了了"、"打不开了"）
- 且完全缺少功能/现象/报错

不要再调用：
- 已经在按 SOP 推进 step
- 用户在回答上一条步骤指令的反馈
```

### 7.3 问题分类契约

```
观察类问题（用户可直接回答）：
- 功能/模块识别、报错原文、时间线、影响范围、背景变更、决策确认
→ 用 get_info_from_user

检查类问题（用户需执行操作才能回答）：
- 运行命令、打开页面查看、看日志、观察物理状态、核对配置项
→ 先读 SOP step_detail，再 present_sop_step_instruction
```

### 7.4 工作流阶段

```
0. 意图识别：判断用户输入类型
1. 信号收集 → 路由：按路由决策清单执行
2. 路径验证：获取 SOP 上下文、展示步骤指引、收集反馈
3. 解决方案执行：按 SOP 执行 solution step
4. 效果确认与闭环：确认故障是否恢复、根因归因
5. 路径切换（reroute）：触发条件、说明原因、更新排除路径
6. 人工升级：整理摘要、建议检查方向
```

## 8. 状态管理

### 8.1 ops_state_update 工具

OpsAgent 通过 `ops_state_update` 工具维护内部状态：

```python
@dataclass
class OpsStateSnapshot:
    current_stage: str = "intake"           # 当前阶段
    current_route: str = ""                 # 当前路径
    confirmed_signals: list[str]            # 已确认信号
    rejected_signals: list[str]             # 已排除信号
    unknown_signals: list[str]              # 未知信号
    candidate_routes: list[dict]            # 候选路径
    excluded_routes: list[dict]             # 已排除路径
    step_attempts: dict[str, dict]          # 步骤尝试次数
    risk_flags: list[str]                   # 风险标记
    last_user_feedback: str = ""            # 最近用户反馈
    notes: str = ""                         # 备注
```

### 8.2 阶段定义

```python
ALLOWED_STAGES = (
    "intake",       # 信息收集
    "routing",      # 路由决策
    "validation",   # 验证检查
    "solution",     # 解决方案执行
    "confirmation", # 效果确认
    "reroute",      # 路径切换
    "escalation",   # 人工升级
    "closed",       # 已关闭
)
```

### 8.3 状态更新时机

- 进入新阶段时
- 拿到新强信号 / 用户明确否定某信号
- 同一 step 重试（写入 step_attempts）
- 发现新的高风险点或风险解除
- 准备升级或闭环前

## 9. 高风险操作处理

### 9.1 高风险判定

```
判定为高风险，满足任一即可：
- SOP 标记 is_high_risk_operation=true
- 操作类型属于：重启、停服、回滚、删除、覆盖配置
- 修改证书、鉴权、权限、安全策略、网络策略
- 用户明确表达担忧或不熟悉操作
```

### 9.2 执行流程

```
1. 在 ops_state_update.risk_flags 写入风险点
2. 用 present_sop_step_instruction 填写 risk_notice
3. 等待用户明确选择"确认执行"
4. 执行后立即收集结果并确认
```

## 10. 结案规则

### 10.1 outcome_tag 取值

| 标签 | 含义 |
|-----|------|
| `resolved` | 故障已修复或方案已给出 |
| `user_operation_blocked` | 用户拒绝或无法执行检查性动作 |
| `insufficient_information` | 多轮澄清后仍无法获取诊断信息 |
| `sop_uncovered` | SOP 内没有覆盖当前现象 |
| `user_terminated` | 用户明确要求结束 |

### 10.2 根因归因规则

```
不允许写成"改名后好了 / 重启后正常"，必须写技术性根因：
- 主根因（SOP 层术语优先）
- 证据链 / 触发条件 / 更深层机制
- 未能定位时显式写"未定位具体技术根因"
```

## 11. 设计亮点

### 11.1 主从 Agent 分离

- OpsAgent 专注于用户交互和流程编排
- SOPQuerySubAgent 专注于 SOP 检索和路径收敛
- 通过 `query_sop_candidates` 工具桥接

### 11.2 状态显式化

- 通过 `ops_state_update` 工具显式维护状态
- 避免 LLM 在多轮对话中遗忘或矛盾
- 支持自检（重复信号、路径回流等）

### 11.3 问题分类契约

- 严格区分观察类和检查类问题
- 防止用户陷入"不知道怎么做"的困境
- 所有检查类操作必须有 SOP 依据

### 11.4 根因归因自检

- 结案前必须判断是"根因级修复"还是"触发刷新"
- 防止把浅层修复误写成根因
- 强制显式标注"未定位具体技术根因"
