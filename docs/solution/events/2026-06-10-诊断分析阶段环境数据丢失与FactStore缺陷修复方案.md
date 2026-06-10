# 2026-06-10-诊断分析阶段环境数据丢失与 FactStore 缺陷修复方案

## 1. 问题现象与表现

在超融合故障排障平台的实际运行中，工单 `Q2026060935237` 的 S0-S1 排障流程触发了以下两个异常：

1. **AI 助手报错**：
   ```
   [Agent Error: AI 推理未返回任何内容，可能是模型服务异常或配置问题]
   ```
2. **交互提示环境数据为空**：
   ```
   环境数据为空，无法开始诊断推理
   需要补充环境信息
   ```

然而，经检查发现，数据库的 `environment` 数据表中确实存在当前工单关联的 `cluster` / `alert` / `task` 采集数据。

---

## 2. 根因分析

该故障本质上是由**通信请求端、校验逻辑端、底层数据库 Schema** 三个维度的缺陷叠加导致的闭环失效：

### 2.1 请求端阶段转换时序 Bug (`conversation-service`)
在 `conversation_service.py` 中，用户在 `S0` 阶段选定故障分类（如 `【硬件-024 硬盘寿命到期】`）时，系统首先将内存中的诊断阶段修改为 `S1`。
紧接着，在加载环境上下文（Segment 4 数据）时，条件判断硬编码为 `if current_stage == "S0"`：
```python
# conversation_service.py
context_info: dict | None = None
if current_stage == "S0" and self.environment_client:
    # 此时 current_stage 已提前被修改为 "S1"
    # 条件评估为 False，导致跳过加载，最终下发 env_context = None
```
由于此 Bug，进入 `S1` 推理时，发送给 downstream 的 `env_context` 变为了 `None`。

### 2.2 校验端逻辑过于严苛 Bug (`agent-service`)
即使在 S0 阶段曾经成功读取并将环境事实写入过 Redis，在 `EvidenceBuilder.check_information_quality` 中，校验逻辑过于绝对：
```python
# evidence_builder.py
# 即使 FactStore/Redis 里有缓存，只要本次请求参数 env_context 为空，便强行拦截报错
if not env_context:
    report.clarification_reason = "环境数据为空，无法开始诊断推理"
    return report
```
这导致 FactStore 历史持久化/缓存的兜底功能失效。如果本次请求的入参 `env_context` 为 `None`，Agent 将立刻生成交互澄清请求并直接 `return` 退出，不进行任何 LLM 推理。

### 2.3 容器推理空流报警拦截
由于 Agent 提前 `return` 退出且没有产生任何 `AgentTextChunk`，在 `agent.py` 的流式响应端点 `_event_stream` 中触发了空流防护检测：
```python
if not _has_text_chunk:
    yield _sse({"type": "error", "message": "AI 推理未返回任何内容..."})
```
导致前端界面额外弹出了 `[Agent Error]` 提示。

### 2.4 底层基础设施缺失（无 Fact/Claim Table）
经排查，虽然在 python 代码中定义了 `shared.models.fact.Fact` 事实表，但它从未被合并入主 Schema 文件 `database/desired_schema.sql`，导致 PostgreSQL 数据库中没有这两张表。
这使得 `FactStore.write` 每次将事实数据持久化到 PG 时均会因表缺失而抛出异常，阻止了 Redis 缓存的写入，使 `FactStore` 机制整体瘫痪，系统百分之百依赖于每轮传递的 `env_context` 参数。

---

## 3. 解决方案设计

本方案从以下三个方向进行修复，使系统设计形成闭环：

### 3.1 修复一：扩展环境上下文的加载阶段范围
修改 `conversation_service.py`。获取环境信息的条件由仅限 `S0` 扩展为支持诊断和验证相关的核心阶段（`S0` 至 `S4`）。

```python
# backend/conversation-service/app/services/conversation_service.py
if current_stage in ("S0", "S1", "S2", "S3", "S4") and self.environment_client:
```

### 3.2 修复二：允许 FactStore 对校验进行兜底
修改 `evidence_builder.py` 中的 `check_information_quality` 逻辑。在参数 `env_context` 为空时，先查询 `FactStore` 是否包含已存储的事实。只有当两者均为空时，才发起澄清卡片。

```python
# backend/agent-service/app/services/evidence_builder.py
has_stored_facts = False
if self._fact_store:
    stored_packets = await self._fact_store.read_all_types(
        session_id,
        fact_types=["vm_status", "host_status", "alert_status", "task_status", "env_inject"]
    )
    if stored_packets:
        has_stored_facts = True

if not env_context and not has_stored_facts:
    report.clarification_reason = "环境数据为空，无法开始诊断推理"
    return report
```

### 3.3 修复三：补全数据库 DDL 并应用迁移
在 `database/desired_schema.sql` 中补全 `fact` 与 `claim_evidence_link` 的建表 DDL 及索引结构，并在开发环境运行 `atlas schema apply --env local --auto-approve` 以使 `FactStore` 在 PG 数据库层落地生效。

---

## 4. 2026-06-10 补充修复：命令执行超时与诊断阶段显示修复

### 4.1 终端 Bridge `exec_id` 连字符不一致 Bug
- **原因**：前端 `terminal.ts` 对 UUID `execID` 进行了剔除连字符（`-`）处理并截取前 16 位用于构建 SSH 执行完毕 marker。而 `terminal_bridge/main.go` 并没有做剔除连字符处理，导致切片前 16 位匹配 marker 失败。
- **修复**：修改 `terminal_bridge/main.go` 中的 `checkMarkers` 和 `on_output_start`，将 `execID` 的连字符替换为空白后再提取前 16 位以和前端对齐。

### 4.2 前端工单切换/刷新丢失诊断阶段 Bug
- **原因**：在前端 `chat.ts` 中，切换工单或页面初始化时调用 `loadConversationHistory` 只加载了对话消息，没有将从数据库获取的最新诊断阶段 `conv.diagnostic_stage` 恢复给前端 `diagnosticStage.value` 响应式变量，导致前台界面重新加载时回退到默认的 `S0`。
- **修复**：修改 `loadConversationHistory` 方法，在获取会话详情后，将 `conv.diagnostic_stage` 同步还原给 `diagnosticStage.value`。

### 4.3 意图质量校验空列表拦截 Bug
- **原因**：在 `evidence_builder.py` 的 `check_information_quality` 中，检查必填字段时，逻辑为 `if not val or val in ("", "N/A", "暂无数据", [], {}):`。当某些没有对应活跃任务或告警的工单数据被注入时，其 `task_logs` 或 `alert_logs` 表现为合法的空列表 `[]`。原逻辑会将 `[]` 误判为缺失字段，导致触发澄清拦截，提示“以下环境信息缺失：任务日志”。
- **修复**：修改必填校验，仅当 `val` 为 `None`，或属于特定占位符字符串时视为缺失；空列表 `[]` 不再被视为缺失状态。

### 4.4 澄清请求空流错误拦截 Bug
- **原因**：在 `agent.py` 的 `_event_stream` 流式处理中，程序仅根据 `_has_text_chunk` 这一单一标识符来判定是否输出过内容。当触发质量检查并向用户发起澄清交互请求时，程序只 yield 了 `AgentInteractiveRequest` 这一有效交互事件并提前返回，而没有产生任何 `AgentTextChunk`。这导致 `_has_text_chunk` 保持为 `False`，使得末尾的空流防护守卫逻辑误判本次推理为“空流”，进而错误地向前端输出了 `[Agent Error: AI 推理未返回任何内容...]`。
- **修复**：在 `_event_stream` 中引入 `_has_interactive_request` 和 `_has_escalation` 两个状态位，当事件类型为 `AgentInteractiveRequest` 或 `AgentEscalation` 时分别置位；空流守卫的判定条件修正为 `if not _has_text_chunk and not _has_interactive_request and not _has_escalation:`，避免合法交互/升级事件被误判为推理错误。

### 4.5 命令行工具执行缺少 case_id 与超时时间过长修复
- **原因一（缺少 case_id）**：`BridgeRelayExecutor` 发送执行请求 `/internal/conversations/{conversation_id}/agent-exec` 时，其 `"case_id"` 键仅尝试从 `args.get("case_id", "")` 读取，但 `args` 作为 LLM 的入参字典并不携带此系统级上下文，导致 `"case_id"` 总是为空。前端在收到事件后传递 `"case_id": ""` 至 terminal_bridge，致使 terminal_bridge 因无法根据空 case 寻找到已注册的 SSH 实例而报错 `会话不存在`，阻断命令执行。
- **原因二（超时过长）**：原有命令执行等待超时设置在 `agent-service` 端为 `BLPOP_TIMEOUT = 32`，而在前端为 `35_000`（35秒）。对于故障命令，这个等待时间过长，不利于用户快速得到失败回显。
- **修复**：
  1. 在 `BridgeRelayExecutor.execute` 和 `ReactEngine._execute_tool_call` 的参数中支持显式传递 `case_id`，从而能够将正确的工单 ID 发送到 backend 并流式推送到前端。
  2. 将 `BridgeRelayExecutor` 的 `BLPOP_TIMEOUT` 缩短为 `10` 秒，HTTP Client 连接超时缩短为 `12.0` 秒。
  3. 将前端 `chat.ts` 中 `waitForExecResult` 的等待超时从 `35_000` 缩短为 `10_000`（10秒），实现故障命令的快速报错返回。
