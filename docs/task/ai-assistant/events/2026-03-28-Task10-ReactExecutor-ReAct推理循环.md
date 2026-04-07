---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 10
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