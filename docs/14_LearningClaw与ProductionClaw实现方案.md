# HCI 智能排障平台 - LearningClaw 与 ProductionClaw 实现方案

## 文档信息
- **版本**: 1.0
- **作者**: Claude
- **创建日期**: 2026-03-05
- **状态**: 已实现（代码和配置已入库）
- **关联文档**: 13_AI层设计.md（架构决策背景和讨论过程）

---

## 1. 架构概述

本方案将 AI Assistant Layer 从"单 Pod 多 Session"升级为"两类专职 Pod"：

| 角色 | 类型 | 实例数 | 生命周期 | 职责 |
|---|---|---|---|---|
| **LearningClaw** | StatefulSet，常驻 | 1（单实例）| 平台启动即运行，永不停止 | 知识学习、案例提炼、知识库写入 |
| **ProductionClaw** | Per-Case Pod，动态 | 与活跃工单数相同 | Case 创建时分配，Case 关闭时销毁 | 一线排障对话，session-memory 完全隔离 |

```
┌──────────────────────────────────────────────────────────────────────┐
│                       AI Assistant Layer v3.0                        │
│                                                                       │
│  ┌──────────────────────────────────────────────┐                   │
│  │          LearningClaw (StatefulSet-0)         │  ← 常驻           │
│  │  • Web MCP 浏览 7000 案例                     │                   │
│  │  • 提炼 Production 工单经验                   │  → KB Service     │
│  │  • session-memory 开启（单实例，无污染）       │     (pgvector)    │
│  │  • PVC /home/node 持久化                      │                   │
│  └──────────────────────────────────────────────┘                   │
│                              ↓  写入知识                              │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐   │
│  │ ProductionClaw-1 │  │ ProductionClaw-2 │  │ ProductionClaw-N │   │
│  │ case-Q001 专用   │  │ case-Q002 专用   │  │ case-Q00N 专用   │   │
│  │ emptyDir 临时存储│  │ emptyDir 临时存储│  │ emptyDir 临时存储│   │
│  │ session-memory ✅│  │ session-memory ✅│  │ session-memory ✅│   │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘   │
│            ↑                    ↑                    ↑               │
│            └────────────────────┴────────────────────┘              │
│                           读取知识 (KB Search API)                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 文件结构

```
deploy/
├── claw-configs/                          # 【源文件，人工编辑】
│   ├── learningclaw/
│   │   ├── SOUL.md          AI 灵魂与价值观
│   │   ├── IDENTITY.md      身份与角色定义
│   │   ├── AGENTS.md        行为规范与工作手册
│   │   ├── BOOTSTRAP.md     启动初始化引导
│   │   ├── TOOLS.md         可用工具和 API 清单
│   │   └── USER.md          环境与用户信息
│   └── productionclaw/
│       ├── SOUL.md
│       ├── IDENTITY.md
│       ├── AGENTS.md
│       ├── BOOTSTRAP.md
│       ├── TOOLS.md
│       └── USER.md
│
└── helm/hci-platform/
    ├── claw-configs/                      # 【Helm 副本，由 make sync-claw-configs 同步】
    │   ├── learningclaw/                  # （内容与上面相同）
    │   └── productionclaw/
    ├── templates/
    │   ├── learningclaw.yaml              # StatefulSet + Service + CronJob
    │   ├── learningclaw-init-configmap.yaml  # 初始化文件 ConfigMap
    │   └── productionclaw-init-configmap.yaml  # 初始化文件 ConfigMap
    └── values.yaml                        # learningclaw / productionclaw 配置项

backend/scheduler-service/app/services/
├── k8s_client.py                          # ⭐ 已更新：ProductionClaw Pod 创建逻辑
│                                          #   - init 容器复制 ConfigMap 文件
│                                          #   - 注入 CASE_ID / CASE_TITLE / CASE_DESCRIPTION
│                                          #   - emptyDir for /home/node
├── pod_pool.py                            # ⭐ 已更新：acquire_pod 接受 case_info 参数
└── scheduler_service.py                   # ⭐ 已更新：allocate_pod 接受 case_info 参数

backend/scheduler-service/app/routes/
└── scheduler_routes.py                    # ⭐ 已更新：PodAllocationRequest 增加 case_info 字段
```

---

## 3. LearningClaw 详细设计

### 3.1 部署方式

- **类型**：StatefulSet（保证唯一性和 PVC 绑定）
- **副本数**：1（单实例，无需扩展）
- **存储**：PVC 5Gi，StorageClass `local-path-retain`（Pod 重启后 config/sessions/memory 完整保留）
- **启动命令**：`node dist/index.js gateway --allow-unconfigured --bind lan --port 18789`

### 3.2 init 容器逻辑

每次 Pod 启动时，init 容器从 `learningclaw-init-config` ConfigMap 复制文件到 `/home/node/.openclaw/`：

```
/init-config/SOUL.md      → /home/node/.openclaw/workspace/SOUL.md (仅首次)
/init-config/IDENTITY.md  → /home/node/.openclaw/workspace/IDENTITY.md (仅首次)
/init-config/AGENTS.md    → /home/node/.openclaw/workspace/AGENTS.md (仅首次)
/init-config/BOOTSTRAP.md → /home/node/.openclaw/workspace/BOOTSTRAP.md (仅首次)
/init-config/TOOLS.md     → /home/node/.openclaw/workspace/TOOLS.md (仅首次)
/init-config/USER.md      → /home/node/.openclaw/workspace/USER.md (仅首次)
/init-config/openclaw.json → /home/node/.openclaw/openclaw.json (每次覆盖，确保配置变更生效)
```

**注意**：SOUL/IDENTITY/AGENTS 等文件仅首次复制（`if [ ! -f ... ]`），防止覆盖运行时积累的修改。

### 3.3 openclaw.json 关键配置

| 项 | 值 | 说明 |
|---|---|---|
| `session-memory.enabled` | `true` | 开启（单实例，无污染风险）|
| `chatCompletions.systemPrompt` | LearningClaw 引导 | 启动即执行学习任务 |
| `gateway.controlUi.enabled` | `false` | 关闭 UI（后台服务无需界面）|

### 3.4 知识提炼流程

#### 从网页案例库（CronJob 每日 02:00）

```
K8s CronJob → POST /v1/chat/completions (learningclaw svc)
              "开始今日批量学习任务..."
                     ↓
          LearningClaw 执行 BOOTSTRAP.md 指引
                     ↓
          Web MCP 访问案例库，分批读取
                     ↓
          提炼：故障现象、根因、解决步骤、命令
                     ↓
          KB Service API: POST /api/kb/ingest
                     ↓
          更新 memory/learning_progress.md
```

#### 从 Production 工单（事件触发）

```
Case 关闭 → Conversation Service → POST learningclaw /v1/chat/completions
            "工单 Q001 已关闭，请提炼经验"
                     ↓
          LearningClaw 调用 Conversation Service API 获取对话历史
                     ↓
          分析：有效的诊断路径、命令、根因
                     ↓
          KB Service API 写入（去重检查后）
```

### 3.5 数据保护

| 数据 | 存储位置 | 保护措施 |
|---|---|---|
| openclaw sessions | PVC `/home/node/.openclaw/agents/` | PVC 使用 `local-path-retain` StorageClass，Pod 删除后 PVC 保留 |
| 学习记忆文件 | PVC `/home/node/.openclaw/workspace/memory/` | 同上 |
| 已摄入知识 | PostgreSQL pgvector (via KB Service) | PostgreSQL 每日备份（backup-cronjob.yaml）|
| 学习进度 | PVC `memory/learning_progress.md` | PVC 持久化 |

---

## 4. ProductionClaw 详细设计

### 4.1 部署方式

- **类型**：动态 Pod（由 Scheduler Service 按需创建/销毁）
- **生命周期**：与 Case 同步（分配时创建，关闭时 `delete_pod`）
- **存储**：emptyDir（临时，Pod 销毁后消失）——以 PostgreSQL 为持久化存储
- **实例隔离**：每个 Pod 独立的 `/home/node`，session-memory 天然隔离

### 4.2 init 容器逻辑

与 LearningClaw 类似，但**每次全量复制**（emptyDir，无需检查已存在）：

```
/init-config/*.md → /home/node/.openclaw/workspace/
/init-config/openclaw.json → /home/node/.openclaw/openclaw.json
```

打印启动日志：`✅ ProductionClaw 就绪，工单 {CASE_ID}`

### 4.3 注入的环境变量

| 变量 | 来源 | 用途 |
|---|---|---|
| `CASE_ID` | Scheduler → k8s_client → Pod labels + env | 工单身份，不可更改 |
| `CASE_TITLE` | POST /api/scheduler/pods/allocate 请求体 | 填入 BOOTSTRAP 初始化背景 |
| `CASE_DESCRIPTION` | 同上 | 触发 KB 预加载的 query |
| `CASE_CREATED_AT` | 同上 | session 记录时间参考 |
| `POD_NAME` | Downward API | 可观测性标识 |
| `INTERNAL_API_TOKEN` | Secret | 调用 KB/Case/Conversation Service |
| `KB_SERVICE_URL` | ConfigMap | 知识检索地址 |

### 4.4 openclaw.json 关键配置

| 项 | 值 | 说明 |
|---|---|---|
| `session-memory.enabled` | `true` | 开启（每 Pod 独立，天然隔离）|
| `chatCompletions.systemPrompt` | ProductionClaw 引导 | 读取 BOOTSTRAP.md，等待工程师消息 |
| `gateway.controlUi.enabled` | `false` | 关闭 UI |

### 4.5 知识获取流程

```
Pod 启动 → 读取 BOOTSTRAP.md
            ↓
         KB Search: POST {KB_SERVICE_URL}/api/kb/search
         query = "{CASE_TITLE} {CASE_DESCRIPTION}", top_k=5
            ↓
         KB SOP Match: POST {KB_SERVICE_URL}/api/kb/sop/match
            ↓
         内化为排障假设（不展示给用户）
            ↓
         等待工程师第一条消息
            ↓
         对话中按需补充检索（症状更新时）
```

### 4.6 结案处理

```
Case 关闭 → release_pod(case_id) → k8s.delete_pod(pod_name)
                                          ↓
                              并行：Conversation Service
                                   发送事件到 LearningClaw
                                   （提炼对话经验）
```

---

## 5. Scheduler Service 变更说明

### 5.1 API 变更

`POST /api/scheduler/pods/allocate` 请求体新增字段：

```json
{
  "case_id": "Q20260305001",
  "assistant_type": "productionclaw",
  "case_title": "虚拟机启动失败",          // 新增
  "case_description": "3台VM无法启动...",  // 新增
  "case_created_at": "2026-03-05T10:00:00Z" // 新增
}
```

### 5.2 调用链

```
POST /api/scheduler/pods/allocate
  → SchedulerService.allocate_pod(case_id, assistant_type, case_info)
    → PodPool.acquire_pod(case_id, case_info)
      → K8sClient.create_pod(case_id, case_info, assistant_config)
        → Pod Manifest with:
            env: CASE_ID, CASE_TITLE, CASE_DESCRIPTION, ...
            initContainers: copy ConfigMap files
            volumes: emptyDir + productionclaw-init-config
```

---

## 6. values.yaml 新增配置项

```yaml
learningclaw:
  enabled: true
  image.repository: hci-openclaw
  port: 18789
  storageClassName: local-path-retain
  storageSize: 5Gi
  defaultMode: batch
  resources: ...

productionclaw:
  enabled: true
  image.repository: hci-openclaw
  port: 18789
  resources: ...

config:
  kbServiceUrl: "http://kb-service:8004"                          # 新增
  learningclawServiceUrl: "http://learningclaw:18789"             # 新增
  assistantRegistryJson: '{"productionclaw":{...}}'               # 已更新

secrets:
  internalApiToken: "hci-dev-internal-token"                      # 新增
```

---

## 7. 部署操作

### 首次部署

```bash
# 1. 同步 claw-configs 到 Helm chart
make sync-claw-configs

# 2. 验证配置一致性
make check-claw-configs

# 3. Helm upgrade
cd deploy/helm
helm upgrade --install hci-platform hci-platform/ \
  --namespace hci-troubleshoot \
  --set secrets.internalApiToken="your-secure-token" \
  --set secrets.zaiApiKey="your-zai-key"

# 4. 验证 LearningClaw 启动
kubectl -n hci-troubleshoot get pods | grep learningclaw
kubectl -n hci-troubleshoot logs learningclaw-0 --tail=20
```

### 修改 AI 初始化文件后

```bash
# 编辑 deploy/claw-configs/learningclaw/AGENTS.md 等文件
# ...

# 同步到 Helm chart
make sync-claw-configs

# 重新部署（ConfigMap 会更新，init 容器下次启动时生效）
helm upgrade hci-platform hci-platform/
# LearningClaw: 需手动重启 Pod 让 init 容器重新运行 openclaw.json
kubectl -n hci-troubleshoot rollout restart statefulset/learningclaw
```

### 手动触发 LearningClaw 学习

```bash
# 通过 port-forward 或内部网络发送学习请求
kubectl -n hci-troubleshoot port-forward svc/learningclaw 18790:18789 &
curl -X POST http://localhost:18790/v1/chat/completions \
  -H "Authorization: Bearer hci-dev-openclaw-token" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "手动触发：请学习最新的 20 个案例并摄入知识库。"}]
  }'
```

---

## 8. 待实现（Conversation Service 侧）

以下功能需要 Conversation Service 配合实现：

| 功能 | 说明 | 优先级 |
|---|---|---|
| Case 关闭事件推送 | Case 关闭时调用 LearningClaw 触发经验提炼 | P1 |
| 分配时传 case_info | `POST /api/scheduler/pods/allocate` 携带 case_title/description | P1 |
| KB Search 上下文注入 | 在发送给 AI 之前，用 KB 检索结果增强 system message | P2 |

---

## 9. 废弃声明

| 废弃项 | 替代方案 |
|---|---|
| `openclaw` assistant_type（单 Pod 多 Session）| `productionclaw`（一 Pod 一 Case）|
| `deploy/helm/hci-platform/templates/openclaw-service.yaml` | 保留用于兼容现有工单，新工单使用 productionclaw |
| `session-memory` 禁用状态（临时措施）| 一 Pod 一 Case 后重新启用（已在 init ConfigMap 中配置为 enabled）|

---

*文档版本: 1.0 | 创建: 2026-03-05*
