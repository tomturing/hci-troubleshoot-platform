# Prompt 审计机制与上下文可观测性设计

> 版本：v1.0 | 日期：2026-04-24 | 状态：active

本文档详细说明 HCI 智能排障平台的 Agent 上下文可观测性设计，包括：
1. 数据流分析：`message` 表 vs `audit_log.payload.messages` 的区别
2. Prompt 审计机制：如何查看 Agent 收到的完整上下文
3. Grafana Dashboard 配置：可视化审计数据

---

## 1. 核心结论：两个数据源的职责分工

### 1.1 第一性原理分析

通过代码追踪 `send_message_stream_only()` 方法的数据流，得出以下结论：

| 维度 | `message` 表 | `audit_log.payload.messages` |
|------|-------------|------------------------------|
| **存储内容** | 只有用户和AI对话 | **完整messages数组** |
| **包含System Prompt** | ❌ 不包含 | ✅ 包含（第一项，role=system） |
| **包含环境信息** | ❌ 不包含 | ✅ 包含（Segment 4） |
| **包含分类列表** | ❌ 不包含 | ✅ 包含（Segment 5） |
| **用途** | 展示到custom-ui对话界面 | **审计Agent收到的完整上下文** |
| **写入时机** | 用户发消息时、AI回复完成时 | Prompt构建后 |
| **采样策略** | 100%采集 | 10%采样（节省存储开销） |

### 1.2 数据流追踪图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    用户发送消息 send_message_stream_only()                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Step 1: 用户消息入库                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │ message 表 INSERT                                                  │      │
│  │   role = MessageRole.user                                          │      │
│  │   content = 用户输入的文本                                          │      │
│  │   ❌ 不包含 system prompt                                          │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                              ↓                                              │
│  Step 2: 构建 System Prompt                                                 │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │ _build_system_prompt() 返回：                                      │      │
│  │   system_prompt = 7 个 Segment 拼接字符串：                        │      │
│  │     - Segment 1: 专家身份定义                                       │      │
│  │     - Segment 2: 诊断方法论                                         │      │
│  │     - Segment 3: HCI机制知识                                        │      │
│  │     - Segment 4: 环境信息/告警/任务日志 ← 从environment表获取      │      │
│  │     - Segment 5: 198个分类列表                                      │      │
│  │     - Segment 6: 输出格式规范                                       │      │
│  │     - Segment 7: 工单上下文                                         │      │
│  │   ⚠️ system_prompt 从不入库 message 表                            │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                              ↓                                              │
│  Step 3: 组装完整 messages 数组                                              │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │ conversation_service.py 第326-330行（关键代码）：                   │      │
│  │                                                                    │      │
│  │ history_messages: list[dict] = [{"role": "system",                 │      │
│  │                                   "content": system_prompt}]       │      │
│  │                                                                    │      │
│  │ selected_messages = all_messages[-20:]  # 从message表读取          │      │
│  │                                                                    │      │
│  │ for msg in selected_messages:                                      │      │
│  │     history_messages.append({"role": msg.role.value,               │      │
│  │                              "content": msg.content})              │      │
│  │                                                                    │      │
│  │ 结果：history_messages = [                                         │      │
│  │   {"role": "system", "content": "完整7段Prompt..."},               │      │
│  │   {"role": "user", "content": "第1轮用户输入"},                     │      │
│  │   {"role": "assistant", "content": "第1轮AI回复"},                  │      │
│  │   {"role": "user", "content": "第2轮用户输入"},                     │      │
│  │   {"role": "assistant", "content": "第2轮AI回复"},                  │      │
│  │   {"role": "user", "content": "当前用户输入"},                      │      │
│  │ ]                                                                  │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                              ↓                                              │
│  Step 4: 调用 AI                                                            │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │ ai_client.chat_completion_stream(                                  │      │
│  │     messages=history_messages,  ← 传入完整数组给Agent              │      │
│  │     ...                                                            │      │
│  │ )                                                                  │      │
│  │                                                                    │      │
│  │ ⚠️ Agent 收到的就是 history_messages                               │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                              ↓                                              │
│  Step 5: 审计入库                                                           │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │ 第344行：                                                          │      │
│  │ _sample_payload = history_messages if random()<0.10 else None     │      │
│  │                                                                    │      │
│  │ _write_prompt_audit() 写入 audit_log：                             │      │
│  │   payload={"messages": sample_payload, ...}                       │      │
│  │                                                                    │      │
│  │ ⚠️ audit_log.payload.messages = history_messages（完整数组）       │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                              ↓                                              │
│  Step 6: AI回复入库                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │ save_assistant_message() → message 表 INSERT                       │      │
│  │   role = MessageRole.assistant                                     │      │
│  │   content = AI回复文本                                             │      │
│  │   ❌ 不包含 system prompt                                          │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 数据示例对比

**audit_log.payload.messages 结构**（Agent收到的完整上下文）：
```json
[
  {
    "role": "system",
    "content": "你是深信服HCI智能排障专家...\n【当前阶段：S0意图识别】\n【系统上下文信息】\n## 当前环境信息\n- HCI 版本：6.8.1\n- 集群名称：prod-cluster-01\n...\n【故障分类基准】(共198个)\n### 虚拟机域（45个）\n- 虚拟机-001 虚拟机创建失败\n..."
  },
  {
    "role": "user",
    "content": "虚拟机开机失败"
  },
  {
    "role": "assistant",
    "content": "根据告警日志分析..."
  },
  {
    "role": "user",
    "content": "第二条用户输入"
  }
]
```

**message 表内容**（仅对话内容）：
```sql
SELECT role, content FROM message WHERE conversation_id = 'xxx';
-- 结果：
-- user: "虚拟机开机失败"
-- assistant: "根据告警日志分析..."
-- user: "第二条用户输入"
```

---

## 2. 如何查看 Agent 是否收到系统上下文

### 2.1 问题诊断流程

当怀疑 Agent 未收到环境上下文时，按以下流程排查：

**步骤1：检查数据库 environment 表是否有数据**
```sql
SELECT case_id, env_type, env_data, collected_at 
FROM environment 
WHERE case_id = '目标工单ID'
ORDER BY collected_at DESC;
```

如果返回空，说明前端未采集数据 → 检查前端 CaseCreateDialog.vue 的 SSH 命令执行。

**步骤2：检查日志（Loki）**
```logql
{container=~"hci-conversation.*"} | json
| event=~"environment_context.*|s0_context_info_loaded"
| case_id="目标工单ID"
```

关键事件：
- `environment_context_loaded` — 成功加载环境数据
- `environment_context_empty` — 返回404（工单无环境数据）
- `environment_context_failed` — HTTP失败
- `environment_context_timeout` — 5秒超时
- `s0_context_info_loaded` — 成功注入Prompt

**步骤3：直接调用 API 验证**
```bash
curl http://case-service:8001/api/environments/case/Q2026042453824/context | jq
```

**步骤4：查看 audit_log 采样记录**
```sql
SELECT 
  id,
  payload->'messages'->0->>'role' as first_role,
  LENGTH(payload->'messages'->0->>'content') as system_prompt_length,
  payload->'messages'->0->>'content' LIKE '%系统上下文信息%' as has_context_segment,
  payload->>'total_chars' as total_chars
FROM audit_log 
WHERE audit_type = 'prompt' 
  AND payload->'messages' IS NOT NULL
  AND conversation_id = '目标会话UUID'
ORDER BY started_at DESC 
LIMIT 1;
```

判断标准：
- `system_prompt_length ~ 10000` → 正常（含完整7段）
- `has_context_segment = true` → 环境信息已注入
- `total_chars ~ 6500` → 可能缺少环境信息

### 2.2 messages 为 null 的原因

**这是设计决策，不是bug**：

- 元数据字段（has_sop、kb_chunks_count、total_chars等）**100%采集**
- 完整 messages 数组**10%采样**（节省存储开销）
- 查到的记录恰好没被采样到

**临时调试方案**（调高采样率）：
```python
# conversation_service.py 第343行
_do_sample = True  # 临时改为100%采样
```

---

## 3. Grafana Prompt 审计 Dashboard 配置

### 3.1 新增文件

| 文件路径 | 作用 |
|---------|------|
| `deploy/observability/grafana/provisioning/dashboards/prompt-audit.json` | Prompt审计Dashboard配置 |
| `deploy/observability/grafana/provisioning/datasources/datasources.yml` | 新增PostgreSQL数据源 |
| `deploy/env/obs.env.example` | 新增 `GRAFANA_PG_PASSWORD` 配置 |

### 3.2 Dashboard 功能

| Panel | 功能 |
|-------|------|
| Prompt 构建次数 | 趋势图（Prometheus指标） |
| SOP 命中率 | 统计卡片（百分比） |
| KB chunks 注入分布 | 时间序列 |
| Prompt Token 估算分布 | 直方图 |
| 上下文结构 breakdown | 饼图（各Segment占比） |
| 最近 Prompt 审计记录详情 | 表格（50条记录） |
| 完整 Prompt Messages | 表格（仅采样记录，显示完整内容） |

### 3.3 部署步骤

```bash
cd deploy/observability
cp ../env/obs.env.example ../env/obs.env
# 编辑 obs.env 设置真实密码
docker compose -f docker-compose-obs.yml restart grafana
```

访问 Grafana → Dashboards → HCI → "HCI Prompt 审计分析"

### 3.4 PostgreSQL 数据源配置

```yaml
# datasources.yml 新增内容
- name: PostgreSQL
  uid: postgres
  type: postgres
  access: proxy
  url: postgres:5432
  user: hci_admin
  jsonData:
    database: hci_troubleshoot
    sslmode: disable
  secureJsonData:
    password: ${GRAFANA_PG_PASSWORD}
```

密码通过 `obs.env` 环境变量注入：
```bash
GRAFANA_PG_PASSWORD=dev_password_123
```

---

## 4. 相关文件索引

| 文件 | 作用 |
|------|------|
| `backend/conversation-service/app/services/conversation_service.py` | Prompt构建和审计写入主逻辑 |
| `backend/conversation-service/app/services/prompt_builder.py` | S0 Prompt 7段构建器 |
| `backend/conversation-service/app/services/environment_client.py` | 调用 case-service 获取环境数据 |
| `backend/case-service/app/routes/environments.py` | Environment API 路由 |
| `backend/case-service/app/services/environment_service.py` | build_context_info() 实现 |
| `backend/shared/models/schemas.py` | EnvironmentContextResponse 定义 |
| `backend/shared/models/audit.py` | AuditLog ORM 模型 |
| `database/desired_schema.sql` | audit_log 表结构定义 |

---

## 5. 更新历史

| 日期 | 变更 |
|------|------|
| 2026-04-24 | 初版创建，完整分析数据流差异 |
| 2026-04-24 | 新增 Grafana Prompt 审计 Dashboard |
| 2026-04-24 | 新增 PostgreSQL 数据源配置 |