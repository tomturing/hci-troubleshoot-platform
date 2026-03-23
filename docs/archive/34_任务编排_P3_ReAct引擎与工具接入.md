# 34_任务编排_P3_ReAct引擎与工具接入

> **阶段**：Phase 3 — ReAct 执行器 + SCP 工具接入（Phase 2 完成后开始）  
> **目标**：实现主动工具调用能力，让 AI Agent 能自主查询 HCI 平台信息（告警/任务/VM状态），而不依赖用户人工描述  
> **并行条件**：T09（GLMClient）可与 T10（ReactExecutor）并行 | T11（SCPAdapter）依赖 T10 完成 | T12（人工确认）依赖 T10完成 | T13（审计日志）依赖 T10/T11 完成  
> **前置依赖**：Task 07（DB 迁移含 tool_audit_log）、Task 08（状态机）  
> **创建日期**：2026-03-22  
> **关联文档**：
> - [docs/architecture/完整技术方案.md](../architecture/完整技术方案.md) § 六、Phase 3
> - [docs/architecture/各层最优设计.md](../architecture/各层最优设计.md) § Layer 2/5
> - [docs/reference/scp/openapi.yaml](../reference/scp/openapi.yaml)（SCP REST API 完整规范）

---

# Task 09：GLMClient——LLM 专用适配器（P1）

```
你是一名负责 hci-troubleshoot-platform conversation-service LLM 接入的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
当前系统通过 OpenAI SDK 调用 GLM（通过 OpenClaw 代理），但存在以下问题：
  1. GLM 偶尔返回非标准 JSON 格式的 tool_calls 参数（缺少引号/尾随逗号）
  2. GLM 的 function_call 格式与 OpenAI 规范有细微差异
  3. 流式输出的 tool_calls 合并逻辑与标准 OpenAI 不完全相同
  4. 没有错误码重试逻辑（429 限流/502 网关超时）
  5. 没有 usage 统计（无法追踪 token 消耗）

需要封装一个专用的 GLMClient 处理以上差异，并作为 ReactExecutor 的唯一 LLM 入口。

LLM 配置（从环境变量读取）：
  OPENCLAW_BASE_URL  → GLM 服务地址（OpenAI 兼容格式）
  OPENCLAW_API_KEY   → API Key
  GLM_MODEL          → 模型名称（如 glm-4-flash）

【任务目标】
1. 实现 backend/conversation-service/app/core/glm_client.py
2. 处理 GLM 特有的 JSON 修复（工具调用参数）
3. 实现流式 + 非流式两种调用模式
4. 实现指数退避重试（429/502 错误）
5. 每次调用记录 token usage 到 trace 日志

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/core/glm_client.py（新建）
  - backend/conversation-service/app/core/__init__.py
只读参考：
  - docs/architecture/各层最优设计.md § Layer 2（GLMClient 代码示例）
  - .env / deploy/env/platform.env（查看现有环境变量名称，不修改）
禁止：
  - 在代码中硬编码任何 API Key 或 Base URL

【详细实现步骤】

Step 1：实现 GLMClient

```python
# backend/conversation-service/app/core/glm_client.py
"""GLM 专用 LLM 客户端，处理与标准 OpenAI 的格式差异"""
import asyncio
import json
import logging
import re
from typing import AsyncGenerator
from pydantic import BaseModel
from openai import AsyncOpenAI, RateLimitError, APIConnectionError

logger = logging.getLogger(__name__)

class ToolCall(BaseModel):
    """工具调用结构"""
    id: str
    name: str
    args: dict

class LLMResponse(BaseModel):
    """GLM 响应标准化结构"""
    content: str | None
    finish_reason: str       # stop | tool_calls | length
    tool_calls: list[ToolCall]
    usage: dict

class GLMClient:
    """GLM 专用客户端"""

    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 1.0   # 指数退避基数（秒）

    def __init__(self, base_url: str, api_key: str, model: str):
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
        )
        self.model = model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        """同步调用（适用于 ReAct 推理步骤）"""
        params = self._build_params(messages, tools, stream=False)

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await self.client.chat.completions.create(**params)
                result = self._parse_response(resp)
                # 记录 usage
                logger.info(
                    "GLM 调用完成",
                    extra={
                        "model": self.model,
                        "usage": result.usage,
                        "has_tool_calls": bool(result.tool_calls),
                        "finish_reason": result.finish_reason,
                    }
                )
                return result
            except RateLimitError:
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(f"GLM 限流，{wait}s 后重试（第 {attempt+1} 次）")
                    await asyncio.sleep(wait)
                else:
                    raise
            except APIConnectionError as e:
                logger.error(f"GLM 连接失败: {e}")
                raise

    async def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]:
        """流式调用（适用于向用户输出对话内容）"""
        params = self._build_params(messages, tools, stream=True)
        async with await self.client.chat.completions.create(**params) as stream:
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content

    def _build_params(self, messages, tools, stream) -> dict:
        params = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": 0.1,       # 排障场景需要确定性输出
            "max_tokens": 4096,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        return params

    def _parse_response(self, resp) -> LLMResponse:
        choice = resp.choices[0]
        tool_calls = []

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = self._safe_parse_json(tc.function.arguments, tc.id)
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    args=args,
                ))

        return LLMResponse(
            content=choice.message.content,
            finish_reason=choice.finish_reason or "stop",
            tool_calls=tool_calls,
            usage=resp.usage.model_dump() if resp.usage else {},
        )

    def _safe_parse_json(self, raw: str, call_id: str) -> dict:
        """安全解析 JSON，处理 GLM 偶尔的非标准格式"""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 修复1：移除尾随逗号
        fixed = re.sub(r',\s*([}\]])', r'\1', raw)
        # 修复2：补全未闭合的引号（简单处理）
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            logger.warning(f"无法解析 tool_call JSON [id={call_id}]: {raw[:100]}")
            return {"_raw": raw}   # 降级：保留原始字符串

    @classmethod
    def from_env(cls) -> "GLMClient":
        """从环境变量创建实例"""
        import os
        return cls(
            base_url=os.environ["OPENCLAW_BASE_URL"],
            api_key=os.environ["OPENCLAW_API_KEY"],
            model=os.environ.get("GLM_MODEL", "glm-4-flash"),
        )
```

Step 2：单元测试

tests/unit/test_glm_client.py：
  - 测试 _safe_parse_json 对尾随逗号/缺少引号的处理
  - 使用 AsyncMock mock OpenAI client 测试 chat() 方法
  - 测试 429 重试逻辑（mock RateLimitError，验证 sleep 调用次数）
  - 测试 tool_calls 列表的正确解析

Step 3：集成到 conversation_service.py

替换现有的 OpenAI client 使用，统一通过 GLMClient 调用：
```python
# 在 conversation_service.py 的依赖注入或初始化中
self.glm_client = GLMClient.from_env()
# 确保 OPENCLAW_BASE_URL、OPENCLAW_API_KEY 在 platform.env 中已有定义（不新增）
```

【约束】
- API Key 和 Base URL 只从环境变量读取，不硬编码
- _safe_parse_json 不能抛异常（降级为保留 _raw 字段）
- 重试间隔不超过 8 秒（RETRY_DELAY_BASE × 2^2）

【验收标准】
- [ ] uv run pytest tests/unit/test_glm_client.py -v 全通过，含 JSON 修复用例
- [ ] 重试逻辑：429 时最多重试 3 次，每次延迟翻倍
- [ ] 连接 OpenClaw 后发送真实请求，token usage 出现在日志中
- [ ] make lint 无新增错误
```

---

# Task 10：ReactExecutor——ReAct 推理循环（P1）

```
你是一名负责 hci-troubleshoot-platform conversation-service 推理引擎的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
ReAct（Reasoning + Acting）是 AI Agent 主动诊断的核心执行器。
当用户描述 HCI 故障后，ReAct 执行器负责：
  1. 让 GLM 分析当前信息，判断下一步行动（Reason）
  2. 如果 GLM 决定调用工具，执行工具调用（Act）
  3. 将工具结果加入上下文，继续推理（Observe）
  4. 循环直到 GLM 生成最终回复（stop），或到达最大步数

接口设计对齐 LangGraph 概念（为未来迁移留余地）：
  - AgentState：对应 LangGraph State
  - 每个工具调用 = 一个 Node 执行
  - MAX_STEPS = 15（硬限制，防无限循环）

重要：授权检查是安全边界：
  - risk_level = 1（只读）：自动执行
  - risk_level = 2（写操作）：通过 Redis + SSE 请求用户确认，阻塞等待
  - risk_level = 3（高危）：直接 block，不执行

前置条件：Task 09（GLMClient 完成）

【任务目标】
1. 实现 backend/conversation-service/app/core/react_executor.py
2. 实现「推理→工具调用→授权检查→执行→观察」主循环
3. 每步推理结果通过 SSE 实时推送给前端
4. 工具调用结果写入 tool_audit_log（调用 Task 07 建立的审计表）
5. 实现 TOOL_REGISTRY：工具注册表，含 risk_level 声明

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/core/react_executor.py（新建）
  - backend/conversation-service/app/core/tool_registry.py（新建）
  - backend/conversation-service/app/services/conversation_service.py（集成 ReactExecutor）
只读参考：
  - docs/architecture/各层最优设计.md § Layer 5（ReAct 设计）
  - backend/conversation-service/app/core/glm_client.py（Task 09 产物）

【详细实现步骤】

Step 1：工具注册表

```python
# backend/conversation-service/app/core/tool_registry.py
"""工具注册表：所有工具的声明性元数据，风险等级静态声明"""
from pydantic import BaseModel
from typing import Callable

class ToolDefinition(BaseModel):
    """工具定义（OpenAI function calling 格式 + 扩展字段）"""
    name: str
    description: str
    parameters: dict            # JSON Schema
    risk_level: int             # 1=只读, 2=写操作需确认, 3=高危禁用
    policy: str                 # auto|notify|confirm|block
    category: str               # scp|acli|kb|dialog

# 工具注册表（Phase 3 初始 4 个 SCP 工具，Phase 4 扩展）
TOOL_REGISTRY: dict[str, ToolDefinition] = {
    "get_active_alerts": ToolDefinition(
        name="get_active_alerts",
        description="查询 HCI 平台当前活跃告警列表。用于了解平台当前是否有告警事件，"
                    "是意图识别阶段（S0）的必要信息收集步骤。",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回告警数量，默认 10，最大 50",
                    "default": 10
                },
            },
            "required": []
        },
        risk_level=1,
        policy="auto",
        category="scp",
    ),
    "get_failed_tasks": ToolDefinition(
        name="get_failed_tasks",
        description="查询 HCI 平台最近的失败操作任务。包含虚拟机开关机失败、存储操作失败等，"
                    "是定位故障原因的关键信息来源。",
        parameters={
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "description": "任务类型关键词，如'启动虚拟机'、'关闭虚拟机'",
                },
                "begin_time": {
                    "type": "string",
                    "description": "开始时间，格式 YYYY-MM-DD HH:MM:SS，默认 24 小时内",
                },
                "limit": {"type": "integer", "default": 10},
            },
            "required": []
        },
        risk_level=1,
        policy="auto",
        category="scp",
    ),
    "get_vm_list": ToolDefinition(
        name="get_vm_list",
        description="查询 HCI 平台上的虚拟机列表，可按名称过滤。"
                    "用于确认虚拟机是否存在、当前状态和所在节点。",
        parameters={
            "type": "object",
            "properties": {
                "name_filter": {
                    "type": "string",
                    "description": "虚拟机名称关键词（支持模糊匹配）",
                },
                "limit": {"type": "integer", "default": 20},
            },
            "required": []
        },
        risk_level=1,
        policy="auto",
        category="scp",
    ),
    "get_cluster_detail": ToolDefinition(
        name="get_cluster_detail",
        description="查询指定集群的详细信息，包括架构类型、许可模式、可用区等。",
        parameters={
            "type": "object",
            "properties": {
                "cluster_id": {
                    "type": "string",
                    "description": "集群 ID",
                }
            },
            "required": ["cluster_id"]
        },
        risk_level=1,
        policy="auto",
        category="scp",
    ),
}

def get_tools_for_llm() -> list[dict]:
    """返回 OpenAI function calling 格式的工具列表"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
        }
        for tool in TOOL_REGISTRY.values()
        if tool.policy != "block"    # 高危工具不暴露给 LLM
    ]
```

Step 2：ReactExecutor 核心实现

```python
# backend/conversation-service/app/core/react_executor.py
"""ReAct 推理执行器：推理→工具调用→授权→执行→观察循环"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator
from .glm_client import GLMClient, LLMResponse
from .tool_registry import TOOL_REGISTRY, get_tools_for_llm

MAX_STEPS = 15   # 硬限制，防止无限循环

class AgentState:
    """单次 ReAct 执行的状态（对应 LangGraph State 概念）"""
    def __init__(self, session_id: str, messages: list[dict]):
        self.session_id = session_id
        self.messages = messages.copy()
        self.step_count = 0
        self.tool_results: list[dict] = []

class ReactExecutor:
    """ReAct 推理执行器"""

    def __init__(
        self,
        glm_client: GLMClient,
        tool_executor,           # 注入：SCPAdapter / acli 执行器
        confirm_service,         # 注入：人工确认服务（Task 12）
        audit_service,           # 注入：审计日志服务（Task 13）
        sse_emitter,             # 注入：SSE 推送器
    ):
        self.glm = glm_client
        self.tool_executor = tool_executor
        self.confirm_service = confirm_service
        self.audit = audit_service
        self.sse = sse_emitter

    async def run(
        self,
        state: AgentState,
        system_prompt: str,
    ) -> AsyncGenerator[str, None]:
        """
        主 ReAct 循环，通过 SSE 实时返回内容。
        
        循环终止条件：
          1. GLM 返回 finish_reason="stop"（生成最终回复）
          2. 达到 MAX_STEPS 限制
          3. 工具执行出现不可恢复错误
        """
        messages = [
            {"role": "system", "content": system_prompt},
            *state.messages
        ]
        tools = get_tools_for_llm()

        while state.step_count < MAX_STEPS:
            state.step_count += 1

            # Step 1: 推理（Reason）
            await self.sse.emit(state.session_id, {
                "type": "thinking",
                "step": state.step_count,
                "message": "正在分析..."
            })

            response: LLMResponse = await self.glm.chat(
                messages=messages,
                tools=tools,
            )

            # Step 2: 检查是否有工具调用
            if not response.tool_calls:
                # 没有工具调用 = 生成最终文字回复
                yield response.content or ""
                return

            # Step 3: 处理工具调用（Act）
            tool_results = []
            for tool_call in response.tool_calls:
                result = await self._execute_tool_call(
                    tool_call, state
                )
                tool_results.append(result)

            # Step 4: 将工具结果加入消息历史（Observe）
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": str(tc.args)}
                    }
                    for tc in response.tool_calls
                ]
            })
            for r in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": r["tool_call_id"],
                    "content": str(r["result"]),
                })

        # 达到 MAX_STEPS
        yield "⚠️ 诊断步骤已达上限，请联系人工支持"

    async def _execute_tool_call(self, tool_call, state: AgentState) -> dict:
        """执行单个工具调用，含授权检查和审计记录"""
        tool_def = TOOL_REGISTRY.get(tool_call.name)
        if not tool_def:
            return {"tool_call_id": tool_call.id, "result": f"未知工具: {tool_call.name}"}

        # 授权检查（risk_level >= 2 需要用户确认）
        if tool_def.risk_level >= 2:
            if tool_def.policy == "block":
                return {
                    "tool_call_id": tool_call.id,
                    "result": f"工具 {tool_call.name} 风险等级过高，已阻止执行"
                }
            # 请求用户确认（Task 12 实现）
            confirmed = await self.confirm_service.request_confirm(
                session_id=state.session_id,
                tool_name=tool_call.name,
                tool_args=tool_call.args,
                risk_level=tool_def.risk_level,
            )
            if not confirmed:
                return {
                    "tool_call_id": tool_call.id,
                    "result": "用户已取消该操作"
                }

        # 通知（risk_level == 1 的 notify 策略）
        if tool_def.policy == "notify":
            await self.sse.emit(state.session_id, {
                "type": "tool_executing",
                "tool": tool_call.name,
                "args": tool_call.args,
            })

        # 执行工具
        started_at = datetime.now(timezone.utc)
        audit_id = str(uuid.uuid4())
        result = error = None

        try:
            result = await self.tool_executor.execute(
                tool_call.name, tool_call.args
            )
        except Exception as e:
            error = str(e)
            result = f"工具执行失败: {error}"
        finally:
            completed_at = datetime.now(timezone.utc)
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            # 审计记录（必须写入，不可绕过）
            await self.audit.write(
                id=audit_id,
                session_id=state.session_id,
                tool_name=tool_call.name,
                tool_args=tool_call.args,
                risk_level=tool_def.risk_level,
                policy=tool_def.policy,
                result=result,
                error=error,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
            )

        return {"tool_call_id": tool_call.id, "result": result}
```

Step 3：单元测试

tests/unit/test_react_executor.py：
  - 测试 risk_level=3（block）的工具被拒绝
  - 测试 MAX_STEPS 达到上限后的行为
  - 使用 AsyncMock mock glm_client，模拟工具调用流程
  - 测试 tool_results 正确附加到 messages

【约束】
- MAX_STEPS = 15 是硬限制，不可配置化到可超过的值
- 工具审计日志写入失败不应阻止工具执行（降级错误，但要记录）
- risk_level >= 2 的工具在无法连接 Redis 时应 fallback 为 block

【验收标准】
- [ ] uv run pytest tests/unit/test_react_executor.py -v 通过
- [ ] risk_level=3 工具调用返回 block 消息，不执行
- [ ] 超过 MAX_STEPS 时输出上限提示，不无限循环
- [ ] 工具调用完成后，tool_audit_log 表有对应记录
- [ ] make lint 无新增错误
```

---

# Task 11：SCPAdapter——SCP REST API 工具实现（P1）

```
你是一名负责 hci-troubleshoot-platform SCP 平台接入的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
SCP（Service Control Platform）是深信服 HCI 的管理 REST API 网关，提供：
  - 告警列表查询（GET /janus/20180725/alarms）
  - 操作任务查询（GET /janus/20180725/tasks）
  - 云主机/虚拟机列表（GET /janus/20240725/servers）
  - 集群详情（GET /janus/20190725/clusters/{cluster_id}）

完整 OpenAPI 规范在：docs/reference/scp/openapi.yaml

认证方式：
  - EC2Auth（默认）或 TokenAuth（API Key in header: x-auth-token）
  - SCP 地址从环境变量读取：SCP_BASE_URL、SCP_API_KEY

SCPAdapter 是 ReactExecutor 的工具执行后端之一，实现上述 4 个"Tool"
（对应 Task 10 中 TOOL_REGISTRY 声明的 4 个工具函数）。

前置条件：Task 10（ReactExecutor 完成，TOOL_REGISTRY 已定义）

【任务目标】
1. 实现 backend/conversation-service/app/adapters/scp_adapter.py
2. 实现 4 个工具：get_active_alerts / get_failed_tasks / get_vm_list / get_cluster_detail
3. 实现连接超时和错误处理（SCP 不可用时的降级策略）
4. 集成到 ReactExecutor 的工具执行后端
5. 端到端验证：发送"查看告警"请求触发 get_active_alerts 工具调用

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/adapters/scp_adapter.py（新建）
  - backend/conversation-service/app/adapters/__init__.py
  - deploy/env/platform.env（仅新增 SCP_BASE_URL / SCP_API_KEY 配置项注释）
只读参考：
  - docs/reference/scp/openapi.yaml（权威 API 规范）
  - backend/conversation-service/app/core/tool_registry.py（Task 10 产物）

【详细实现步骤】

Step 1：参考 OpenAPI 规范，实现 4 个工具

先阅读 docs/reference/scp/openapi.yaml 中以下路径的完整参数定义：
  - GET /janus/20180725/alarms（参数：fields, page_num, page_size, az_id）
  - GET /janus/20180725/tasks（参数：az_id, begin_time, end_time, fields, object_id）
  - GET /janus/20240725/servers（参数：fields, order_by, page_num）
  - GET /janus/20190725/clusters/{cluster_id}

```python
# backend/conversation-service/app/adapters/scp_adapter.py
"""SCP（深信服 HCI 管理平台）REST API 适配器"""
import logging
import os
from datetime import datetime, timedelta, timezone
import httpx

logger = logging.getLogger(__name__)

class SCPAdapter:
    """SCP REST API 工具执行适配器"""

    DEFAULT_TIMEOUT = 15.0    # 秒
    DEGRADED_RESPONSE = {"_degraded": True, "message": "SCP 暂时不可达，使用降级响应"}

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            "x-auth-token": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @classmethod
    def from_env(cls) -> "SCPAdapter":
        return cls(
            base_url=os.environ["SCP_BASE_URL"],
            api_key=os.environ["SCP_API_KEY"],
        )

    async def execute(self, tool_name: str, args: dict) -> dict:
        """统一工具执行入口，供 ReactExecutor 调用"""
        dispatch = {
            "get_active_alerts": self.get_active_alerts,
            "get_failed_tasks": self.get_failed_tasks,
            "get_vm_list": self.get_vm_list,
            "get_cluster_detail": self.get_cluster_detail,
        }
        handler = dispatch.get(tool_name)
        if not handler:
            return {"error": f"SCPAdapter 未实现工具: {tool_name}"}
        return await handler(**args)

    async def get_active_alerts(self, limit: int = 10) -> dict:
        """
        查询活跃告警列表
        API: GET /janus/20180725/alarms
        参数约定：page_size=limit, page_num=1
        响应结构：{"code": 0, "data": {"data": [...], "total": N}}
        """
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20180725/alarms",
                    params={"page_num": 1, "page_size": limit},
                )
                resp.raise_for_status()
                data = resp.json()
                alarms = data.get("data", {}).get("data", [])
                return {
                    "total": len(alarms),
                    "alarms": [
                        {
                            "id": a.get("id"),
                            "name": a.get("name"),
                            "level": a.get("level"),      # critical|major|minor
                            "status": a.get("status"),
                            "message": a.get("message"),
                            "created_at": a.get("created_at"),
                        }
                        for a in alarms[:limit]
                    ]
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP 告警查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE

    async def get_failed_tasks(
        self,
        task_type: str | None = None,
        begin_time: str | None = None,
        limit: int = 10,
    ) -> dict:
        """
        查询失败任务/操作日志
        API: GET /janus/20180725/tasks
        """
        # 默认 begin_time = 24 小时内
        if not begin_time:
            begin_time = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).strftime("%Y-%m-%d %H:%M:%S")

        params = {
            "begin_time": begin_time,
            "page_size": limit,
            "page_num": 1,
        }
        if task_type:
            params["fields"] = task_type

        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20180725/tasks",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                tasks = data.get("data", {}).get("data", [])
                # 过滤失败任务
                failed = [t for t in tasks if t.get("status") in ("failed", "error")]
                return {
                    "total_failed": len(failed),
                    "tasks": [
                        {
                            "id": t.get("id"),
                            "name": t.get("name"),
                            "description": t.get("description"),
                            "status": t.get("status"),
                            "error_message": t.get("error_message"),
                            "created_at": t.get("created_at"),
                            "object_name": t.get("object_name"),
                        }
                        for t in failed[:limit]
                    ]
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP 任务查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE

    async def get_vm_list(
        self, name_filter: str | None = None, limit: int = 20
    ) -> dict:
        """查询虚拟机列表，可按名称过滤"""
        params = {"page_size": limit, "page_num": 1}
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20240725/servers", params=params
                )
                resp.raise_for_status()
                data = resp.json()
                vms = data.get("data", {}).get("data", [])
                if name_filter:
                    vms = [v for v in vms if name_filter.lower() in (v.get("name") or "").lower()]
                return {
                    "total": len(vms),
                    "vms": [
                        {
                            "id": v.get("id"),
                            "name": v.get("name"),
                            "status": v.get("status"),
                            "host_name": v.get("host_name"),
                            "cluster_name": v.get("cluster_name"),
                        }
                        for v in vms[:limit]
                    ]
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP VM 列表查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE

    async def get_cluster_detail(self, cluster_id: str) -> dict:
        """查询集群详情"""
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20190725/clusters/{cluster_id}"
                )
                resp.raise_for_status()
                data = resp.json()
                cluster = data.get("data", {})
                return {
                    "id": cluster.get("id"),
                    "name": cluster.get("name"),
                    "arch_type": cluster.get("arch_type"),
                    "authorize_mode": cluster.get("authorize_mode"),
                    "az_id": cluster.get("az_id"),
                    "node_count": cluster.get("node_count"),
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP 集群详情查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE
```

Step 2：集成到 ReactExecutor

在 ReactExecutor 初始化时注入 SCPAdapter：
```python
# 在 conversation_service.py 的服务初始化中
scp_adapter = SCPAdapter.from_env()
react_executor = ReactExecutor(
    glm_client=glm_client,
    tool_executor=scp_adapter,
    ...
)
```

Step 3：单元测试（使用 respx mock HTTP 调用）

```bash
uv add respx --dev    # HTTP mock 库

# tests/unit/test_scp_adapter.py
# - mock GET /alarms，验证返回格式
# - mock 连接超时，验证降级响应
# - mock name_filter 过滤逻辑
```

Step 4：端到端验证

```bash
# 在 conversation-service 中发送消息，触发告警查询
curl -X POST http://localhost:8002/api/v1/conversations/{session_id}/messages \
  -d '{"content": "帮我看一下当前有什么告警"}'
# 预期：AI 调用 get_active_alerts 工具，在 tool_audit_log 中有记录
# 如果 SCP 不可达，AI 应说明无法连接平台，而不是崩溃
```

【约束】
- SCP 不可达时，返回降级响应（不抛异常）
- 不缓存 SCP 响应（每次调用都实时查询，保证数据新鲜度）
- API Key 只从环境变量读取

【验收标准】
- [ ] uv run pytest tests/unit/test_scp_adapter.py -v 通过（含超时降级测试）
- [ ] SCP 可达时，get_active_alerts 返回正确格式的告警列表
- [ ] SCP 不可达时，返回 _degraded=True 的降级响应，不崩溃
- [ ] 每次工具调用在 tool_audit_log 表有记录
- [ ] make lint 无新增错误
```

---

# Task 12：人工确认机制——Redis 等待 + SSE 通知（P1）

```
你是一名负责 hci-troubleshoot-platform 人工确认机制的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
ReAct 执行器在遇到 risk_level >= 2 的工具时，需要暂停并等待用户确认。
实现方案：
  1. ReactExecutor 将待确认工具调用信息推送到用户的 SSE 流（confirm_request 事件）
  2. 前端收到 confirm_request 显示确认弹窗
  3. 用户点击确认/取消，触发 POST /confirm
  4. ReactExecutor 通过 Redis BRPOP 阻塞等待确认结果（超时 120 秒）
  5. 超时则自动取消操作

Redis Key 设计：
  confirm:{session_id}  → LIST，ReactExecutor BRPOP，前端 POST 触发 LPUSH

前置条件：Task 10（ReactExecutor 完成）

【任务目标】
1. 实现 ConfirmService（Redis BRPOP 等待逻辑）
2. 新增 POST /api/v1/conversations/{session_id}/confirm 接口
3. 在 SSE 推送中新增 confirm_request 事件类型
4. 集成到 ReactExecutor（替换 Task 10 中的 confirm_service 占位符）
5. 验证：触发 risk_level=2 工具后，SSE 推送 confirm_request，POST /confirm 后继续执行

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/services/confirm_service.py（新建）
  - backend/conversation-service/app/api/conversations.py（新增 /confirm 路由）
只读参考：
  - docs/architecture/各层最优设计.md § Layer 1（Redis Key 设计）
  - backend/conversation-service/app/（现有 SSE 实现，了解 SSE event 格式）

【详细实现步骤】

Step 1：实现 ConfirmService

```python
# backend/conversation-service/app/services/confirm_service.py
"""人工确认服务：通过 Redis 实现 ReAct 执行器的阻塞等待"""
import json
import logging
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 120    # 等待用户确认的超时秒数
REDIS_KEY_PREFIX = "confirm:"

class ConfirmService:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def request_confirm(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
    ) -> bool:
        """
        请求用户确认。阻塞等待直到用户响应或超时。
        返回 True = 用户确认，False = 用户取消或超时
        """
        key = f"{REDIS_KEY_PREFIX}{session_id}"

        # 清空可能残留的旧确认结果
        await self.redis.delete(key)

        # 推送 SSE confirm_request 事件（由调用方的 sse_emitter 处理）
        # 这里只负责等待

        logger.info(
            f"等待用户确认 [session={session_id}] 工具={tool_name}，超时={CONFIRM_TIMEOUT}s"
        )

        # BRPOP 阻塞等待，超时返回 None
        result = await self.redis.brpop(key, timeout=CONFIRM_TIMEOUT)

        if result is None:
            logger.warning(f"确认超时 [session={session_id}]")
            return False

        _, value = result
        try:
            data = json.loads(value)
            confirmed = data.get("confirmed", False)
            logger.info(f"用户确认结果 [session={session_id}]: confirmed={confirmed}")
            return confirmed
        except Exception:
            return False

    async def submit_confirm(
        self, session_id: str, confirmed: bool, authorized_by: str
    ) -> None:
        """
        接收并提交用户确认结果（由 POST /confirm 路由调用）
        """
        key = f"{REDIS_KEY_PREFIX}{session_id}"
        value = json.dumps({"confirmed": confirmed, "authorized_by": authorized_by})
        await self.redis.lpush(key, value)
        # 设置过期（防止遗留数据）
        await self.redis.expire(key, 300)
```

Step 2：新增 /confirm 路由

在 conversations.py 路由中新增：

```python
class ConfirmRequest(BaseModel):
    confirmed: bool
    authorized_by: str    # 当前用户 ID

@router.post("/{session_id}/confirm", status_code=200)
async def submit_confirm(
    session_id: str,
    req: ConfirmRequest,
    confirm_service: ConfirmService = Depends(get_confirm_service),
):
    """接收用户的工具调用确认结果"""
    await confirm_service.submit_confirm(
        session_id=session_id,
        confirmed=req.confirmed,
        authorized_by=req.authorized_by,
    )
    return {"status": "ok"}
```

Step 3：SSE confirm_request 事件格式定义

在 SSE 推送模块中，新增 confirm_request 事件类型：
```json
{
  "type": "confirm_request",
  "tool_name": "service_restart",
  "tool_args": {"service_name": "exporter", "host_id": "node-01"},
  "risk_level": 2,
  "risk_description": "重启 exporter 服务将导致监控数据短暂中断（约 30 秒）",
  "timeout_seconds": 120
}
```

Step 4：测试

```bash
# 模拟触发一个 risk_level=2 的工具调用
# 1. 发送消息（需要在 TOOL_REGISTRY 中临时添加一个 risk_level=2 的测试工具）
# 2. 监听 SSE，确认 confirm_request 事件推出
# 3. 调用 POST /confirm，确认 ReactExecutor 继续执行

uv run pytest backend/conversation-service/tests/test_confirm_service.py -v
```

【约束】
- 确认超时（120s）后，工具调用自动取消，不可强制执行
- Redis 不可用时，所有高风险工具 fallback 为 block（安全优先）

【验收标准】
- [ ] POST /api/v1/conversations/{session_id}/confirm 接口存在且返回 200
- [ ] Redis BRPOP 超时后，request_confirm 返回 False
- [ ] SSE 流中出现 confirm_request 事件类型
- [ ] 用户确认后，ReactExecutor 继续执行工具
- [ ] Redis 不可用时，高风险工具 fallback 为 block
- [ ] uv run pytest backend/conversation-service/tests/test_confirm_service.py -v 通过
- [ ] make lint 无新增错误
```

---

# Task 13：AuditService——工具调用审计日志服务（P1）

```
你是一名负责 hci-troubleshoot-platform 工具调用审计日志的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
Task 10（ReactExecutor）中每次工具调用都执行：
  await self.audit.write(...)

但 audit_service 的实现从未被定义（只有注入占位）。
这是一个安全生产要求：所有工具调用——无论成功还是失败——都必须写入 tool_audit_log 表。
该表在 Task 07（迁移 003）中已创建，包含如下关键字段：
  - session_id、tool_name、tool_args（JSONB）、risk_level
  - policy（auto/notify/confirm/block）
  - authorized_by（risk_level>=2 时记录确认用户）
  - result（JSONB，执行结果摘要）
  - error（文本，执行异常信息）
  - started_at、completed_at、duration_ms
  - trace_id（W3C traceparent）

审计服务有两个关键约束：
  1. 写入操作在 ReactExecutor 的 finally 块中调用，不可因审计失败阻断工具执行
  2. 审计记录不可删除（只读 API，无 DELETE 路由）

前置条件：Task 07（tool_audit_log 表已建立）、Task 10（ReactExecutor 已定义注入接口）

【任务目标】
1. 实现 AuditService（写 tool_audit_log）
2. 实现审计日志查询 API（GET /api/v1/audit-logs，供管理后台展示）
3. 将 AuditService 集成到 ReactExecutor（替换 Task 10 中的 audit_service 占位符）
4. 验证：执行一次工具调用后，tool_audit_log 表有对应记录

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/services/audit_service.py（新建）
  - backend/conversation-service/app/api/audit.py（新建：查询路由）
  - backend/conversation-service/app/services/conversation_service.py（注入 AuditService）
只读参考：
  - backend/shared/models/（Task 07 建立的 ToolAuditLog ORM 模型）
  - backend/conversation-service/app/core/react_executor.py（Task 10 产物，找到 audit_service 注入点）
禁止：
  - 添加 DELETE /audit-logs 路由（审计记录不可删除）
  - 审计写入失败时抛出异常阻断工具执行（必须 except Exception: logger.error 降级）

【详细实现步骤】

Step 1：实现 AuditService

```python
# backend/conversation-service/app/services/audit_service.py
"""工具调用审计日志服务：写 tool_audit_log 表，强制不可绕过"""
import logging
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from backend.shared.models.audit import ToolAuditLog   # Task 07 建立的 ORM 模型

logger = logging.getLogger(__name__)

class AuditService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def write(
        self,
        id: str,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
        policy: str,
        result,
        error: str | None,
        started_at: datetime,
        completed_at: datetime,
        duration_ms: int,
        authorized_by: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """
        写入工具调用审计记录。
        此方法由 ReactExecutor.finally 块调用，内部所有异常必须捕获并记录，
        不可向上抛出（否则会掩盖工具执行的真实结果）。
        """
        try:
            log = ToolAuditLog(
                id=id,
                session_id=session_id,
                tool_name=tool_name,
                tool_args=tool_args,
                risk_level=risk_level,
                policy=policy,
                result={"data": str(result)[:2000]} if result else None,   # 截断大结果
                error=error,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                authorized_by=authorized_by,
                trace_id=trace_id,
            )
            self.db.add(log)
            await self.db.commit()
        except Exception as e:
            # 审计写入失败：记录但不阻断调用方（ReactExecutor finally 块）
            logger.error(
                f"审计日志写入失败 [session={session_id} tool={tool_name}]: {e}",
                exc_info=True
            )
```

Step 2：添加审计查询路由（只读）

```python
# backend/conversation-service/app/api/audit.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from ..dependencies import get_db

router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit"])

@router.get("")
async def list_audit_logs(
    session_id: str | None = Query(None, description="按会话 ID 过滤"),
    tool_name: str | None = Query(None, description="按工具名称过滤"),
    risk_level: int | None = Query(None, description="按风险等级过滤"),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """查询工具调用审计日志（只读，无删除接口）"""
    ...
    # 返回分页结果，含总数
```

Step 3：将 AuditService 集成到 conversation_service.py

```python
# 在服务初始化中（conversation_service.py）
from .audit_service import AuditService

audit_service = AuditService(db=db_session)
react_executor = ReactExecutor(
    ...
    audit_service=audit_service,    # 替换 Task 10 中的占位符
    ...
)
```

Step 4：验证

```bash
# 触发一次工具调用（如 get_active_alerts）
curl -X POST http://localhost:8002/api/v1/conversations/{session_id}/messages \
  -d '{"content": "查看当前有哪些告警"}'

# 查询审计日志，确认记录已写入
curl http://localhost:8002/api/v1/audit-logs?tool_name=get_active_alerts
# 预期：返回包含本次工具调用的记录，含 duration_ms、result 字段

# 数据库直查
docker compose -f deploy/docker/docker-compose.yml exec postgres \
  psql -U hci_user -d hci_db \
  -c "SELECT tool_name, risk_level, duration_ms, error FROM tool_audit_log ORDER BY started_at DESC LIMIT 5"
```

【约束】
- AuditService.write() 内部所有异常必须 try/except 捕获，不可向上抛出
- 不添加 DELETE /audit-logs 路由（审计记录只增不删）
- result 字段截断到 2000 字符（防止大响应撑爆 DB）
- 所有代码注释使用中文

【验收标准】
- [ ] 执行工具调用后，tool_audit_log 表有对应记录
- [ ] GET /api/v1/audit-logs 返回分页列表
- [ ] AuditService.write() 在 DB 连接中断时不抛异常（仅打 error 日志）
- [ ] 无 DELETE /audit-logs 路由
- [ ] uv run pytest backend/conversation-service/tests/test_audit_service.py -v 通过
- [ ] make lint 无新增错误
```
