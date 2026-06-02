# Ops-Agent 内核深度解析

> 本文档深入分析 ops-agent 的 Context 存储机制、分支差异、Agent 核心架构和交互机制。

## 目录

1. [Context 存储与获取机制](#1-context-存储与获取机制)
2. [分支差异分析](#2-分支差异分析)
3. [Agent 核心层架构](#3-agent-核心层架构)
4. [交互机制详解](#4-交互机制详解)

---

## 1. Context 存储与获取机制

### 1.1 概述

ops-agent 的 Context（上下文）存储分为两个维度：

| 维度 | 存储内容 | 存储位置 | 生命周期 |
|------|----------|----------|----------|
| **会话历史** | LLM 消息历史（message_history） | 内存 / 文件 | 会话级 / 持久化 |
| **执行轨迹** | 完整执行过程（trajectory） | 文件 | 持久化 |

### 1.2 CLI 模式存储

#### 1.2.1 轨迹文件存储

**存储位置**：`.trajectories/YYYY-MM-DD/` 目录

**文件命名规则**：
```
# 主 Agent
trajectory__{change_id}__{state_id}__{timestamp}__OpsAgent.json

# 子 Agent
trajectory__{change_id}__{state_id}__{timestamp}__OpsAgent-SOPQuerySubAgent__{count}.json

# 原始消息历史
original_trajectory__{change_id}__{state_id}__{timestamp}__OpsAgent.json
```

**核心字段说明**（`trajectory_recorder.py`）：

```python
trajectory_data = {
    # 基本信息
    "task": "",                    # 原始任务描述
    "start_time": "",              # ISO 格式开始时间
    "end_time": "",                # ISO 格式结束时间
    "provider": "",                # LLM 提供商（openai/anthropic/openrouter）
    "model": "",                   # 模型名称
    "max_steps": 0,                # 最大步数限制

    # LLM 交互记录
    "llm_interactions": [
        {
            "timestamp": "",       # 交互时间戳
            "provider": "",        # 提供商
            "model": "",           # 模型
            "input_messages": [],  # 输入消息（包含 role/content）
            "response": {
                "content": "",     # 响应文本
                "model": "",       # 实际使用的模型
                "finish_reason": "",  # 完成原因
                "usage": {         # Token 用量
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "reasoning_tokens": 0,
                },
                "tool_calls": [],  # 工具调用
                "reasoning_content": "",  # 推理内容（如 DeepSeek think mode）
            },
            "tools_available": [], # 可用工具名称列表
            "in_place_retry_count": 0,  # 重试次数
        }
    ],

    # Agent 步骤记录
    "agent_steps": [
        {
            "step_number": 1,
            "start_timestamp": "",   # 步骤开始时间
            "end_timestamp": "",     # 步骤结束时间
            "duration": 0.0,         # 持续时间（秒）
            "state": "",             # THINKING / CALLING_TOOL / COMPLETED / ERROR
            "llm_messages": [],      # 本步骤发送给 LLM 的消息
            "llm_response": {},      # LLM 响应
            "tool_calls": [],        # 工具调用列表
            "tool_results": [],      # 工具执行结果
            "reflection": "",        # Agent 反思
            "error": "",             # 错误信息
            "lakeview_summary": "",  # 摘要内容
        }
    ],

    # 结果
    "success": False,            # 是否成功
    "final_result": None,        # 最终结果
    "execution_time": 0.0,       # 总执行时间（秒）

    # 追踪标识
    "state_id": "",              # 20位状态ID，用于关联父子 Agent
    "agent_name": "",            # Agent 名称
    "tools": [],                 # 工具定义
}
```

#### 1.2.2 消息历史存储

**存储位置**：同目录下 `original_trajectory__*.json`

**格式**：
```python
{
    "messages": [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "...", "tool_calls": [...]},
        {"role": "tool", "content": "...", "tool_call_id": "..."},
        # ...
    ],
    "tools": [
        {"name": "query_sop_candidates", "description": "...", "parameters": {...}},
        # ...
    ]
}
```

#### 1.2.3 获取方式

```bash
# 查看轨迹摘要
python scripts/analyze_trajectory.py .trajectories/2026-05-10/trajectory__xxx.json

# 分析目录下所有轨迹
python scripts/analyze_trajectory.py .trajectories/2026-05-10/

# 详细模式
python scripts/analyze_trajectory.py trajectory.json -d

# 导出 CSV
python scripts/analyze_trajectory.py trajectory.json -o result.csv
```

### 1.3 HTTP API 模式存储（feature-hci 分支新增）

#### 1.3.1 会话存储

**实现类**：`InMemorySessionStore`（`session_store.py`）

```python
class InMemorySessionStore:
    """LRU 内存会话存储"""

    def __init__(self, capacity: int = 1000):
        # OrderedDict 维护 LRU 顺序：末尾 = 最近使用
        self._cache: OrderedDict[str, list[Any]] = OrderedDict()

    def get(self, session_id: str) -> list[Any] | None:
        """获取会话消息历史。未命中返回 None。"""
        if session_id not in self._cache:
            return None
        # 移到末尾（标记为最近使用）
        self._cache.move_to_end(session_id)
        return self._cache[session_id]

    def set(self, session_id: str, messages: list[Any]) -> None:
        """保存会话消息历史。超容量时淘汰最旧会话。"""
        # ...
```

**特点**：
- 容量限制：默认 1000 个会话
- 淘汰策略：LRU（最近最少使用）
- 存储内容：LLM 客户端的 `message_history`（provider 原生格式）
- 生命周期：进程内，重启丢失

#### 1.3.2 会话恢复流程

```
┌─────────────────────────────────────────────────────────────────┐
│                      HTTP 请求处理流程                           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  1. 解析请求体                                                   │
│     ChatCompletionRequest:                                      │
│     - model: "ops-agent"                                        │
│     - messages: [{"role": "user", "content": "..."}]            │
│     - session_id: "xxx" (可选，用于多轮)                         │
│     - hci_context: {...} (可选，环境注入)                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. 创建 Agent                                                   │
│     agent = Agent(agent_type="ops_agent", config=config)        │
│     agent.agent.new_task(user_query, extra_args)                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  3. 会话恢复（如果 session_id 存在）                              │
│     prior_history = session_store.get(session_id)               │
│     if prior_history:                                           │
│         # 恢复 LLM 客户端历史                                    │
│         agent.agent._llm_client.client.message_history = prior  │
│         # 仅发送新用户消息                                       │
│         agent.agent._initial_messages = [new_user_msg]          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  4. 流式执行                                                     │
│     async for text_chunk in agent.agent.execute_task_streaming():│
│         yield text_chunk  # SSE 输出                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  5. 保存会话历史                                                 │
│     raw_history = list(agent.agent._llm_client.client.message_history)│
│     session_store.set(session_id, raw_history)                  │
└─────────────────────────────────────────────────────────────────┘
```

#### 1.3.3 HTTP 模式的限制

| 功能 | CLI 模式 | HTTP 模式 | 说明 |
|------|----------|-----------|------|
| 轨迹记录 | ✅ 支持 | ❌ 不支持 | HTTP 模式不创建 TrajectoryRecorder |
| 会话持久化 | ❌ 进程重启丢失 | ❌ 进程重启丢失 | 均为内存存储 |
| 用户交互 | ✅ CLI 交互 | ❌ 不支持 | `get_info_from_user` 无法工作 |
| 流式输出 | ✅ CLI 渲染 | ✅ SSE 流 | HTTP 使用真流式输出 |
| 会话历史 | ✅ LLM 内部维护 | ✅ 显式 session_id | HTTP 模式显式管理 |

### 1.4 LLM 客户端内部历史

每个 LLM 客户端维护自己的 `message_history`：

```python
# llm_client.py
class LLMClient:
    def get_history_length(self) -> int:
        """获取当前消息历史长度"""
        return self.client.get_history_length()

    def rollback_history(self, length: int) -> None:
        """回滚消息历史到指定长度"""
        self.client.rollback_history(length)
```

**用途**：
- 重试时回滚历史
- 上下文压缩时管理历史
- 会话恢复时设置历史

---

## 2. 分支差异分析

### 2.1 分支结构

```
ops-agent/
├── main           # 基础分支，仅支持 CLI 模式
└── feature-hci    # HCI 集成分支，新增 HTTP API
```

### 2.2 HTTP API 为 feature-hci 分支新增

**Git 提交记录**：
```
$ git log --oneline main..feature-hci -- ops_agent/server/
ed25b19 style: 修复 __init__.py 模块文档字符串后缺少空行
e79641d style: 修复 pre-commit 格式检查
89bf793 fix: 修复 PR review 指出的 lint 错误和逻辑 Bug
4e5af36 feat: 添加 OpenAI 兼容 HTTP server 和会话管理支持
```

**新增文件**：
```
ops_agent/server/
├── __init__.py         # 模块入口
├── main.py             # FastAPI 应用入口，/v1/chat/completions 端点
├── openai_compat.py    # OpenAI Chat Completions API 兼容实现
├── session_store.py    # InMemorySessionStore LRU 会话管理
└── otel_integration.py # OpenTelemetry 链路追踪集成
```

**main 分支验证**：
```
$ git show main:ops_agent/server/
fatal: path 'ops_agent/server/' does not exist in 'main'
```

**结论**：HTTP API 模块完全由 feature-hci 分支新增，共 569 行代码。

### 2.3 功能对比

| 功能 | main 分支 | feature-hci 分支 |
|------|-----------|------------------|
| CLI 运行模式 | ✅ | ✅ |
| Web UI (Streamlit) | ✅ | ✅ |
| **ACP 协议支持** | ✅ **推荐用于 HTP** | ✅ |
| **交互式工具审批** | ✅ (ACP) | ✅ (ACP) |
| **get_info_from_user** | ✅ (ACP) | ✅ (ACP) |
| **present_sop_step_instruction** | ✅ (ACP) | ✅ (ACP) |
| OpenAI 兼容 API (`/v1/chat/completions`) | ❌ | ✅ 新增（无交互能力） |
| InMemorySessionStore | ❌ | ✅ 新增 |
| 真流式输出 (execute_task_streaming) | ❌ | ✅ 新增 |
| OpenTelemetry 集成 | ❌ | ✅ 新增 |

**关键区分**：
- **ACP REST API** (`/acp/sessions/*`)：main 分支已有，支持完整交互，**推荐 HTP 使用**
- **OpenAI 兼容 API** (`/v1/chat/completions`)：feature-hci 新增，仅支持文本输出，**无交互能力**

### 2.4 关键代码变更

#### 2.4.1 base_agent.py 新增流式输出方法

```python
# feature-hci 分支新增
async def execute_task_streaming(self) -> AsyncGenerator[str, None]:
    """流式执行任务：每步 LLM 推理完成后立即 yield 助手文本内容。

    与 execute_task() 完全兼容：
    - CLI 模式走原有 execute_task()，不受影响
    - ACP 模式走原有 execute_task()，不受影响
    - HTTP 流式模式调用此方法，获得步骤级真流式输出
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def _on_step_text(content: str) -> None:
        await queue.put(content)

    self._step_text_hook = _on_step_text
    # ...
```

#### 2.4.2 openai_compat.py 核心处理逻辑

```python
class OpenAICompatibleHandler:
    async def _run_agent_stream(self, req, user_query):
        agent = Agent(agent_type="ops_agent", config=self.config)
        agent.agent.new_task(user_query, extra_args)

        # 会话恢复
        if req.session_id:
            prior_history = session_store.get(req.session_id)
            if prior_history:
                agent.agent._llm_client.client.message_history = prior_history
                agent.agent._initial_messages = [new_user_msg]

        # 流式执行
        async for text_chunk in agent.agent.execute_task_streaming():
            yield text_chunk

        # 保存会话
        if req.session_id:
            session_store.set(session_id, raw_history)
```

---

## 3. Agent 核心层架构

### 3.1 完整架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              用户交互层                                      │
│                                                                             │
│  ┌─────────────────┐   ┌─────────────────────────────────────────────────┐ │
│  │    CLI 模式     │   │              HTTP 接入层                         │ │
│  │   ops-cli run   │   │                                                 │ │
│  │                 │   │  ┌─────────────────┐  ┌───────────────────────┐  │ │
│  │ CLIConsole      │   │  │  ACP REST API   │  │ OpenAI 兼容 API       │  │ │
│  │ - 交互式选择    │   │  │  (推荐用于 HTP) │  │ (feature-hci 新增)    │  │ │
│  │ - 工具审批      │   │  │                 │  │                       │  │ │
│  │ - 状态渲染      │   │  │ /acp/sessions   │  │ /v1/chat/completions  │  │ │
│  │                 │   │  │ - 完整交互支持   │  │ - 仅文本输出          │  │ │
│  │                 │   │  │ - 工具审批      │  │ - 无交互能力          │  │ │
│  │                 │   │  │ - 用户问答      │  │ - auto_approve=True   │  │ │
│  │                 │   │  │ - SOP 操作卡    │  │                       │  │ │
│  │                 │   │  │                 │  │                       │  │ │
│  │                 │   │  │ ACPConsole      │  │ InMemorySessionStore  │  │ │
│  │                 │   │  │ (main 分支已有) │  │ (feature-hci 新增)    │  │ │
│  │                 │   │  └────────┬────────┘  └───────────┬───────────┘  │ │
│  └────────┬────────┘   └───────────┼──────────────────────┼──────────────┘ │
│           │                      │                      │                │
│           │                      │                      │                │
│           │    ┌─────────────────┘                      │                │
│           │    │                                        │                │
│           │    ▼                                        ▼                │
│           │  ┌─────────────────────────────────────────────────────────┐  │
│           │  │              HTP OpsAgentBrainAdapter                   │  │
│           │  │                                                         │  │
│           │  │  - 使用 ACP REST 协议（/acp/sessions/*）                │  │
│           │  │  - 支持 BrainInteractiveRequest 事件                    │  │
│           │  │  - 支持 submit_acp_response() 提交交互响应              │  │
│           │  └─────────────────────────────────────────────────────────┘  │
│           │                                                                │
└───────────┼────────────────────────────────────────────────────────────────┘
            │
            ▼
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Agent 核心层                                      │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                         OpsAgent (主 Agent)                            │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     系统提示词                                    │  │  │
│  │  │  ops_agent_prompt.jinja2                                         │  │  │
│  │  │  - 角色定义：SOP 引导的故障排查专家                               │  │  │
│  │  │  - 工作流程：信号抽取 → 路由决策 → 验证执行 → 结果确认           │  │  │
│  │  │  - 输出格式：结构化 Markdown                                      │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     工具集 (Tools)                                │  │  │
│  │  │                                                                   │  │  │
│  │  │  【状态管理】                                                     │  │  │
│  │  │  ├─ ops_state_update    : 排障状态更新                           │  │  │
│  │  │  │   - current_stage    : intake/routing/validation/solution/... │  │  │
│  │  │  │   - confirmed_signals: 已确认信号                             │  │  │
│  │  │  │   - candidate_routes : 候选路径                               │  │  │
│  │  │  │   - excluded_routes  : 已排除路径                             │  │  │
│  │  │  │                                                                   │  │
│  │  │  【SOP 检索】                                                     │  │  │
│  │  │  ├─ query_sop_candidates: SOP 路径检索（触发子 Agent）           │  │  │
│  │  │  │   - problem_statement: 问题概述                               │  │  │
│  │  │  │   - confirmed_signals: 已确认信号                             │  │  │
│  │  │  │   - excluded_routes  : 已排除路径                             │  │  │
│  │  │  │   - query_goal       : initial_route/refine_route/...         │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【SOP 上下文获取】                                               │  │  │
│  │  │  ├─ get_sop_context     : 获取 SOP 上下文                        │  │  │
│  │  │  │   - route            : node_path > branch_id                  │  │  │
│  │  │  │   - mode             : summary/step_detail/discriminators     │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  ├─ get_sop_discriminators: 获取判别条件                         │  │  │
│  │  │  │   - route            : 目标路径                               │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【步骤呈现】                                                     │  │  │
│  │  │  ├─ present_sop_step_instruction: 呈现 SOP 步骤指令              │  │  │
│  │  │  │   - route            : 目标路径                               │  │  │
│  │  │  │   - step_id          : 步骤 ID                                │  │  │
│  │  │  │   - execution_guidance: 执行指引（来自 SOP）                  │  │  │
│  │  │  │   - risk_notice      : 风险提示                               │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【用户交互】                                                     │  │  │
│  │  │  ├─ get_info_from_user : 向用户提问                              │  │  │
│  │  │  │   - question         : 问题文本                               │  │  │
│  │  │  │   - option_1~4       : 选项（最多 4 个）                      │  │  │
│  │  │  │   - context          : 背景说明                               │  │  │
│  │  │  │   - risk_notice      : 风险提示                               │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【思维链】                                                       │  │  │
│  │  │  ├─ sequentialthinking : 结构化思考                              │  │  │
│  │  │  │   - thought          : 思考内容                               │  │  │
│  │  │  │   - next_action      : 下一步动作                             │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【任务结束】                                                     │  │  │
│  │  │  ├─ task_done          : 标记任务完成                            │  │  │
│  │  │  │   - summary          : 排障总结报告                           │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  ├─ case_intake        : 案例录入                                │  │  │
│  │  │  │   - case_facts       : 案例事实                               │  │  │
│  │  │  │   - root_cause       : 根因                                   │  │  │
│  │  │  │   - solution         : 解决方案                               │  │  │
│  │  │  └───────────────────────────────────────────────────────────────┘  │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     执行引擎 (BaseAgent)                          │  │  │
│  │  │                                                                   │  │  │
│  │  │  execute_task() 主循环:                                          │  │  │
│  │  │  ┌───────────────────────────────────────────────────────────┐  │  │  │
│  │  │  │  while step_number <= max_steps:                          │  │  │  │
│  │  │  │    1. check_and_summarize()  # 上下文压缩                  │  │  │  │
│  │  │  │    2. _run_llm_step()        # LLM 调用                    │  │  │  │
│  │  │  │    3. 解析响应:                                            │  │  │  │
│  │  │  │       - task_done? → 结束                                  │  │  │  │
│  │  │  │       - tool_calls? → _tool_call_handler()                 │  │  │  │
│  │  │  │       - 空响应? → 重试                                     │  │  │  │
│  │  │  │    4. _finalize_step()      # 记录轨迹                     │  │  │  │
│  │  │  └───────────────────────────────────────────────────────────┘  │  │  │
│  │  │                                                                   │  │  │
│  │  │  重试机制:                                                       │  │  │
│  │  │  - MAX_IN_PLACE_RETRIES = 3                                      │  │  │
│  │  │  - 重试温度: [0.6, 0.8, 1.0]                                     │  │  │
│  │  │  - 场景: 空响应 / 内容无工具 / 工具执行失败                      │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     消息管理                                      │  │  │
│  │  │                                                                   │  │  │
│  │  │  _initial_messages: [system_prompt, user_task]                   │  │  │
│  │  │  _llm_client.client.message_history: 完整对话历史                │  │  │
│  │  │  _message_summarizer: 上下文压缩管理                             │  │  │
│  │  │    - DEFAULT_TOKEN_LIMIT: 150000                                 │  │  │
│  │  │    - 压缩模式: token / step                                      │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     轨迹记录                                      │  │  │
│  │  │  TrajectoryRecorder:                                             │  │  │
│  │  │    - record_llm_interaction()  # 记录 LLM 交互                   │  │  │
│  │  │    - record_agent_step()       # 记录 Agent 步骤                │  │  │
│  │  │    - finalize_recording()      # 完成记录                       │  │  │
│  │  │    - create_child_recorder()   # 创建子 recorder                │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│                    │ 工具调用: query_sop_candidates                        │
│                    ▼                                                        │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                     QuerySOPCandidatesTool                            │  │
│  │                                                                       │  │
│  │  1. 构建请求: SOPQueryRequest                                         │  │
│  │     - query_goal: initial_route / refine_route / fallback_route       │  │
│  │     - problem_statement, confirmed_signals, excluded_routes           │  │
│  │                                                                       │  │
│  │  2. 创建子轨迹记录器:                                                  │  │
│  │     child_recorder = parent_recorder.create_child_recorder(           │  │
│  │         "SOPQuerySubAgent-{count}"                                    │  │
│  │     )                                                                 │  │
│  │                                                                       │  │
│  │  3. 进入 CLI 子 Agent 上下文:                                          │  │
│  │     cli_console.push_agent_to_stack("SOPQuerySubAgent")               │  │
│  │                                                                       │  │
│  │  4. 调用子 Agent:                                                      │  │
│  │     payload = await run_sop_query_once(                               │  │
│  │         request, ops_agent_config,                                    │  │
│  │         cli_console, child_recorder, state_id                         │  │
│  │     )                                                                 │  │
│  │                                                                       │  │
│  │  5. 格式化输出返回给主 Agent                                           │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                    │                                                        │
│                    ▼                                                        │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                    SOPQuerySubAgent (子 Agent)                         │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     系统提示词                                    │  │  │
│  │  │  sop_query_subagent_prompt.jinja2                                │  │  │
│  │  │  - 角色定义：SOP 检索专家                                         │  │  │
│  │  │  - 工具调用预算管理                                               │  │  │
│  │  │  - 输出格式：JSON payload                                         │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     工具集 (Tools)                                │  │  │
│  │  │                                                                   │  │  │
│  │  │  【索引浏览】                                                     │  │  │
│  │  │  ├─ sop_query_open_index: 打开 SOP 索引层                        │  │  │
│  │  │  │   - scope_id: root / domain:name / node:path / branch:...     │  │  │
│  │  │  │   - view: children / siblings / related                       │  │  │
│  │  │  │   - focus_signals: 聚焦信号                                   │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【目标读取】                                                     │  │  │
│  │  │  ├─ sop_query_read_target: 读取 SOP 目标                         │  │  │
│  │  │  │   - target_id: node:path / branch:path:id                     │  │  │
│  │  │  │   - view: summary / node_flow / branch_graph / step_index     │  │  │
│  │  │  │   - detail_level: brief / full                                │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【候选比较】                                                     │  │  │
│  │  │  ├─ sop_query_compare_candidates: 比较候选路径                   │  │  │
│  │  │  │   - candidate_routes: 候选路径列表                            │  │  │
│  │  │  │   - focus_signals: 聚焦信号                                   │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【检查点】                                                       │  │  │
│  │  │  ├─ sop_query_checkpoint: 保存检索状态                           │  │  │
│  │  │  │   - assessment: 匹配评估                                      │  │  │
│  │  │  │   - candidates: 候选列表                                      │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【思维链】                                                       │  │  │
│  │  │  ├─ sequentialthinking: 结构化思考                               │  │  │
│  │  │  │                                                                   │  │  │
│  │  │  【任务结束】                                                     │  │  │
│  │  │  └─ sub_agent_task_done: 标记子 Agent 任务完成                   │  │  │
│  │  │      - direct_response: JSON payload                             │  │  │
│  │  │        {                                                         │  │  │
│  │  │          "assessment": {"status": "high", "reason": "..."},      │  │  │
│  │  │          "candidates": [...],                                    │  │  │
│  │  │          "high_value_questions": [...],                          │  │  │
│  │  │          "retrieval_trace": [...],                               │  │  │
│  │  │          "stop_reason": "...",                                   │  │  │
│  │  │          "next_actions": [...]                                   │  │  │
│  │  │        }                                                         │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     预算管理                                      │  │  │
│  │  │  tool_call_budget: 默认 20 次工具调用上限                         │  │  │
│  │  │  budget_warning_threshold: 剩余 10 次时预警                       │  │  │
│  │  │  _build_budget_notice_xml(): 生成预算提醒 XML                    │  │  │
│  │  │  build_budget_guard_message(): 预算耗尽拦截消息                  │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │                     输出验证                                      │  │  │
│  │  │  validate_completion_payload(): 验证最终 JSON payload            │  │  │
│  │  │    - 检查必需字段: assessment, candidates, stop_reason...        │  │  │
│  │  │    - 验证 candidate route 格式                                   │  │  │
│  │  │    - 校验 route 是否存在于本地 SOP 索引                          │  │  │
│  │  │  _validate_candidate_routes(): 路径有效性校验                    │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            外部服务层                                        │
│                                                                             │
│  ┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐       │
│  │   LLM Provider    │  │    SOP Index      │  │    MCP Server     │       │
│  │                   │  │                   │  │    (可选)         │       │
│  │  OpenAI           │  │  node_sops.jsonl  │  │                   │       │
│  │  Anthropic        │  │  本地文件         │  │  外部工具集成     │       │
│  │  OpenRouter       │  │  检索索引         │  │  动态工具发现     │       │
│  │  OpenAI-Compatible│  │                   │  │                   │       │
│  └───────────────────┘  └───────────────────┘  └───────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 类继承关系

```
BaseAgent (抽象基类)
    │
    ├── OpsAgent (主 Agent)
    │       │
    │       └── 工具: ops_state_update, query_sop_candidates,
    │                 get_sop_context, get_sop_discriminators,
    │                 present_sop_step_instruction, get_info_from_user,
    │                 sequentialthinking, task_done, case_intake
    │
    └── SOPQuerySubAgent (子 Agent)
            │
            └── 工具: sop_query_open_index, sop_query_read_target,
                      sop_query_compare_candidates, sop_query_checkpoint,
                      sequentialthinking, sub_agent_task_done
```

### 3.3 数据流向

```
用户输入
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ OpsAgent.new_task(task)                                      │
│   - 构建 _initial_messages: [system, user]                   │
│   - 初始化工具列表                                           │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ OpsAgent.execute_task()                                      │
│   - 循环执行直到 task_done 或达到 max_steps                  │
└─────────────────────────────────────────────────────────────┘
    │
    ├─── 步骤 1: LLM 推理
    │         │
    │         ▼
    │    ┌─────────────────────────────────────────────┐
    │    │ LLMClient.achat(messages, config, tools)    │
    │    │   - 发送消息到 LLM                          │
    │    │   - 返回 LLMResponse                       │
    │    └─────────────────────────────────────────────┘
    │
    ├─── 步骤 2: 解析响应
    │         │
    │         ├── task_done? ──→ 结束，返回结果
    │         │
    │         └── tool_calls? ──→ 执行工具
    │                   │
    │                   ▼
    │         ┌─────────────────────────────────────────────┐
    │         │ ToolExecutor.execute_tool_call(tool_call)   │
    │         │   - 查找工具                               │
    │         │   - 执行 tool.execute(arguments)           │
    │         │   - 返回 ToolResult                        │
    │         └─────────────────────────────────────────────┘
    │                   │
    │                   ├── query_sop_candidates?
    │                   │         │
    │                   │         ▼
    │                   │    ┌─────────────────────────────────────┐
    │                   │    │ run_sop_query_once()                │
    │                   │    │   - 创建 SOPQuerySubAgent           │
    │                   │    │   - 设置 request 和 state_id        │
    │                   │    │   - 执行子 Agent                    │
    │                   │    │   - 返回 JSON payload               │
    │                   │    └─────────────────────────────────────┘
    │                   │
    │                   └── 其他工具? ──→ 直接执行
    │
    └─── 步骤 3: 更新消息历史
              │
              ▼
         ┌─────────────────────────────────────────────┐
         │ messages.append(tool_result_messages)       │
         │ _message_summarizer.check_and_summarize()   │
         └─────────────────────────────────────────────┘
```

---

## 4. 交互机制详解

### 4.1 人-Agent 交互

#### 4.1.1 CLI 模式交互流程

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI 交互流程                              │
└─────────────────────────────────────────────────────────────────┘

用户输入: "HCI 虚拟机无法启动"
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│ CLIConsole 初始化                                              │
│   - 创建 OpsAgent                                              │
│   - 设置 console: agent.set_cli_console(cli_console)          │
│   - 设置工具 console: tool.set_cli_console(cli_console)       │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│ Agent 执行循环                                                 │
│                                                                │
│  Step 1: THINKING                                              │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ CLIConsole 显示:                                         │  │
│  │ ╭──────────────────────────────────────────────────────╮│  │
│  │ │ 🤔 OpsAgent 正在思考...                              ││  │
│  │ ╰──────────────────────────────────────────────────────╯│  │
│  └─────────────────────────────────────────────────────────┘  │
│        │                                                       │
│        ▼                                                       │
│  LLM 返回 tool_calls: [ops_state_update, query_sop_candidates]│
│        │                                                       │
│        ▼                                                       │
│  Step 1: CALLING_TOOL                                          │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ CLIConsole 显示:                                         │  │
│  │ ╭──────────────────────────────────────────────────────╮│  │
│  │ │ 🔧 调用工具: ops_state_update                        ││  │
│  │ │    current_stage: routing                            ││  │
│  │ │    confirmed_signals: [虚拟机无法启动]               ││  │
│  │ ╰──────────────────────────────────────────────────────╯│  │
│  └─────────────────────────────────────────────────────────┘  │
│        │                                                       │
│        ▼                                                       │
│  工具执行成功 → 返回结果                                        │
│        │                                                       │
│        ▼                                                       │
│  Step 2: THINKING → 调用 query_sop_candidates                  │
│        │                                                       │
│        ▼                                                       │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ CLIConsole 显示 (子 Agent 上下文):                       │  │
│  │ ╭──────────────────────────────────────────────────────╮│  │
│  │ │ 🤔 SOPQuerySubAgent 正在检索 SOP...                  ││  │
│  │ │    工具调用预算: 20/20                               ││  │
│  │ ╰──────────────────────────────────────────────────────╯│  │
│  └─────────────────────────────────────────────────────────┘  │
│        │                                                       │
│        ▼                                                       │
│  子 Agent 执行完成，返回候选路径                                │
│        │                                                       │
│        ▼                                                       │
│  Step 3: THINKING → 决定需要用户澄清                           │
│        │                                                       │
│        ▼                                                       │
│  调用 get_info_from_user                                       │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ CLIConsole 交互:                                         │  │
│  │ ╭──────────────────────────────────────────────────────╮│  │
│  │ │ ❓ 请选择虚拟机的状态:                               ││  │
│  │ │                                                      ││  │
│  │ │ 背景说明: 这有助于判断问题出在哪个层级              ││  │
│  │ │                                                      ││  │
│  │ │ [1] 虚拟机显示"运行中"但无法访问                    ││  │
│  │ │ [2] 虚拟机显示"已停止"                              ││  │
│  │ │ [3] 虚拟机显示"错误"状态                            ││  │
│  │ │ [4] 虚拟机卡在"启动中"                              ││  │
│  │ │ [5] <自定义输入>您的想法                            ││  │
│  │ ╰──────────────────────────────────────────────────────╯│  │
│  │                                                          │  │
│  │ 用户选择: [1]                                            │  │
│  └─────────────────────────────────────────────────────────┘  │
│        │                                                       │
│        ▼                                                       │
│  继续执行... 最终调用 task_done                                │
│        │                                                       │
│        ▼                                                       │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ CLIConsole 显示最终总结:                                 │  │
│  │ ╭──────────────────────────────────────────────────────╮│  │
│  │ │ ✅ 排障总结                                          ││  │
│  │ │                                                      ││  │
│  │ │ ## 问题描述                                          ││  │
│  │ │ HCI 虚拟机显示"运行中"但无法访问...                  ││  │
│  │ │                                                      ││  │
│  │ │ ## 根因定位                                          ││  │
│  │ │ 虚拟机网络配置问题...                                ││  │
│  │ │                                                      ││  │
│  │ │ ## 解决方案                                          ││  │
│  │ │ 已执行: 检查网络配置, 重启网络服务...                ││  │
│  │ ╰──────────────────────────────────────────────────────╯│  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

#### 4.1.2 CLIConsole 关键方法

```python
# cli_console.py
class CLIConsole:
    async def request_info_from_user_input(
        self,
        question: str,
        options: dict[str, str],
        context: str,
        risk_notice: str,
    ) -> str:
        """请求用户选择或输入"""

    def request_user_approval(self, tool_name: str, arguments: dict) -> bool:
        """请求用户批准工具调用（非 auto_approve 模式）"""

    def print(self, message: str, color: str = None):
        """打印消息到终端"""

    def push_agent_to_stack(self, agent_code: str):
        """进入子 Agent 上下文"""

    def pop_agent_from_stack(self):
        """退出子 Agent 上下文"""

    def set_sub_agent_mode(self, is_sub_agent: bool, agent_code: str):
        """设置子 Agent 模式"""
```

#### 4.1.3 HTP ACP 协议交互流程（推荐方式）

**HTP 与 ops-agent 的对接方式是 ACP REST 协议，而非 OpenAI 兼容 API。**

```
┌─────────────────────────────────────────────────────────────────┐
│                   HTP ACP 协议交互流程                           │
└─────────────────────────────────────────────────────────────────┘

客户端请求:
POST /acp/sessions              → 创建会话（幂等，可指定 session_id）
POST /acp/sessions/{id}/prompt  → 提交用户消息
GET  /acp/sessions/{id}/events  → SSE 事件流（长连接）
POST /acp/sessions/{id}/responses/{request_id} → 提交交互响应

        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│ OpsAgentBrainAdapter.process()                                │
│   - _ensure_acp_session()   创建 ACP 会话                      │
│   - _submit_prompt()        提交用户消息                       │
│   - _consume_events()       消费 SSE 事件流                    │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│ ACPServer (ops-agent 服务端)                                   │
│   - ACPSession: 会话状态管理                                   │
│   - ACPConsole: 实现交互方法的 Console                         │
│   - ACPClientBridge: 客户端通信桥接                            │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
SSE 事件流输出:
┌───────────────────────────────────────────────────────────────┐
│ 1. 文本输出                                                    │
│    method: "session/update"                                    │
│    update.sessionUpdate: "agent_message_chunk"                 │
│    update.content.text: "正在分析问题..."                      │
│                                                                │
│ 2. 工具审批请求                                                │
│    method: "session/request_permission"                        │
│    params.toolCall: {...}                                      │
│    params.options: [允许一次, 允许并记住, 拒绝]                 │
│                                                                │
│ 3. 用户交互请求（get_info_from_user / present_sop_step）       │
│    method: "_ops/request_input"                                │
│    params.request.kind: "info_request" | "sop_step"            │
│    params.request.prompt: "请选择虚拟机状态..."                │
│    params.request.options: [选项列表]                          │
│    params.request.customInput: true                            │
│                                                                │
│ 4. 会话结束                                                    │
│    method: "session/done"                                      │
│    params.stopReason: "end_turn" | "refusal"                   │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
用户交互响应:
┌───────────────────────────────────────────────────────────────┐
│ POST /acp/sessions/{id}/responses/{request_id}                 │
│ {                                                              │
│   "result": {                                                  │
│     "outcome": {                                               │
│       "outcome": "selected",      // 或 "free_text"            │
│       "optionId": "1",            // 选择的选项 ID             │
│       "text": "自定义输入内容"     // 仅 free_text 时有效       │
│     }                                                          │
│   }                                                            │
│ }                                                              │
└───────────────────────────────────────────────────────────────┘
```

#### 4.1.4 ACP 协议支持的交互类型

| 交互类型 | ACP 方法 | 触发工具 | 说明 |
|----------|----------|----------|------|
| **工具审批** | `session/request_permission` | 所有工具调用 | 非自动批准模式下请求用户确认 |
| **信息确认** | `_ops/request_input` (kind=info_request) | `get_info_from_user` | 向用户提问并收集选项 |
| **SOP 操作** | `_ops/request_input` (kind=sop_step) | `present_sop_step_instruction` | 展示操作步骤并收集反馈 |
| **选择输入** | `_ops/request_input` (kind=choice) | 通用选择 | 通用选项选择 |

#### 4.1.5 ACPConsole 关键交互方法

```python
# acp/console.py
class ACPConsole(CLIConsole):
    """支持完整交互的 ACP Console 实现"""

    async def request_tool_approval(
        self, tool_calls, approval_manager=None
    ) -> tuple[bool, str | None, list[str]]:
        """请求工具审批"""
        response = self._bridge.request_client(
            "session/request_permission",
            {
                "sessionId": self._session_id,
                "toolCall": self._tool_call_payload(tool_call),
                "options": [允许一次, 允许并记住, 拒绝],
            },
        )
        # 解析用户选择...

    async def request_info_from_user_input(
        self, question: str, options: dict, context: str, risk_notice: str
    ) -> str:
        """请求用户信息确认（get_info_from_user 工具调用）"""
        return self._request_choice_input(
            kind="info_request",
            title="信息确认卡",
            prompt=question,
            options=options,
            meta={"question": question, "context": context, "riskNotice": risk_notice},
        )

    async def request_sop_step_instruction_input(
        self, route, operation_goal, execution_guidance, ...
    ) -> str:
        """请求 SOP 操作反馈（present_sop_step_instruction 工具调用）"""
        return self._request_choice_input(
            kind="sop_step",
            title="SOP 操作卡",
            prompt=feedback_request,
            options=options,
            meta={
                "route": route,
                "operationGoal": operation_goal,
                "executionGuidance": execution_guidance,
                ...
            },
        )
```

#### 4.1.6 OpenAI 兼容 API 模式（feature-hci 分支新增，不推荐用于 HTP）

**注意**：此模式为 feature-hci 分支新增，用于简单的非交互场景，**不支持 `get_info_from_user` 等交互工具**。

```
POST /v1/chat/completions
{
    "model": "ops-agent",
    "messages": [{"role": "user", "content": "..."}],
    "stream": true,
    "session_id": "xxx",           // 可选，多轮会话
    "hci_context": {...}           // 可选，环境注入
}
```

| 功能 | 说明 | 影响 |
|------|------|------|
| `get_info_from_user` 不支持 | HTTP 无交互能力 | Agent 无法向用户提问 |
| 无工具审批 | auto_approve 强制为 True | 所有工具自动执行 |
| 无轨迹记录 | 不创建 TrajectoryRecorder | 无法回放执行过程 |
| 仅流式文本输出 | SSE 只能输出文本 | 无法传输结构化数据 |

**推荐**：HTP 应使用 ACP 协议（`/acp/sessions/*`）以获得完整交互能力。

### 4.2 Agent-环境交互

#### 4.2.1 工具执行流程

```
┌─────────────────────────────────────────────────────────────────┐
│                     工具执行流程                                 │
└─────────────────────────────────────────────────────────────────┘

LLM 返回 tool_calls:
[
    {
        "call_id": "call_abc123",
        "name": "query_sop_candidates",
        "arguments": {
            "problem_statement": "虚拟机无法启动",
            "query_goal": "initial_route"
        }
    }
]
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│ ToolExecutor.execute_tool_call(tool_call)                     │
│   1. 查找工具: tools[name]                                     │
│   2. 验证参数: tool.validate_arguments(arguments)              │
│   3. 执行工具: tool.execute(arguments)                         │
│   4. 返回结果: ToolResult                                      │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│ ToolResult 结构                                                │
│ {                                                              │
│     "name": "query_sop_candidates",                            │
│     "call_id": "call_abc123",                                  │
│     "success": true,                                           │
│     "result": "Route Assessment...",                           │
│     "error": null,                                             │
│     "error_code": 0                                            │
│ }                                                              │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
结果追加到消息历史:
messages.append({
    "role": "tool",
    "content": result,
    "tool_call_id": "call_abc123"
})
```

#### 4.2.2 工具分类与交互方式

| 工具类别 | 工具名称 | 交互方式 | 环境访问 |
|----------|----------|----------|----------|
| **状态管理** | ops_state_update | 内存读写 | 无外部访问 |
| **SOP 检索** | query_sop_candidates | 创建子 Agent | 读取本地文件 |
| **SOP 读取** | get_sop_context | 直接执行 | 读取本地文件 |
| **SOP 读取** | get_sop_discriminators | 直接执行 | 读取本地文件 |
| **步骤呈现** | present_sop_step_instruction | CLI 交互 | 读取本地文件 |
| **用户交互** | get_info_from_user | CLI 交互 | 无外部访问 |
| **思维链** | sequentialthinking | 内存读写 | 无外部访问 |
| **任务结束** | task_done | 内存读写 | 无外部访问 |
| **任务结束** | sub_agent_task_done | 内存读写 | 无外部访问 |
| **案例录入** | case_intake | 文件写入 | 写入本地文件 |

#### 4.2.3 SOP 索引访问

```python
# sop_index.py
class SOPIndex:
    """SOP 索引管理"""

    def __init__(self, catalog_path: str):
        self.node_map: dict[str, dict] = {}  # node_path -> node_data
        self._load_from_jsonl(catalog_path)

    def get_node_context(self, node_path: str, detail_level: str) -> dict:
        """获取节点上下文"""

    def get_branch_context(self, node_path: str, branch_id: str, detail_level: str) -> dict:
        """获取分支上下文"""

    def get_step_context(self, node_path: str, branch_id: str, step_id: str, detail_level: str) -> dict:
        """获取步骤上下文"""

    def search_nodes(self, query: str, limit: int) -> list[dict]:
        """搜索节点"""
```

**SOP 数据文件格式** (node_sops.jsonl):
```json
{"node_path": "hci/vm", "meta": {"node_name": "虚拟机故障"}, "entry": {...}, "branches": [...]}
{"node_path": "hci/network", "meta": {"node_name": "网络故障"}, "entry": {...}, "branches": [...]}
```

### 4.3 Agent-Agent 交互

#### 4.3.1 主 Agent 与子 Agent 的交互流程

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    主 Agent 与子 Agent 交互流程                               │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ OpsAgent (主 Agent)                      │
│                                          │
│  Step 3: LLM 返回 tool_call              │
│  {                                       │
│    "name": "query_sop_candidates",       │
│    "arguments": {                        │
│      "problem_statement": "...",         │
│      "query_goal": "initial_route"       │
│    }                                     │
│  }                                       │
└──────────────────┬──────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ QuerySOPCandidatesTool.execute(arguments)                                    │
│                                                                              │
│  1. 构建请求对象:                                                             │
│     request = SOPQueryRequest(                                               │
│         query_goal="initial_route",                                          │
│         problem_statement="虚拟机无法启动",                                   │
│         confirmed_signals=[],                                                │
│         excluded_routes=[]                                                   │
│     )                                                                        │
│                                                                              │
│  2. 生成子 Agent 名称:                                                        │
│     parent._sop_query_subagent_call_count += 1                               │
│     child_name = f"SOPQuerySubAgent-{count}"                                 │
│                                                                              │
│  3. 创建子轨迹记录器 (共享 state_id):                                         │
│     child_recorder = parent_recorder.create_child_recorder(child_name)       │
│     ┌─────────────────────────────────────────────────────────────────┐     │
│     │ 父 recorder: state_id = "ABC123..."                              │     │
│     │                   ↓                                              │     │
│     │ 子 recorder: state_id = "ABC123..." (相同)                       │     │
│     │               parent_agent_name = "OpsAgent"                     │     │
│     │               sub_agent_call_count = 1                           │     │
│     └─────────────────────────────────────────────────────────────────┘     │
│                                                                              │
│  4. 进入 CLI 子 Agent 上下文:                                                 │
│     cli_console.push_agent_to_stack("SOPQuerySubAgent-1")                    │
│     cli_console.set_sub_agent_mode(True, "SOPQuerySubAgent-1")               │
│                                                                              │
│  5. 调用子 Agent:                                                             │
│     payload = await run_sop_query_once(                                      │
│         request=request,                                                     │
│         ops_agent_config=config,                                             │
│         sop_catalog_path=catalog_path,                                       │
│         cli_console=cli_console,       # 共享 console                        │
│         trajectory_recorder=child_recorder, # 独立轨迹                        │
│         state_id=parent_state_id,       # 共享状态ID                         │
│         auto_approve_mode=auto_approve                                       │
│     )                                                                        │
└──────────────────────────────────────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ run_sop_query_once() 内部实现                                                 │
│                                                                              │
│  1. 创建 SOPQuerySubAgent:                                                   │
│     agent = SOPQuerySubAgent(ops_agent_config)                               │
│     agent.set_request(request)        # 设置请求对象                         │
│     agent.set_state_id(state_id)      # 设置共享状态ID                       │
│     agent.set_cli_console(cli_console) # 设置共享 console                    │
│     agent.set_trajectory_recorder(child_recorder) # 设置独立轨迹             │
│                                                                              │
│  2. 初始化任务:                                                               │
│     agent.new_task(task=f"SOP query: {request.query_goal}", extra_args)      │
│     ┌─────────────────────────────────────────────────────────────────┐     │
│     │ _initial_messages = [                                            │     │
│     │     LLMMessage(role="system", content=subagent_prompt),          │     │
│     │     LLMMessage(role="user", content=query_task_message)          │     │
│     │ ]                                                               │     │
│     │                                                                 │     │
│     │ query_task_message 包含:                                        │     │
│     │   - problem_statement                                           │     │
│     │   - confirmed_signals / rejected_signals                        │     │
│     │   - excluded_routes                                             │     │
│     │   - retrieval_priors (预检索提示)                                │     │
│     └─────────────────────────────────────────────────────────────────┘     │
│                                                                              │
│  3. 执行子 Agent:                                                             │
│     execution = await agent.execute_task()                                   │
│     ┌─────────────────────────────────────────────────────────────────┐     │
│     │ 子 Agent 执行循环:                                               │     │
│     │   while step <= max_steps:                                      │     │
│     │     1. 检查预算                                                 │     │
│     │     2. LLM 调用                                                 │     │
│     │     3. 工具执行:                                                │     │
│     │        - sop_query_open_index                                   │     │
│     │        - sop_query_read_target                                  │     │
│     │        - sop_query_compare_candidates                           │     │
│     │        - sop_query_checkpoint                                   │     │
│     │     4. 预算扣减                                                 │     │
│     │     5. sub_agent_task_done? → 结束                              │     │
│     └─────────────────────────────────────────────────────────────────┘     │
│                                                                              │
│  4. 验证输出:                                                                 │
│     payload = json.loads(execution.final_result)                             │
│     payload = normalize_query_payload(payload, index)                        │
│     ┌─────────────────────────────────────────────────────────────────┐     │
│     │ validate_completion_payload():                                   │     │
│     │   - 检查必需字段                                                │     │
│     │   - 验证 candidate route 格式                                   │     │
│     │   - 校验 route 是否存在于本地 SOP 索引                          │     │
│     └─────────────────────────────────────────────────────────────────┘     │
│                                                                              │
│  5. 返回 payload                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ QuerySOPCandidatesTool 继续处理                                               │
│                                                                              │
│  6. 退出 CLI 子 Agent 上下文:                                                 │
│     cli_console.pop_agent_from_stack()                                       │
│     cli_console.reset_sub_agent_mode()                                       │
│                                                                              │
│  7. 格式化输出:                                                               │
│     return self._format_output(payload, request.output_mode)                 │
│     ┌─────────────────────────────────────────────────────────────────┐     │
│     │ 输出格式 (Markdown):                                             │     │
│     │                                                                 │     │
│     │ ## Route Assessment                                              │     │
│     │ - status: high                                                   │     │
│     │ - reason: 高度匹配已知故障模式                                   │     │
│     │                                                                 │     │
│     │ ## Candidate Route 1                                             │     │
│     │ - route: hci/vm > vm-start-failure                              │     │
│     │ - relevance: high                                                │     │
│     │ - matched-signals: 虚拟机无法启动                               │     │
│     │ - why: 症状与该分支高度匹配                                     │     │
│     │                                                                 │     │
│     │ ## High-Value Question                                           │     │
│     │ - question: 虚拟机当前显示什么状态？                            │     │
│     │ - why: 帮助区分启动失败 vs 运行异常                             │     │
│     └─────────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────┐
│ OpsAgent 继续执行                        │
│                                          │
│  - 收到工具结果                          │
│  - 追加到消息历史                        │
│  - 继续下一轮 LLM 调用                   │
└─────────────────────────────────────────┘
```

#### 4.3.2 状态追踪机制

**state_id 生成**：
```python
# agent_state_id.py
def generate_agent_state_id() -> str:
    """生成 20 位随机状态 ID"""
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choices(chars, k=20))
```

**父子 Agent 关联**：
```
父 Agent 轨迹文件:
trajectory__CH001__abc123__20260510_120000__OpsAgent.json
             │       │
             │       └── state_id (20位)
             └── change_id (可选)

子 Agent 轨迹文件:
trajectory__CH001__abc123__20260510_120000__OpsAgent-SOPQuerySubAgent__1.json
             │       │                                       │           │
             │       │                                       │           └── 调用序号
             │       │                                       └── 子 Agent 名称
             │       └── 相同的 state_id
             └── 相同的 change_id
```

#### 4.3.3 CLI 栈管理

```python
# cli_console.py
class CLIConsole:
    def __init__(self):
        self._agent_stack: list[str] = []  # Agent 栈

    def push_agent_to_stack(self, agent_code: str):
        """进入子 Agent 上下文"""
        self._agent_stack.append(agent_code)
        # 更新显示前缀

    def pop_agent_from_stack(self):
        """退出子 Agent 上下文"""
        if self._agent_stack:
            self._agent_stack.pop()

    def set_sub_agent_mode(self, is_sub_agent: bool, agent_code: str):
        """设置子 Agent 显示模式"""
        # 修改提示符前缀
        # 调整输出缩进
```

**显示效果**：
```
[OpsAgent] 🤔 正在思考...
[OpsAgent] 🔧 调用工具: query_sop_candidates
  [SOPQuerySubAgent-1] 🤔 正在检索 SOP...
  [SOPQuerySubAgent-1] 🔧 调用工具: sop_query_open_index
  [SOPQuerySubAgent-1] ✅ 找到 3 个候选路径
[OpsAgent] 🤔 分析候选路径...
```

### 4.4 交互机制总结表

| 交互类型 | 参与方 | 通信方式 | 数据流向 | 可观测性 |
|----------|--------|----------|----------|----------|
| **人-Agent (CLI)** | 用户 ↔ OpsAgent | CLIConsole | 双向 | 终端显示 |
| **人-Agent (ACP)** | 用户 ↔ HTP ↔ OpsAgent | ACP REST + SSE | 双向 | 前端卡片 |
| **人-Agent (OpenAI API)** | 用户 ↔ OpsAgent | SSE 流 | 单向 | 仅文本输出 |
| **Agent-工具** | OpsAgent ↔ Tool | 函数调用 | 双向 | 轨迹记录 |
| **Agent-SOP** | Tool ↔ SOPIndex | 文件读取 | 单向 | 轨迹记录 |
| **主-子Agent** | OpsAgent ↔ SOPQuerySubAgent | 工具调用 | 双向 | 关联轨迹 |

**关键区分**：
- **ACP 模式**：支持 `_ops/request_input` 事件，HTP 前端渲染交互卡片，用户选择后通过 `submit_acp_response` 回传
- **OpenAI API 模式**：仅支持 SSE 文本流，无法处理交互请求，`get_info_from_user` 会返回错误

---

## 附录

### A. 配置文件示例

```yaml
# ops_config.yaml
model_providers:
  openai:
    api_key: ${OPENAI_API_KEY}
    base_url: https://api.openai.com/v1

models:
  gpt-4:
    model: gpt-4-turbo-preview
    model_provider: openai
    temperature: 0.0
    top_p: 1.0
    max_tokens: 4096

agents:
  ops_agent:
    model: gpt-4
    max_steps: 50
    sop_catalog: hci
    sop_catalogs:
      hci: data/case_sop_data/hci/sop/node_sops.jsonl
      af: data/case_sop_data/af/sop/node_sops.jsonl
    query_sub_agent:
      model: gpt-4
      max_steps: 30
      tool_call_budget: 20
```

### B. 相关源文件索引

| 模块 | 文件路径 | 主要功能 | 分支 |
|------|----------|----------|------|
| Agent 基类 | `ops_agent/agent/base_agent.py` | Agent 执行引擎 | main |
| 主 Agent | `ops_agent/agent/ops_agent.py` | OpsAgent 实现 | main |
| 子 Agent | `ops_agent/agent/sop_query_subagent.py` | SOPQuerySubAgent 实现 | main |
| 轨迹记录 | `ops_agent/utils/trajectory_recorder.py` | 执行轨迹记录 | main |
| CLI 控制台 | `ops_agent/utils/cli/cli_console.py` | CLI 交互渲染 | main |
| **ACP 协议** | `ops_agent/acp/server.py` | ACP 服务端 | **main** |
| **ACP 协议** | `ops_agent/acp/console.py` | ACPConsole 交互实现 | **main** |
| **ACP 协议** | `ops_agent/acp/protocol.py` | ACP 消息格式 | **main** |
| OpenAI 兼容 API | `ops_agent/server/openai_compat.py` | OpenAI 兼容 API | feature-hci |
| 会话存储 | `ops_agent/server/session_store.py` | HTTP 会话管理 | feature-hci |
| HTTP 入口 | `ops_agent/server/main.py` | FastAPI 应用 | feature-hci |
| 工具注册 | `ops_agent/tools/__init__.py` | 工具注册表 | main |
| SOP 索引 | `ops_agent/runtime/sop_index.py` | SOP 数据访问 | main |
| 配置管理 | `ops_agent/utils/config.py` | 配置加载 | main |

### C. HTP 集成相关文件

| 模块 | 文件路径 | 主要功能 |
|------|----------|----------|
| 大脑适配器 | `backend/conversation-service/app/adapters/ops_agent_brain_adapter.py` | ACP 客户端 |
| 大脑路由器 | `backend/conversation-service/app/adapters/brain_router.py` | 请求路由 + 降级 |
| 大脑端口 | `backend/conversation-service/app/core/brain_port.py` | BrainEvent 定义 |
| 交互响应 | `backend/conversation-service/app/routes/conversations.py` | interactive-response 端点 |

### C. 常用命令

```bash
# CLI 运行
ops-cli run "问题描述" --sop hci --auto-approve

# 查看轨迹
python scripts/analyze_trajectory.py .trajectories/2026-05-10/trajectory__xxx.json

# 启动 HTTP 服务
ops-server

# 运行测试
make test
```

---

## 5. OpenAI 兼容 API 回滚可行性分析

### 5.1 结论摘要

| 项目 | 结论 |
|------|------|
| **OpenAI 兼容 API 是否必要？** | **否**。HTP 实际使用的是 ACP REST 协议，而非 OpenAI 兼容 API。 |
| **是否可以完全回滚？** | **是**。可以安全删除 OpenAI 兼容 API 相关代码，不影响 HTP 功能。 |
| **回滚范围** | 3 个文件完全删除 + 1 个文件部分修改 + 1 个方法删除 |

### 5.2 方案演进历史

```
时间线：
┌─────────────────────────────────────────────────────────────────────────────┐
│ 方案 C（OpenAI 兼容 API）                                                     │
│                                                                              │
│ commit cc1159b (2026-04-28)                                                  │
│ feat: 添加 OpenAI-compatible API 服务器                                       │
│                                                                              │
│ 问题发现：                                                                    │
│ - 无法支持交互式工具审批（get_info_from_user）                                 │
│ - 无法支持 SOP 操作卡（present_sop_step_instruction）                          │
│ - auto_approve=True 硬编码，工具调用无用户确认                                 │
│ - 架构缺陷：SSE 单向流无法实现双向交互                                         │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 方案 E（ACP REST 协议）—— 最终方案                                            │
│                                                                              │
│ main 分支已有实现                                                             │
│                                                                              │
│ 优势：                                                                        │
│ - 完整支持双向交互（session/request_permission + _ops/request_input）         │
│ - 会话在 ops-agent 侧持久化（利用 ACP session ID）                            │
│ - HTP OpsAgentBrainAdapter 已基于此协议实现                                   │
│ - 支持工具审批、用户问答、SOP 操作卡等完整交互能力                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.3 代码依赖分析

#### 5.3.1 文件级依赖关系

```
main.py (FastAPI 入口)
├── /v1/chat/completions → openai_compat.OpenAICompatibleHandler
├── /chat/completions → openai_compat.OpenAICompatibleHandler
├── /acp/sessions/* → acp_routes.router (HTP 使用此路由)
└── otel_integration (仅 OpenAI API 使用，ACP 未使用)

openai_compat.py (OpenAI 兼容 API 处理器)
├── 依赖: session_store.InMemorySessionStore
├── 依赖: base_agent.execute_task_streaming()
└── 无其他外部依赖

session_store.py (内存会话存储)
└── 仅被 openai_compat.py 使用

otel_integration.py (OpenTelemetry 集成)
└── 仅被 main.py 用于 OpenAI API 的链路追踪

execute_task_streaming() (base_agent.py 方法)
└── 仅被 openai_compat.py 调用
```

#### 5.3.2 ACP 路由独立性验证

```bash
# ACP 路由文件
$ grep -l "execute_task_streaming\|session_store\|otel_integration\|openai_compat" \
    ops_agent/server/acp_routes.py
# (无输出) — ACP 路由与 OpenAI API 完全独立

# ACP 服务端
$ grep -l "execute_task_streaming\|session_store\|otel_integration\|openai_compat" \
    ops_agent/acp/server.py ops_agent/acp/console.py
# (无输出) — ACP 实现与 OpenAI API 完全独立
```

### 5.4 Git 提交历史

```
$ git log --oneline --all --source --remotes --grep="OpenAI" --grep="compatible" --all-match
cc1159b refs/remotes/origin/feature/openai-compatible-api feat: 添加OpenAI-compatible API服务器

$ git show cc1159b --stat
commit cc1159b7bbfd15c5d80b82e6b76c721fae0d2a4f
Author: HCI Dev <dev@hci-platform.local>
Date:   Tue Apr 28 00:02:39 2026 +0800

    feat: 添加OpenAI-compatible API服务器

    新增文件：
     Dockerfile.ops-server                |  43 ++++++++
     ops_agent/server/__init__.py         |   6 ++
     ops_agent/server/main.py             | 150 ++++++++++++++++++++++++++++
     ops_agent/server/openai_compat.py    | 183 +++++++++++++++++++++++++++++++++++
     ops_agent/server/otel_integration.py |  61 ++++++++++++
     pyproject.toml                       |   9 ++

$ git show 4e5af36 --stat
commit 4e5af362a6eb461876f560f1de3d0727a53ddfea
Author: HCI Dev <dev@hci-platform.local>
Date:   Wed Apr 29 08:32:39 2026 +0800

    feat: 添加 OpenAI 兼容 HTTP server 和会话管理支持

    新增文件：
     ops_agent/server/session_store.py    |  73 +++++++++++
     ops_agent/agent/base_agent.py        |  56 +++++++++ (execute_task_streaming)
```

### 5.5 回滚操作清单

#### 5.5.1 完全删除的文件

| 文件 | 行数 | 说明 |
|------|------|------|
| `ops_agent/server/openai_compat.py` | 233 | OpenAI 兼容 API 处理器，仅被 `/v1/chat/completions` 使用 |
| `ops_agent/server/session_store.py` | 73 | 内存会话存储，仅被 openai_compat.py 使用 |

#### 5.5.2 修改的文件

**`ops_agent/server/main.py`**：
- 删除 `/v1/chat/completions` 端点（约 30 行）
- 删除 `/chat/completions` 端点（约 5 行）
- 删除 `otel_integration` 导入和初始化代码（约 10 行）
- **保留**：ACP 路由注册、健康检查端点、lifespan 管理等

**`ops_agent/agent/base_agent.py`**：
- 删除 `execute_task_streaming()` 方法（约 56 行，lines 591-624）
- 该方法仅被 openai_compat.py 调用

#### 5.5.3 可选保留

**`ops_agent/server/otel_integration.py`**：
- 当前仅被 OpenAI API 使用
- 未来可为 ACP 路由添加链路追踪支持
- **建议**：保留但暂不初始化，待后续按需启用

### 5.6 回滚验证清单

```bash
# 1. 删除文件后，确认无导入错误
$ python -c "from ops_agent.server.main import app; print('OK')"

# 2. 确认 ACP 路由正常工作
$ curl -X POST http://localhost:8006/acp/sessions -d '{"session_id": "test"}'

# 3. 确认 HTP OpsAgentBrainAdapter 正常工作
$ pytest backend/conversation-service/tests/test_ops_agent_brain_adapter.py -v

# 4. 确认 OpenAI 端点已移除
$ curl -X POST http://localhost:8006/v1/chat/completions
# 期望: 404 Not Found
```

### 5.7 回滚风险评估

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| ACP 路由受影响 | 低 | 高 | 已验证代码独立，无依赖关系 |
| 其他服务调用 OpenAI API | 低 | 中 | 检查 HTP 代码，确认无调用 |
| 遗留配置项 | 中 | 低 | 清理 pyproject.toml 中的 server 依赖（可选） |

### 5.8 总结

**OpenAI 兼容 API（方案 C）** 是在发现交互架构缺陷后被 **ACP REST 协议（方案 E）** 替代的过时实现。HTP 的 `OpsAgentBrainAdapter` 使用的是 ACP 协议，完全不依赖 OpenAI 兼容 API。

**已执行回滚（2026-05-10）**：
- 删除 `openai_compat.py`（266 行）
- 删除 `session_store.py`（70 行）
- 删除 `otel_integration.py`（61 行）
- 清理 `main.py` 中的 OpenAI API 端点和 OTEL 初始化（68 行）
- 清理 `base_agent.py` 中的 `execute_task_streaming()` 方法和相关钩子（77 行）
- 清理 `pyproject.toml` 中不再需要的 OpenTelemetry 依赖（5 个包）

**回滚后收益**：
- 减少约 540 行维护负担
- 消除架构混淆（两种 HTTP API 共存）
- 避免误用不支持交互的 OpenAI API
