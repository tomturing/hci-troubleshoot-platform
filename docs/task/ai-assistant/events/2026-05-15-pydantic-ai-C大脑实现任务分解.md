# pydantic-ai C 大脑实现任务分解

**任务编号**：C-Brain-001  
**创建日期**：2026-05-15  
**状态**：✅ Phase 1 完成  
**关联方案**：[大脑可选-集成重设计方案](../../solution/ai-assistant/大脑可选-集成重设计方案.md) §11 C 方案

---

## 1. 背景与目标

在 A（HTPBrainAdapter/OpenClaw）、B（OpsAgentBrainAdapter）大脑之外，
引入 C 大脑（pydantic-ai）进行三向 A/B/C 测试：

| 方案 | 大脑 | assistant_type | 路由标识 |
|------|------|---------------|---------|
| A | HTPBrainAdapter + OpenClaw | openclaw/glm-* | 默认（else 分支） |
| B | OpsAgentBrainAdapter | ops-agent | `OPS_AGENT_TYPE` |
| **C** | **PydanticAIBrainAdapter** | **pydantic-ai** | **`PYDANTIC_AI_TYPE`** |

C 方案核心价值：
- 用 pydantic-ai Agent 图替代手写 ReAct 循环
- `@agent.tool` 自动生成 JSON Schema，工具注册零样板代码
- pydantic-ai 内部透明处理工具调用循环，对外只暴露文本流
- 通过 `OpenAIModel(base_url=GLM_URL)` 零成本对接已有 GLM 端点

---

## 2. 任务清单

| 编号 | 文件 | 描述 | 状态 |
|------|------|------|------|
| T1 | `backend/kb-service/app/routes/sop_ingest.py` | 新增 `GET /{document_id}/tree` 端点 | ✅ |
| T2 | `backend/conversation-service/app/services/kb_client.py` | 新增 `get_sop_tree(document_id)` 方法 | ✅ |
| T3 | `backend/conversation-service/app/adapters/pydantic_ai_brain_adapter.py` | 新建 C 大脑适配器 | ✅ |
| T4 | `backend/conversation-service/app/adapters/brain_router.py` | 增加 pydantic-ai 路由分支 | ✅ |
| T5 | `backend/conversation-service/requirements.txt` | 添加 `pydantic-ai[openai]>=0.0.50` | ✅ |
| T6 | `backend/conversation-service/app/config.py` | 新增 `PYDANTIC_AI_ENABLED: bool = False` | ✅ |
| T7 | `backend/conversation-service/app/main.py` | 条件性构建并注入 `PydanticAIBrainAdapter` | ✅ |

---

## 3. 关键实现细节

### 3.1 消息格式转换（OpenAI → pydantic-ai）

`_openai_messages_to_pydantic()` 负责格式转换：

```python
# OpenAI 格式（输入）
messages = [
    {"role": "system", "content": "你是助手"},       # 忽略（Agent.system_prompt 处理）
    {"role": "user", "content": "我的虚拟机启动失败"},  # → ModelRequest(parts=[UserPromptPart(...)])（历史）
    {"role": "assistant", "content": "请提供错误信息"}, # → ModelResponse(parts=[TextPart(...)])
    {"role": "user", "content": "错误代码 E001"},       # → user_prompt（最后一条 user）
]

# pydantic-ai 格式（输出）
user_prompt = "错误代码 E001"
message_history = [
    ModelRequest(parts=[UserPromptPart(content="我的虚拟机启动失败")]),
    ModelResponse(parts=[TextPart(content="请提供错误信息")]),
]
```

### 3.2 工具注册（5 个只读工具）

```
get_sop_tree(document_id)      — KB Service SOP 决策树
get_active_alerts(limit)       — SCP API 活跃告警
get_vm_list(name_filter, limit)— SCP API 虚拟机列表
get_failed_tasks(task_type, limit) — SCP API 失败任务
get_cluster_detail(cluster_id) — SCP API 集群详情
```

### 3.3 Agent 构建策略

- **模块级单例**：`_AGENT` 在首次调用 `_get_agent()` 时初始化，避免每次请求重建
- **运行时 model 传入**：`agent = Agent(model=None, ...)`，实际模型通过 `agent.run_stream(model=self._openai_model, ...)` 传入，支持同一 Agent 使用不同模型实例
- **UsageLimits(request_limit=15)**：防止 ReAct 无限循环

### 3.4 pydantic-ai run_stream 关键 API

```python
async with agent.run_stream(
    user_prompt,
    message_history=message_history,  # list[ModelMessage]
    model=self._openai_model,          # 运行时注入
    deps=deps,                         # PydanticAIDeps 注入工具依赖
) as streamed:
    async for text in streamed.stream_text(delta=True):
        if text:
            yield BrainTextChunk(content=text)
```

---

## 4. 启用方式

在 conversation-service 的 Helm values 或 ConfigMap 中设置：

```yaml
env:
  - name: PYDANTIC_AI_ENABLED
    value: "true"
  # 以下已有配置，无需重复设置（pydantic-ai 复用 GLM 端点）
  - name: OPENCLAW_BASE_URL
    value: "http://your-glm-endpoint"
  - name: OPENCLAW_API_KEY
    value: "your-api-key"
  - name: GLM_MODEL
    value: "glm-4-flash"
  # SCP 工具调用（可选，未配置时工具返回错误提示）
  - name: SCP_BASE_URL
    value: "http://192.168.1.100:8082"
  - name: SCP_API_KEY
    value: "your-scp-token"
```

前端发送请求时 `assistant_type` 设为 `"pydantic-ai"` 即可路由到 C 大脑。

---

## 5. Phase 2 扩展方向

- acli 工具：`acli_system_top`、`acli_vm_info`（需要 SSH 节点信息注入）
- 写操作工具：通过 `BrainInteractiveRequest` 实现高危工具的用户确认流程
- 流程记忆：利用 pydantic-ai `message_history` 实现跨轮工具调用的上下文保持
- SOP 树遍历：`get_sop_tree` 返回后，Agent 通过多轮 `get_active_alerts` 验证各分支的 prerequisites

---

## 6. 完成标准

| 验证项 | 标准 |
|--------|------|
| 单测 | `tests/unit/test_pydantic_ai_brain_adapter.py` 所有用例通过 |
| 路由测试 | `assistant_type=pydantic-ai` 请求正确路由到 C 大脑 |
| 降级测试 | `PYDANTIC_AI_ENABLED=false` 时返回降级通知并切换到 htp 大脑 |
| 工具测试 | `get_active_alerts` / `get_sop_tree` 工具调用返回预期格式 |
| 流式输出 | `stream_text(delta=True)` 输出每个文本增量作为 `BrainTextChunk` |
| SOP 树端点 | `GET /api/sop/{id}/tree` 返回 200/404 正确状态码 |
