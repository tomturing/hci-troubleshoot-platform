# 修复 React 引擎工具参数 JSON 序列化缺陷与文档归档

本实施方案针对 HTP-Agent 在 Staging 环境中测试发现的 `AI_UPSTREAM_ERROR: AI 服务请求参数错误: A parameter specified in the request is not valid` 问题，提供第一性原理的深度成因分析及彻底解决该类问题的方案，同时对全流程测试指南文档进行归档。

---

## 1. 第一性原理原因分析 (First-Principles Analysis)

### 1.1 问题的根本原因
当 ReactEngine 进行 ReAct 推理循环时，每个工具执行完成后，需要将本次工具调用的元数据及结果追加到对话历史中，作为下一次调用 LLM 的上下文。

在 OpenAI Function Calling 协议中，助理（assistant）的工具调用响应消息结构如下：
```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_xxxxx",
      "type": "function",
      "function": {
        "name": "acli_exec",
        "arguments": "{\"command\": \"task get -s failed -l 10\"}"
      }
    }
  ]
}
```
其中，`arguments` 必须是一个**符合 JSON 格式的字符串**。

然而，在 [react_engine.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/adapters/agents/htp/react_engine.py) 的第 199 行，代码为：
```python
"arguments": str(tc.arguments),
```
1. `tc.arguments` 在 LLM 客户端层已被解析为 Python 的字典（`dict`）类型。
2. 调用 Python 内置的 `str()` 序列化字典会生成带有**单引号**的字符串，例如 `"{'command': 'task get -s failed -l 10'}"`。
3. 单引号字符串是**非法的 JSON 串**。
4. 当 ReactEngine 将这个非法的 `assistant_msg` 消息作为历史记录，在下一步的 `ai_client.invoke(...)` 中传给火山引擎（火山方舟/豆包 API）等严格遵循 OpenAI OpenAPI Schema 的模型服务时，上游网关做强校验，直接抛出 `400 BadRequest` (`A parameter specified in the request is not valid`) 异常。
5. 这导致只要前一步触发了带参数的工具调用，下一次推理迭代就必然会崩掉。

### 1.2 为什么之前的单元测试没有发现
在 `test_react_engine_no_duplicate.py` 等本地单元测试中，`ai_client` 处于 Mock 状态，直接返回预置的 Mock 数据，并不实际请求真实的大模型 API Gateway，因而没有触发上游对输入消息列表中 `arguments` 字段 JSON 格式的强校验。

---

## 2. 彻底避免此类问题的全局审计与规范

为了避免类似的“测试一步出一个问题，耗时太长”的痛点，我们对全系统可能存在的“Python 数据类型序列化到外部服务协议”进行了全局审计：

1. **大模型请求数据流向审计**：
   - 经排查，只有 `ReactEngine` 涉及多轮 ReAct 并手动重建 `assistant` 的 `tool_calls` 消息往历史记录里回传。
   - `KBDDiagnostic` 中的 LLM 调用（批量判断、诊断报告生成）以及 `TriageAgent`（意图识别）均为**单次调用或无工具调用**，不涉及多轮 React 回溯工具历史，因此没有重建 `tool_calls` 消息的行为。
2. **微服务间 JSON 数据交换审计**：
   - 本项目通过 `httpx` 发送微服务间请求时，全部使用 `json=payload_dict` 形式。`httpx` 内部会自动使用标准的 `json.dumps` 序列化，不会产生 `str(dict)` 传参错误。
3. **ORM JSON 字段入库审计**：
   - 数据库中的 JSON/JSONB 字段（如 `context_variables` 等），其转换和反序列化由 SQLAlchemy ORM 层的 `JSON` 类型自动处理，在驱动层（`asyncpg`）自动完成 JSON 序列化，不会产生格式异常。

**防范规范**：
- **规则 1**：任何回传给 LLM 的结构化参数字段（`arguments`、`response_format` 等），凡是属于 `string` 类型的，必须强类型使用 `json.dumps()` 转换，严禁直接使用 Python 内置的 `str()`。
- **规则 2**：在单元测试中增加真实 Schema 结构性校验的 Assert，即使是 Mock 数据，也要校验重建的历史消息中 `tool_calls[*].function.arguments` 是能够被 `json.loads` 解析的合法 JSON 字符串。

---

## 3. 拟修改文件清单

### [Component: agent-service]

#### [MODIFY] [react_engine.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/adapters/agents/htp/react_engine.py)
- 导入内置 `json` 模块（若未导入）。
- 将 `"arguments": str(tc.arguments)` 替换为 `"arguments": json.dumps(tc.arguments or {})`。

---

### [Component: docs]

#### [NEW] [htp-agent全流程测试及数据库内容校验.md](file:///aihci/hci-troubleshoot-platform/docs/verify/agent/htp-agent%E5%85%A8%E6%B5%81%E7%A8%8B%E6%B5%8B%E8%AF%95%E5%8F%8A%E6%95%B0%E6%8D%AE%E5%BA%93%E5%86%85%E5%AE%B9%E6%A0%A1%E9%AA%8C.md)
- 将该文档移入 `docs/verify/agent/` 目录，并纳入 git 跟踪。

---

## 4. 验证与回归测试计划

### 自动化单元测试
- 运行整个 `agent-service` 的单元测试：
  ```bash
  uv run pytest backend/agent-service/tests/unit/ -v
  ```
- 重点验证 `test_react_engine_no_duplicate.py` 中对 ReAct 逻辑的覆盖。
- 增加一个针对 `react_engine` 输出格式的断言检查，确认 `tool_calls` 中的 `arguments` 确实为合法的 JSON 字符串。

### 手动验证
- 将代码同步到 Staging 环境。
- 以工单 `Q2026060588858` 为例，运行包含工具调用的排障流程，验证第 7 步大模型不再返回 `400 BadRequest`，React 循环能够顺利跑通。
