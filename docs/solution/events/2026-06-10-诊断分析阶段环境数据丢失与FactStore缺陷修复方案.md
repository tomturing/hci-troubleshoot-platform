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
