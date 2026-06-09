# HTP-Agent 全流程测试与数据库内容校验指南

本指南整合了 **HTP-Agent** 诊断全链路测试中发现的典型问题及原因分析，并归档了诊断执行各个阶段在 PostgreSQL 数据库中的内容校验方法。

---

## 一、 历史问题分类归档 (Problem Classification)

在 HTP-Agent 的 SOP 诊断引擎与审计系统的开发和集成测试中，我们遇到了三类典型问题：

### 📁 分类 A：ORM 启动期懒加载编译冲突（时序与懒加载）
* **代表案例**：大模型 Prompt 审计日志 (`audit_log`) 没有任何数据。
* **现象描述**：推理正常流转，但数据库 `audit_log` 表一直为空。后台仅在 `agent-service` 打印 `WARNING` 警告：
  > `"大模型审计写入失败（已自行隔离）: Foreign key associated with column 'audit_log.system_prompt_id' could not find table 'system_prompt'"`
* **第一性原理分析**：
  1. `AuditLog` ORM 模型字段 `system_prompt_id` 物理上存在指向 `system_prompt` 表的外键关联。
  2. `agent-service` 为诊断推理服务，未显式导入 `SystemPrompt` 模型类。
  3. 当异步协程首次写 `AuditLog` 触发元数据编译时，SQLAlchemy 因缺少关联元数据抛出 `NoReferencedTableError`。
  4. 审计的 Try-Except 安全隔离机制捕获并静默吞没了此异常。
* **彻底根治策略**：
  在各微服务启动 lifespan 首行强制引入 `configure_mappers()` 编译门禁，确保所有模型在主线程被完整预加载和编译。如有缺失，服务拒绝就绪直接 Crash。

---

### 📁 分类 B：微服务架构下的跨服务 ORM 外键设计缺陷
* **代表案例**：`sop_execution` 表没有数据。
* **现象描述**：当工单确认为 `硬件-024 硬盘寿命到期` 时，应该触发 SOP 执行，但数据库 `sop_execution` 表没有生成记录。
* **第一性原理分析**：
  1. `conversation-service` 中的 `SopExecution` 模型误定义了 ORM 级强约束 `ForeignKey("sop_document.id")`。
  2. 但 `sop_document` 表由 `kb-service` 独占定义，`conversation-service` 运行时环境中找不到此表元数据。
  3. 执行 `POST /sop/create` 时，SQLAlchemy 动态编译出错，接口直接抛出 **HTTP 500 错误**。
  4. `InvestigationAgent` 捕获 500 异常后，为防止排障中断，自动执行降级逻辑，切换回普通 LLM 对话的 Fallback 轨道，未写入 `sop_execution` 记录。
* **彻底根治策略**：
  取消微服务 ORM 代码级跨服务的 `ForeignKey` 物理声明，使跨服务主键引用在 ORM 层退化为普通数值列（如 `Column(Integer)`），一致性交由服务应用层逻辑或物理库底层的强约束。

---

### 📁 分类 C：类与函数调用的方法参数签名不一致
* **代表案例**：`ReactEngine.execute() got an unexpected keyword argument 'sop_mode'`。
* **现象描述**：从分类引导卡片点击确认后，AI 推理中途报错：
  > `[Agent Error: 推理异常: ReactEngine.execute() got an unexpected keyword argument 'sop_mode']`
* **第一性原理分析**：
  1. 历史重构合并分支时，[investigation_agent.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/adapters/agents/htp/investigation_agent.py) 在启动 SOP 诊断时调用了 `react_engine.execute(..., sop_mode=True)`。
  2. 但 [react_engine.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/adapters/agents/htp/react_engine.py) 的 `execute` 方法参数列表中丢失了该入参定义，导致 Python 抛出 `TypeError` 签名不匹配异常。
* **彻底根治策略**：
  1. 补齐 `ReactEngine.execute()` 中的 `sop_mode: bool = False` 参数。
  2. 整合 `_get_tools_for_llm(sop_mode=sop_mode)` 工具过滤逻辑。当 `sop_mode=False` 时，过滤掉 category="sop" 的高频工具，降低非 SOP 对话场景的 Token 开销。

---

### 📁 分类 D：ReAct 消息历史工具参数 JSON 序列化缺陷（DC-02）
* **代表案例**：`AI_UPSTREAM_ERROR: AI 服务请求参数错误: A parameter specified in the request is not valid`（Staging 工单 `Q2026060588858`，第 7 步推理时崩溃）。
* **现象描述**：从 S0 分类引导点击确认进入 S1 SOP 诊断后，AI 正常调用第一轮工具（如 `get_sop_node`）并获取结果。但下一次调用 LLM 时，系统抛出 HTTP 400 错误：
  > `AI_UPSTREAM_ERROR: AI 服务请求参数错误: A parameter specified in the request is not valid Request id: 02178...5776d`
* **第一性原理分析**：
  1. OpenAI Function Calling 协议要求，回传给 LLM 的 `assistant` 角色消息中，`tool_calls[*].function.arguments` 字段的值必须是**合法 JSON 格式字符串**（例如 `"{\"node_ip\": \"10.97.128.120\"}"` ）。
  2. 在 [react_engine.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/adapters/agents/htp/react_engine.py) 的 `execute()` 方法中，ReAct 每步循环执行完工具后，会重建 `assistant_msg` 追加到历史消息列表，其中有一行：
     ```python
     "arguments": str(tc.arguments),   # ← BUG 所在
     ```
  3. `tc.arguments` 是 Python `dict` 类型，`str()` 对 dict 调用时产生**Python 单引号格式**的字符串，例如 `"{'node_ip': '10.97.128.120'}"` ——这不是合法的 JSON 字符串。
  4. 下一次调用 LLM 时，消息列表中包含了这个非法的 `arguments` 字段。火山引擎（Volcengine Ark）等严格遵循 OpenAI OpenAPI Schema 的上游 API 对请求体做结构性校验，发现 `arguments` 无法被 JSON 解析，直接拒绝并返回 `400 BadRequest`。
  5. 此错误仅在工具带参数调用后的下一轮 LLM 请求时才触发（第一轮无此历史，因此不报错）。
* **为什么单元测试没有发现**：
  - 本地单元测试中 `ai_client` 全部为 Mock，不请求真实 API Gateway，绕过了上游对请求体的 Schema 强校验。测试通过并不等于真实链路通过。
* **彻底根治策略**：
  1. 将 `str(tc.arguments)` 替换为 `json.dumps(tc.arguments or {})`，确保输出始终为合法的 JSON 字符串。
  2. 在单元测试中追加 Schema 校验断言：对 `invoke.call_args_list` 中所有 `assistant` 历史消息的 `arguments` 字段，断言能被 `json.loads()` 成功反序列化（防止回归）。
  3. **编码规范强化**：凡是将 Python dict 序列化为字符串字段并传给外部 API 的场景，必须使用 `json.dumps()`，严禁使用 `str()`。

---

## 二、 数据库内容校验 SQL 汇总 (Database Verification)

在测试 HTP-Agent 全流程时，请登录 PostgreSQL 数据库，按以下顺序逐步验证各个业务表的数据生成状态。

### 1. 工单表校验 (`case`)
确认工单已成功创建、分类正确，且分配给 `htp-agent`。
```sql
SELECT case_id, title, status, priority, category, assistant_type, created_at 
FROM "case" 
WHERE case_id = 'Q2026060600001'; -- 替换为您的测试工单号
```

### 2. 对话会话表校验 (`conversation`)
确认会话与工单已绑定，并处于正确的诊断阶段（如 S1）。
```sql
SELECT conversation_id, case_id, diagnostic_stage, category_id, message_count 
FROM conversation 
WHERE case_id = 'Q2026060600001';
```

### 3. SOP 执行实例表校验 (`sop_execution`)
一旦进入 S1 阶段且对应分类匹配到 SOP，应生成 SOP 执行节点状态。
```sql
SELECT id, conversation_id, sop_document_id, current_node_id, status, context_variables, updated_at 
FROM sop_execution 
WHERE conversation_id = '{conversation_id}'; -- 替换为上述步骤查出的 UUID
```

### 4. 大模型 Prompt 审计表校验 (`audit_log`)
每次大模型回复，均应在这里产生一条审计记录（包含 Prompt 内容）。
```sql
SELECT id, conversation_id, system_prompt_id, user_prompt_id, response_content, created_at 
FROM audit_log 
WHERE conversation_id = '{conversation_id}' 
ORDER BY created_at DESC;
```

### 5. 工具执行审计表校验 (`tool_result`)
在 ReAct 循环或 SOP 诊断中调用任何工具（如 `get_sop_node`、`acli_system_top`），均在此留痕。
```sql
SELECT id, conversation_id, tool_name, tool_type, step_no, risk_level, policy, error, duration_ms, created_at 
FROM tool_result 
WHERE conversation_id = '{conversation_id}' 
ORDER BY step_no ASC;
```

---

## 三、 回归测试与质量门禁 (Testing & CI Gates)

为了防止上述四类问题再次发生，建立了以下代码质量与测试保护门禁：

### 1. 本地单元测试验证
修改 `react_engine.py` 后，必须在本地运行以下两个关键测试套件，并确保全部通过：
```bash
# 验证 ReactEngine 工具去重、工具过滤及历史消息 JSON Schema 合法性
uv run pytest backend/agent-service/tests/unit/test_react_engine_no_duplicate.py -v

# 验证 InvestigationAgent 路由与 SOP 执行流
uv run pytest backend/agent-service/tests/unit/test_investigation_agent.py -v
```

### 2. 强校验门禁（SQLAlchemy 启动前置编译）
任何新的 SQLAlchemy ORM 字段改动，在微服务 lifespan 中都必须保留编译逻辑：
```python
from sqlalchemy.orm import configure_mappers
configure_mappers()
```
如果编译期间有任何引用关系、外键缺失，微服务在部署自愈时会直接 Crash，阻止不合格代码带病上线。

### 3. 外部 API 序列化规范（防范分类 D 问题再次发生）
凡是构建传给上游大模型 API（或任何 HTTP JSON 服务）的请求体，涉及将 Python dict 字段序列化为字符串的场景，**必须使用 `json.dumps()`，严禁使用 `str()`**：

```python
# ❌ 错误写法（产生非法 JSON 单引号格式）
"arguments": str(tc.arguments)        # => "{'key': 'value'}"

# ✅ 正确写法（标准 JSON 格式）
"arguments": json.dumps(tc.arguments or {})   # => "{\"key\": \"value\"}"
```

### 4. 单元测试 Schema 合法性断言（新增要求）
对任何重建 `tool_calls` 消息历史的代码，单元测试中须添加以下断言，防止 `arguments` 字段格式回归：
```python
import json
# 从 invoke 调用的第二次参数中取 assistant 消息
messages = mock_client.invoke.call_args_list[1].kwargs["messages"]
for msg in messages:
    if msg.get("role") == "assistant" and msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            args_str = tc["function"]["arguments"]
            # 断言 arguments 是合法 JSON 字符串（可被反序列化）
            json.loads(args_str)   # 若为非法 JSON 则抛 json.JSONDecodeError，测试失败
```
