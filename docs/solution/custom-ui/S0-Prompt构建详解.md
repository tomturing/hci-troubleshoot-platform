# S0 阶段 Prompt 构建详解

> 版本：v1.0 | 日期：2026-04-24 | 状态：active

本文档详细说明创建工单后 S0 意图识别阶段的 System Prompt 构建细节，包括 7 个 Segment 的内容来源、存储位置和注入接口。

---

## Prompt 结构概览

```
System Prompt = Segment 1 + Segment 2 + Segment 3 + Segment 4 + Segment 5 + Segment 6 + Segment 7

总长度估算：
- Segment 1: ~200 字符
- Segment 2: ~500 字符
- Segment 3: ~300 字符
- Segment 4: 0~2000 字符（依赖环境数据）
- Segment 5: ~8000 字符（198 个分类）
- Segment 6: ~600 字符
- Segment 7: ~100 字符
- 总计：约 10000 字符 / 2500 tokens
```

---

## Segment 详解

### Segment 1：专家身份定义

**内容示例**：
```
你是深信服超融合基础设施（HCI）智能排障专家助手。
你掌握完整的 HCI 平台知识：虚拟机生命周期管理、分布式存储 ASAN、
vxlan 虚拟网络、IPMI 硬件管理、acli 诊断工具集（完整命令集）。
你的目标是协助现场工程师快速、精准地定位和解决 HCI 平台故障。
```

| 属性 | 值 |
|------|-----|
| **来源** | 代码常量（硬编码） |
| **存储位置** | `backend/conversation-service/app/services/prompt_builder.py` 第101-108行 `_segment_identity()` 方法 |
| **注入接口** | 无（固定内容） |
| **是否动态** | 否 |

---

### Segment 2：S0 意图识别方法论

**内容示例**：
```
【当前阶段：S0 意图识别】

⚠️ S0 是分类任务，不是对话任务。目标是得到 1 个 kb_category.code，不是通过追问收集更多信息。

工作流程：
1. **全量分析**：综合所有已有信息（客户描述 + 告警日志 + 任务日志），一次性完成推理
2. **置信度评估 → 两路分支**：
   ▶ 高置信度（≥80%）：直接输出确认标记
   ▶ 中置信度：展示候选选项，由用户确认
3. ⛔ 禁止追问：不得输出开放性问题
4. ⛔ 禁止硬猜：置信度不足时诚实呈现候选
```

| 属性 | 值 |
|------|-----|
| **来源** | 代码常量（硬编码） |
| **存储位置** | `backend/conversation-service/app/services/prompt_builder.py` 第187-209行 `_segment_s0_methodology()` 方法 |
| **注入接口** | 无（固定内容） |
| **是否动态** | 否 |

---

### Segment 3：HCI 核心机制知识

**内容示例**：
```
【HCI 核心机制知识】
虚拟机开机链路：用户触发 → vtpdaemon → kvm_runner → QEMU/KVM → 存储挂载 → 网络配置
开机失败 4 大方向：① 宿主资源不足（CPU/内存）② 存储不可访问
                  ③ 序列号/授权问题 ④ 平台服务异常（exporter/cfs/prometheus）
诊断首选命令：acli task get -v {vmid} -k '启动虚拟机' → 获取失败原因
快速预诊断：acli alert get -l 10 → 近期告警，acli task get -s failed -l 5 → 近期失败任务
```

| 属性 | 值 |
|------|-----|
| **来源** | 代码常量（硬编码保底知识） |
| **存储位置** | `backend/conversation-service/app/services/prompt_builder.py` 第84-89行 `_MECHANISM_KNOWLEDGE` 常量 |
| **注入接口** | 无（固定内容） |
| **是否动态** | 否 |

---

### Segment 4：系统上下文信息 ⚠️

**内容示例**（有数据时）：
```
【系统上下文信息】

## 当前环境信息
- HCI 版本：6.8.1
- 集群名称：prod-cluster-01
- 主机数量：3
- 存储类型：SSD-Pool
- 网络配置：vxlan

## 最新告警（3 条）
- [CRITICAL] 09:02 Node-02 磁盘 I/O 延迟持续 > 300ms
- [WARNING] 08:45 存储池 SSD-Pool-01 使用率 78%
- [INFO] 06:30 Node-03 心跳延迟偶发

## 近期失败任务（2 条）
- [FAILED] 09:01 VM-Migration-Job-4412: 目标节点存储不足
- [FAILED] 08:55 Backup-Job-1234: 网络中断
```

| 属性 | 值 |
|------|-----|
| **来源** | Environment 数据库表 |
| **存储位置** | PostgreSQL `environments` 表 |
| **注入接口** | `PUT /api/environments/case/{case_id}/type/{env_type}`（前端调用） |
| **获取接口** | `GET /api/environments/case/{case_id}/context`（conversation-service 应调用） |
| **是否动态** | 是（依赖采集数据） |

**数据采集流程**：
```
前端 CaseCreateDialog.vue
  ↓ SSH 采集命令执行
  ↓ acli platform info get        → env_type=cluster
  ↓ acli --formatter json alert list → env_type=alert
  ↓ acli --formatter json task list   → env_type=task
  ↓ environmentApi.upsert(caseId, envType, envData)
  ↓ PostgreSQL environments 表
  ↓ （conversation-service 应调用 context API）
  ↓ PromptBuilder._segment_s0_context_info()
```

**问题发现**：
- conversation-service 目前**没有调用** context API，导致此 Segment 为空
- AI 无法看到环境信息、告警日志、任务日志

---

### Segment 5：故障分类基准（198 个分类）

**内容示例**：
```
【故障分类基准】（共 198 个）

### 虚拟机域（45个）
- 虚拟机-001 虚拟机创建失败
- 虚拟机-002 虚拟机删除失败
- 虚拟机-003 虚拟机开机失败
- ...

### 存储域（38个）
- 存储-001 存储池创建失败
- ...

### 网络域（32个）
- 网络-001 vxlan 配置失败
- ...

### 硬件域（28个）
- 硬件-001 磁盘故障
- ...

### 平台域（55个）
- 平台-001 平台服务异常
- ...
```

| 属性 | 值 |
|------|-----|
| **来源** | KB Service 分类数据或缓存 |
| **存储位置** | KB 系统数据库（或 conversation-service 内存缓存） |
| **注入接口** | `kb_client.get_categories_grouped()` |
| **缓存策略** | 5 分钟 TTL（conversation_service.py 第184-196行） |
| **是否动态** | 是（从 KB 加载） |

---

### Segment 6：输出格式规范

**内容示例**：
```
【输出格式规范】

==== 情况一：高置信度（直接确认，≥80% 把握）====
在回复末尾单独输出一行：
已确认故障分类：{分类code} {分类名称}

==== 情况二：中置信度（候选确认）====
根据 [简要判断依据]，可能属于以下故障之一，请确认：

① {分类code-1} {分类名-1}
   判断依据：[引用告警/日志原文]，概率估计 ~XX%

② {分类code-2} {分类名-2}
   判断依据：[引用具体证据]，概率估计 ~XX%

③ 以上都不是（请补充症状描述）

==== 严格约束 ====
- 候选最多 2 个（+固定的③选项）
- 每个候选必须附判断依据
- 不得输出任何开放性问题
```

| 属性 | 值 |
|------|-----|
| **来源** | 代码常量（硬编码） |
| **存储位置** | `backend/conversation-service/app/services/prompt_builder.py` 第294-323行 `_segment_s0_output_format()` 方法 |
| **注入接口** | 无（固定内容） |
| **是否动态** | 否 |

---

### Segment 7：当前工单上下文

**内容示例**：
```
【当前工单上下文】
工单 ID：Q20260424001
客户描述：虚拟机开机失败，报错 "存储不可访问"
当前诊断阶段：S0
请直接开始意图确认
```

| 属性 | 值 |
|------|-----|
| **来源** | 工单创建时的参数 |
| **存储位置** | PostgreSQL `cases` 表 |
| **注入接口** | `POST /api/cases/` 创建工单 |
| **传递路径** | `case_id` + `description` 通过参数传入 `_build_s0_system_prompt()` |
| **是否动态** | 是（每个工单不同） |

---

## 接口调用流程

### 正常流程（应修复后）

```
┌─────────────────────────────────────────────────────────────────────┐
│                         前端 (CaseCreateDialog.vue)                  │
├─────────────────────────────────────────────────────────────────────┤
│ 1. 用户填写工单标题/描述                                              │
│ 2. SSH 连接成功                                                      │
│ 3. 执行采集命令：                                                    │
│    - acli platform info get   → cluster 数据                        │
│    - acli alert list          → alert 数据                          │
│    - acli task list           → task 数据                           │
│ 4. environmentApi.upsert(caseId, 'cluster', clusterData)            │
│ 5. environmentApi.upsert(caseId, 'alert', {alerts: alertList})      │
│ 6. environmentApi.upsert(caseId, 'task', {tasks: taskList})         │
│ 7. caseApi.create(title, description) → caseId                      │
│ 8. chatStore.connectSSH(...) → 建立 SSH 连接                         │
│ 9. chatStore.completeCaseCreationFlow(caseId, userMessage)          │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    Conversation Service                              │
├─────────────────────────────────────────────────────────────────────┤
│ 1. 接收 send_message_stream_only() 请求                              │
│ 2. 【修复点】调用 context API 获取环境数据：                          │
│    GET /api/environments/case/{case_id}/context                      │
│    → {env_info, alert_logs, task_logs}                               │
│ 3. 调用 kb_client.get_categories_grouped() 获取分类列表              │
│ 4. 调用 PromptBuilder.build_s0_prompt(                              │
│       context_info={env_info, alert_logs, task_logs},                │
│       categories_by_domain={虚拟机:[...], 存储:[...], ...},          │
│       case_context={case_id, description}                            │
│    )                                                                 │
│ 5. 返回完整的 System Prompt                                          │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│                         AI 客户端                                    │
├─────────────────────────────────────────────────────────────────────┤
│ OpenClaw / GLM 接收完整 Prompt（含 7 个 Segment）                    │
│ 输出：                                                                │
│ - 高置信度："已确认故障分类：虚拟机-003 虚拟机开机失败"              │
│ - 中置信度："① 虚拟机-003 开机失败 (~80%) ② 存储-001 存储不可访问 (~20%)" │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 数据库表结构

### environments 表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | UUID | 主键 |
| `case_id` | VARCHAR(36) | 工单 ID（外键） |
| `env_type` | VARCHAR(20) | 类型：cluster/alert/task |
| `env_data` | JSONB | 环境数据（JSON 格式） |
| `collected_at` | TIMESTAMP | 采集时间 |
| `trace_id` | VARCHAR(32) | 调用链 ID |
| `created_at` | TIMESTAMP | 创建时间 |

### env_type 数据格式

**cluster**：
```json
{
  "hci_version": "6.8.1",
  "cluster_name": "prod-cluster-01",
  "host_count": 3,
  "storage_type": "SSD-Pool",
  "network_config": "vxlan"
}
```

**alert**：
```json
{
  "alerts": [
    {"level": "CRITICAL", "trigger_time": "09:02", "content": "磁盘 I/O 延迟 > 300ms", "source": "Node-02"},
    {"level": "WARNING", "trigger_time": "08:45", "content": "存储池使用率 78%", "source": "SSD-Pool-01"}
  ]
}
```

**task**：
```json
{
  "tasks": [
    {"status": "failed", "start_time": "09:01", "name": "VM-Migration-Job-4412", "error_msg": "目标节点存储不足"},
    {"status": "failed", "start_time": "08:55", "name": "Backup-Job-1234", "error_msg": "网络中断"}
  ]
}
```

---

## 问题与修复

### 问题：Segment 4 为空

**原因**：`conversation_service.py` 第282行调用 `_build_system_prompt` 时没有传入 `context_info` 参数。

**影响**：AI 无法看到环境信息、告警日志、任务日志，推理质量下降。

**修复**：见下文代码修改。

---

## 相关文件索引

| 文件 | 作用 |
|------|------|
| `backend/conversation-service/app/services/prompt_builder.py` | Prompt 构建逻辑 |
| `backend/conversation-service/app/services/conversation_service.py` | Prompt 调用入口（需修复） |
| `backend/case-service/app/routes/environments.py` | Environment API 路由 |
| `backend/case-service/app/services/environment_service.py` | Environment 业务逻辑 |
| `backend/case-service/app/repositories/environment_repo.py` | Environment 数据访问 |
| `frontend/customer/src/components/CaseCreateDialog.vue` | 前端采集入口 |
| `frontend/customer/src/stores/chat.ts` | 前端状态管理 |
| `frontend/shared/src/api.ts` | 前端 API 客户端 |

---

## 更新历史

| 日期 | 变更 |
|------|------|
| 2026-04-24 | 初版创建，发现 Segment 4 问题 |
| 2026-04-24 | 修复 Segment 4 为空问题：① 新建 EnvironmentClient 调用 context API；② conversation_service.py 注入 context_info；③ config.py 添加 CASE_SERVICE_URL 配置 |