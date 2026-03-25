<!--
  分片标识：新 11_完整技术方案.md（实施规范 HOW）— 第 4/5 部分
  内容来源：
    - 原 05 第 473–845 行（LearningClaw + ProductionClaw 实现规范）
    - 原 05 §十一（第 1680–1810 行：三类 Claw 角色全景）
  合并目标：新 11 文档 §十一（AI Pod层运维规范）
  说明：从 05 文档迁入，属于 HOW（实施规范），不是 WHY（架构决策）
-->

## 十一、AI Pod 层运维规范（LearningClaw + ProductionClaw + openclaw 守护进程）

> 本节记录 AI Assistant Layer 两类专职 Pod 的部署架构、初始化文件设计、操作规程。
> 关联提交: `60d7aec`（2026-03-05 架构升级）

### 11.1 平台中三类 openclaw 实例对比

平台共运行三种不同角色的 openclaw 实例，**使用同一个镜像**（`hci-openclaw`），通过 ConfigMap 注入差异化配置实现功能分化。

| 维度 | `openclaw`（固定守护进程）| `ProductionClaw`（排障专家）| `LearningClaw`（知识工程师）|
|------|--------------------------|---------------------------|-----------------------------|
| **K8s 类型** | Deployment（1 副本）+ 暖池 Pod×N | 由 Scheduler 动态管理的暖池 Pod | StatefulSet（1 副本，常驻）|
| **数量** | 1 个主守护 + 2 个暖池备用 | 3 个暖池 Pod（可扩展）| 永久 1 个 |
| **健康检查对象** | conversation-service 健康探针靶标 | `CASE_ID` 绑定后提供实际对话 | 常驻任务执行，不接受外部对话 |
| **`session-memory` hook** | ❌ 未开启 | ✅ **开启** | 无（持久化存 PVC）|
| **`compaction.mode`** | `safeguard` | 未设置（默认）| `safeguard`（防批量任务上下文过载）|
| **`maxConcurrent`** | 未限制 | 未限制（一 Pod 一 Case，天然单任务）| `1`（串行执行，避免并发写 KB）|
| **`controlUi`** | **开启**（dangerouslyAllowHostHeaderOriginFallback=true）| 关闭 | 关闭 |
| **生命周期** | 随平台启动，永不停止 | 随 Case 创建/销毁，池中复用 | 随平台启动，永不停止 |
| **K8s Service** | `openclaw:18789`（内部域名）| `productionclaw-pool-<hash>:18789` | `learningclaw:18789` |
| **存储** | 无状态（ConfigMap 注入）| 无状态（每次新 Case 重置）| **PVC**（持久化 workspace + memory）|
| **调用方** | conversation-service 的健康检查探针 | conversation-service 的实际业务请求 | scheduler-service 定时触发 |

#### openclaw.json 关键配置差异

```json
// openclaw（守护进程）
{
  "agents": {
    "defaults": {
      "compaction": { "mode": "safeguard" }
      // ❌ 无 session-memory hook
    }
  },
  "gateway": {
    "controlUi": { "enabled": true, "dangerouslyAllowHostHeaderOriginFallback": true }
  }
}

// ProductionClaw —— productionclaw-init-configmap.yaml
{
  "hooks": {
    "internal": {
      "entries": {
        "session-memory": { "enabled": true }  // ✅ 开启，记录本 Case 对话
      }
    }
  },
  "gateway": {
    "controlUi": { "enabled": false }
  }
}

// LearningClaw —— learningclaw-init-configmap.yaml
{
  "agents": {
    "defaults": {
      "workspace": "/home/node/.openclaw/workspace",  // ✅ 指向 PVC 路径
      "compaction": { "mode": "safeguard" },
      "maxConcurrent": 1
    }
  },
  "gateway": {
    "tailscale": { "mode": "off" },
    "controlUi": { "enabled": false }
  }
}
```

#### 关于 openclaw 守护进程的评估

**为什么它存在：**
- 健康检查靶标：`conversation-service` 的 `check_health()` 循环探针目标
- Fallback 兜底：当 Scheduler 还未为某个 Case 分配 ProductionClaw Pod 时，请求 fallback 到守护进程

**能干掉的前提条件：**
```
✅ health check 改为探针某个 ProductionClaw 暖池 Pod
✅ Scheduler 保证在对话开始前完成 Pod 绑定（消除 Fallback 场景）
✅ 暖池 Pod 数量充足（>= 高峰并发 Case 数 + 1 个备用）
```

**建议路线**：Phase 3（ReAct 引擎）完成后，Scheduler 的 Pod 管理足够稳定，届时评估移除守护进程，消除这个架构冗余。

---

### 11.2 整体架构

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

### 11.3 文件结构

```
deploy/
├── claw-configs/                          # 【源文件，人工编辑】
│   ├── learningclaw/
│   │   ├── SOUL.md, IDENTITY.md, AGENTS.md
│   │   ├── BOOTSTRAP.md, TOOLS.md, USER.md
│   └── productionclaw/
│       ├── SOUL.md, IDENTITY.md, AGENTS.md
│       ├── BOOTSTRAP.md, TOOLS.md, USER.md
│
└── helm/hci-platform/
    ├── claw-configs/                      # 【Helm 副本，由 make sync-claw-configs 同步】
    ├── templates/
    │   ├── learningclaw.yaml              # StatefulSet + Service + CronJob
    │   ├── learningclaw-init-configmap.yaml
    │   └── productionclaw-init-configmap.yaml
    └── values.yaml                        # learningclaw / productionclaw 配置项

backend/scheduler-service/app/services/
├── k8s_client.py     ✅  ProductionClaw Pod 创建逻辑（init 容器 + CASE_ID 注入）
├── pod_pool.py        ✅  acquire_pod 接受 case_info 参数
└── scheduler_service.py  ✅  allocate_pod 接受 case_info 参数
```

---

### 11.4 LearningClaw 详细设计

**部署配置**：
- 类型：StatefulSet
- 副本数：1（单实例）
- 存储：PVC 5Gi，StorageClass `local-path-retain`（Pod 重启后配置/sessions/memory 完整保留）
- 启动命令：`node dist/index.js gateway --allow-unconfigured --bind lan --port 18789`

**init 容器文件复制逻辑**（每次 Pod 启动时：）
```
/init-config/SOUL.md      → /home/node/.openclaw/workspace/SOUL.md (仅首次)
/init-config/IDENTITY.md  → /home/node/.openclaw/workspace/IDENTITY.md (仅首次)
/init-config/AGENTS.md    → /home/node/.openclaw/workspace/AGENTS.md (仅首次)
/init-config/BOOTSTRAP.md → /home/node/.openclaw/workspace/BOOTSTRAP.md (仅首次)
/init-config/openclaw.json → /home/node/.openclaw/openclaw.json (每次覆盖)
```
注：SOUL/IDENTITY/AGENTS 文件仅首次复制（`if [ ! -f ... ]`），防止覆盖运行时积累的修改。

**知识提炼流程（从 Production 工单，事件触发）**：
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

---

### 11.5 ProductionClaw 详细设计

**部署配置**：
- 类型：动态 Pod（由 Scheduler Service 按需创建/销毁）
- 生命周期：与 Case 同步（分配时创建，关闭时 `delete_pod`）
- 存储：emptyDir（临时，Pod 销毁后消失）—— 以 PostgreSQL 为持久化存储

**注入的环境变量**：

| 变量 | 来源 | 用途 |
|------|------|------|
| `CASE_ID` | Scheduler → k8s_client → Pod labels + env | 工单身份，不可更改 |
| `CASE_TITLE` | POST /api/scheduler/pods/allocate 请求体 | 填入 BOOTSTRAP 初始化背景 |
| `CASE_DESCRIPTION` | 同上 | 触发 KB 预加载的 query |
| `CASE_CREATED_AT` | 同上 | session 记录时间参考 |
| `POD_NAME` | Downward API | 可观测性标识 |
| `INTERNAL_API_TOKEN` | Secret | 调用 KB/Case/Conversation Service |
| `KB_SERVICE_URL` | ConfigMap | 知识检索地址 |

---

### 11.6 Scheduler Service 变更说明

`POST /api/scheduler/pods/allocate` 请求体新增字段：

```json
{
  "case_id": "Q20260305001",
  "assistant_type": "productionclaw",
  "case_title": "虚拟机启动失败",
  "case_description": "3台VM无法启动...",
  "case_created_at": "2026-03-05T10:00:00Z"
}
```

调用链：
```
POST /api/scheduler/pods/allocate
  → SchedulerService.allocate_pod(case_id, assistant_type, case_info)
    → PodPool.acquire_pod(case_id, case_info)
      → K8sClient.create_pod(case_id, case_info, assistant_config)
        → Pod Manifest with env: CASE_ID, CASE_TITLE, CASE_DESCRIPTION...
```

---

### 11.7 values.yaml 新增配置项

```yaml
learningclaw:
  enabled: true
  image.repository: hci-openclaw
  port: 18789
  storageClassName: local-path-retain
  storageSize: 5Gi

productionclaw:
  enabled: true
  image.repository: hci-openclaw
  port: 18789

config:
  kbServiceUrl: "http://kb-service:8004"
  learningclawServiceUrl: "http://learningclaw:18789"

secrets:
  internalApiToken: "hci-dev-internal-token"
```

---

### 11.8 部署操作

**首次部署**：
```bash
# 1. 同步 claw-configs 到 Helm chart
make sync-claw-configs

# 2. 验证配置一致性
make check-claw-configs

# 3. Helm upgrade
helm upgrade --install hci-platform hci-platform/ \
  --namespace hci-troubleshoot \
  --set secrets.internalApiToken="your-secure-token" \
  --set secrets.zaiApiKey="your-zai-key"

# 4. 验证 LearningClaw 启动
kubectl -n hci-troubleshoot get pods | grep learningclaw
kubectl -n hci-troubleshoot logs learningclaw-0 --tail=20
```

**修改 AI 初始化文件后**：
```bash
# 编辑 deploy/claw-configs/learningclaw/AGENTS.md
make sync-claw-configs
helm upgrade hci-platform hci-platform/
# LearningClaw 需手动重启让 init 容器重新运行（openclaw.json 覆盖更新）
kubectl -n hci-troubleshoot rollout restart statefulset/learningclaw
```

**废弃声明**：

| 废弃项 | 替代方案 |
|-------|---------|
| `openclaw` assistant_type（单 Pod 多 Session）| `productionclaw`（一 Pod 一 Case）|
| `session-memory` 禁用状态（临时措施）| 一 Pod 一 Case 后重新启用 |
