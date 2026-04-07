---
status: active
category: guide
audience: developer
last_updated: 2026-03-28
owner: team
---

# K3s 集群运维复盘报告

> **用途**：对近期 40 个 PR 改动中涉及的 K3s 集群运维问题进行系统性复盘，帮助团队识别高频陷阱、理解根因、避免重蹈覆辙。
>
> **数据来源**：
> - [guides/本地K3s部署指南.md](本地K3s部署指南.md)（DEPLOY-001~008、DUAL-001~012、NET-001~004）
> - [archive/10_重构优化.md](../archive/10_重构优化.md)（Phase 1~3 代码审查发现）
> - [archive/14_问题审计与修复清单.md](../archive/14_问题审计与修复清单.md)（P0~P2 缺陷清单）
> - [archive/09_项目进展.md](../archive/09_项目进展.md)（全量变更日志）

---

## 一、全量分类体系

| 类别 | 关键词 | 问题数 | 批次来源 |
|------|--------|--------|---------|
| **A 镜像版本不一致** | 旧镜像、import 遗漏、路径错配 | 4 | K3s 部署期 |
| **B 配置漂移/热重载失效** | Secret 漂移、DB schema、ConfigMap | 6 | K3s 部署期 |
| **C 资源生命周期失控** | StatefulSet 被删、NodePort 冲突 | 4 | K3s 部署期 |
| **D 网络/DNS 环境污染** | Clash fake-IP、ndots、搜索域 | 4 | K3s 部署期 |
| **E Helm/应用配置不规范** | YAML 类型歧义、裸 IP、校验失败 | 5 | K3s 部署期 |
| **F 代码层数据一致性** | 双写、双 commit、字段错配 | 5 | 早期开发期 |
| **G 服务接口契约不对齐** | 字段名不同、认证缺失、格式错误 | 6 | 早期开发期 |
| **H 运行时静默失效** | 异常被吞、后台任务无回调、资源泄漏 | 5 | 早期开发期 |
| **I 工程架构缺陷** | 状态不持久、全局 DI、路径 hack | 5 | 早期开发期 |
| **J 容器/运行时环境适配** | WebAPI 限制、探针 405、SSE 超时 | 6 | 首次 K3s 部署 |

---

## 二、两大核心失效模式

在 40 个问题中，归纳出两个最具代表性的跨类模式：

### 模式一：静默失效（Silent Failure）

影响问题：G-1、G-2、H-1、H-2、H-3 以及全部 B 类配置漂移

系统"看起来正常运行"，但核心功能已经失效——没有崩溃，没有告警，只有错误的数据或空的结果。这比直接崩溃更危险。

典型案例：
- KB 客户端字段名写错（`results` vs `chunks`），`except Exception` 吞掉 KeyError，RAG 功能完全失效但日志无报错
- ConfigMap 磁盘已更新，Prometheus 内存仍是旧配置，监控数据静默错误
- Scheduler 关闭工单后 Pod 未释放，池子慢慢耗尽，新工单无法分配但服务健康检查仍返回 200

**根本解法：在关键路径上加明确的成功断言，失败就快速爆出，不要静默降级。**

### 模式二：跨版本漂移（Version Drift）

影响问题：DB schema 漂移、K8s Secret vs postgres 密码、镜像 tag 不一致、接口字段名演化

系统由多个独立演化的组件组成，彼此的"版本约定"散落在代码/配置/文档里，没有统一的权威来源。某一层更新后，其他层不知道。

典型案例：
- `trace_id VARCHAR(50)` → OTel 32字符 hex，字段被截断，Loki→Tempo 钻取完全失效
- K8s Secret 更新了 `postgres-password`，但 PVC 里的 `pg_authid` 系统表密码未变，每次切换环境就爆 `authentication failed`
- `close_reason` 字段加到代码，迁移脚本没跑，Staging 上线直接 `UndefinedColumnError`

**根本解法：DB 迁移自动化（Init Container/Helm hook）+ 接口 schema 共享锁定。**

---

## 三、A 类：镜像版本不一致

### 根本原因

K3s containerd 和 Docker daemon 是两个完全独立的镜像存储。`docker build` 成功不代表 K3s 里有新镜像：

```
docker build → Docker daemon 镜像缓存
                         ↓ 必须手动执行
K3s containerd 镜像存储 ← docker save | k3s ctr images import
         ↓
Pod 调度 → imagePullPolicy=Never → 用 containerd 现有的 → 可能是旧的
```

| 问题 ID | 具体表现 | 根因 |
|---------|---------|------|
| DEPLOY-003 | openclaw `ErrImageNeverPull` | 依赖独立仓库构建，本地从未 import |
| DEPLOY-004 | productionclaw-pool-* 孤立 Pod | scheduler 动态创建，Helm 无法追踪和回收 |
| DUAL-001 | K3s 找不到 ghcr.io 路径镜像 | import 后前缀是 `docker.io/library/`，env repo 配置的是 `ghcr.io/...` |
| master 误部署 | 部署了错误分支的镜像 | 手动构建时未确认当前分支 |

### 解决方法

1. **构建-导入-验证三步原子化**，import 后立即验证：
   ```bash
   docker save "hci-${svc}:${TAG}" | sudo -n k3s ctr images import -
   sudo -n k3s ctr images ls | grep "${svc}:${TAG}" || { echo "import 失败"; exit 1; }
   ```

2. **使用唯一时间戳 tag**，替换 `latest`，强制每次升级都必须显式 import 新镜像：
   ```bash
   IMAGE_TAG="$(git log -1 --format='%cd-%h' --date='format:%Y.%m.%d-%H%M')"
   ```

3. **双仓模式镜像对齐**：import 时直接打 ghcr.io 路径 tag，与 env repo 配置保持一致：
   ```bash
   sudo -n k3s ctr images tag \
     "docker.io/library/hci-${svc}:${TAG}" \
     "ghcr.io/tomturing/hci-troubleshoot-platform/hci-${svc}:latest"
   ```

4. **部署前清理孤立 Pod**（scheduler 动态创建的）：
   ```bash
   kubectl delete pod -l app=productionclaw-pool -n hci-dev --ignore-not-found
   ```

---

## 四、B 类：配置漂移 / 热重载失效

### 根本原因

K8s 对象（ConfigMap、Secret）与应用程序的内存状态是异步的，更新 ConfigMap ≠ 应用立即感知：

```
# 层 1：ConfigMap 磁盘已更新，进程未重载
kubectl apply -f configmap.yaml  → 磁盘更新 ✅
   ↓ kubelet syncPeriod ~60s
挂载路径文件同步                 → 文件更新 ✅
   ↓ 但进程在启动时一次性读取
Prometheus/应用内存状态           → 仍是旧配置 ❌  ← 本次触发原因

# 层 2：多配置源漂移
K8s Secret (已更新密码) ≠ postgres PVC (pg_authid 旧密码)  ← DUAL-003
代码 (引用新字段)       ≠ 数据库实际 schema (缺少字段)      ← DUAL-006
Helm values (新 key 名) ≠ K8s Secret (旧 key 名)           ← DUAL-010
```

| 问题 ID | 具体表现 | 根因 |
|---------|---------|------|
| DUAL-003 | `password authentication failed` | PVC 持久化旧密码，Secret 已更新 |
| DUAL-006 | `UndefinedColumnError: close_reason` | 迁移脚本未按序执行 |
| DUAL-010 | `No API key found` | secretKeyRef 引用旧 key 名 |
| DUAL-011 | `${ZAI_API_KEY}` 占位符未替换 | 环境变量渲染缺失 |
| DUAL-002 | `assistantRegistryJson: {}` 变 `[]` | Helm YAML 类型歧义 |
| Prometheus | 内存配置陈旧 | ConfigMap 更新后未触发 `/-/reload` |

### 解决方法

1. **Prometheus 热重载**（已启用 `--web.enable-lifecycle`）：
   ```bash
   kubectl exec -n hci-observability \
     $(kubectl get pods -n hci-observability -l app=prometheus -o name) -- \
     curl -s -X POST http://localhost:9090/-/reload
   ```
   > 建议：在 ArgoCD PostSync Hook 或 Helm upgrade 脚本末尾自动触发

2. **DB 密码漂移预检**（切换 env 或重新部署前执行）：
   ```bash
   kubectl exec postgres-0 -n hci-dev -- \
     psql -U hci_admin -c "SELECT 1;" 2>&1 | grep -q "ERROR" && \
     echo "密码漂移，需执行 ALTER USER"
   ```

3. **Schema 迁移自动化**：目标是用 Helm `pre-upgrade Job` 在 Pod 启动前自动执行迁移，而非手动跑 SQL 文件：
   ```bash
   # 手动补齐（当前方案）
   for sql in migrate_evaluation_v1.sql migrate_kb_v3.sql migrate_p4_v1.sql migrate_tool_audit_log.sql; do
     cat database/$sql | \
       kubectl exec -i -n hci-dev postgres-0 -- psql -U hci_admin -d hci_db
   done
   ```

4. **Secret Key 只增不删原则**：添加 key 时保留旧 key，逐步迁移，避免 `secretKeyRef` 单点断裂。

5. **YAML 类型安全**：JSON-in-YAML 字段改用 values-file 传递，避免 `--set` 的类型歧义：
   ```yaml
   # /tmp/hci-override.yaml
   config:
     assistantRegistryJson: '{}'
   ```

---

## 五、C 类：资源生命周期失控

### 根本原因

Helm 只管理它在当次 Release 渲染中"知道"的资源。应用动态创建的 Pod、旧 Release 的遗留 Service、条件关闭的 StatefulSet，都不在 Helm reconcile 范围内：

```
# DUAL-009 场景
上次 Release：StatefulSet postgres 由 Helm 管理（dataLayer.manage=true）
这次 Release：dataLayer.manage=false → StatefulSet 不再渲染
Helm diff：postgres = 孤立资源 → 自动删除（PVC Retain 保住数据，但服务中断）

# NET-002 场景
旧 Release hci-platform/hci-troubleshoot 占用 NodePort 30789
新 Release hci-platform/hci-dev 申请同一 NodePort → 已被分配 → install 失败
（Helm 按 Release Name + Namespace 隔离，不做跨 namespace NodePort 冲突检测）
```

| 问题 ID | 具体表现 | 根因 |
|---------|---------|------|
| DUAL-009 | postgres/redis StatefulSet 被 Helm 删除 | `dataLayer.manage=false` 误操作 |
| NET-002 | NodePort 30789/30790 冲突 | 旧 Release 未清理 |
| DEPLOY-004 | productionclaw-pool-* 无法被清理 | 动态 Pod 无 OwnerReference |
| ArgoCD 资源竞争 | sync 冲突 | 多 Chart 管理同一资源（ADR-005 背景） |

### 解决方法

1. **⚠️ 高危警告**：每次 `helm upgrade` 必须保持 `dataLayer.manage=true`，否则 Helm 会删除 postgres/redis StatefulSet：
   ```bash
   # 每次升级前核查
   grep "manage:" .local/values-prod.override.yaml | grep -q "true" || \
     { echo "危险：dataLayer.manage 未设为 true"; exit 1; }
   ```

2. **部署前清理旧 Release**：
   ```bash
   helm list -A | grep "hci-troubleshoot"  # 确认旧 Release 是否遗留
   helm uninstall hci-platform -n hci-troubleshoot  # 确认后清理
   ```

3. **动态 Pod 清理纳入部署脚本**：在 `k3s-deploy-dualrepo.sh` pre-hook 中：
   ```bash
   kubectl delete pod -l managed-by=scheduler-service -n "$NAMESPACE" --ignore-not-found
   ```
   配合 scheduler 在创建 Pod 时统一打 label `managed-by=scheduler-service`。

---

## 六、D 类：网络/DNS 环境污染

### 根本原因

WSL2 + K3s + Clash TUN 三层网络叠加，DNS 解析路径被代理软件劫持：

```
Pod DNS 查询 github.com
  → ndots:5，先尝试搜索域 github.com.tail9f1936.ts.net
  → Tailscale DNS (100.100.100.100) 转发给 Clash
  → Clash 返回 fake-IP 198.18.0.x（本地占位 IP）
  → Pod TCP 443 连接到 fake-IP → Clash TUN 拦截 → TLS reset / EOF
```

| 问题 ID | 具体表现 | 根因 |
|---------|---------|------|
| NET-001 | ArgoCD 无法拉取 GitHub 仓库 | Clash fake-IP 劫持 TLS |
| DUAL-005 | 浏览器 `ERR_EMPTY_RESPONSE` | Clash fake-IP 劫持 nip.io |
| NET-004 | asyncpg `unexpected connection_lost()` | DNS search domain 硬编码旧命名空间 |
| NET-003 | git daemon 404 | git daemon 路径 `.git` 后缀问题 |

### 解决方法（长期）

1. **Clash 代理透传**（推荐，根治方案）：
   ```bash
   PROXY="http://172.26.96.1:7897"
   kubectl patch deployment argocd-repo-server -n argocd --type=json -p='[
     {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"HTTPS_PROXY","value":"'"$PROXY"'"}},
     {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"NO_PROXY","value":"10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,localhost,127.0.0.1"}}
   ]'
   ```

2. **CoreDNS NodeHosts 持久化**（目标固化到 Helm Chart，而非每次手动 patch）：
   ```bash
   kubectl edit configmap coredns -n kube-system
   # 在 NodeHosts 段追加：
   # 140.82.112.4 github.com
   # 185.199.108.133 raw.githubusercontent.com
   kubectl rollout restart deployment coredns -n kube-system
   ```

3. **命名空间模板化**：DNS search domain 使用 Helm template helper，禁止硬编码命名空间字符串：
   ```yaml
   searches:
     - {{ include "hci.namespace" . }}.svc.cluster.local
   ```

4. **WSL 重启后自检清单**（见 [本地K3s部署指南.md #第七节](本地K3s部署指南.md)）。

---

## 七、E 类：Helm/应用配置不规范

| 问题 ID | 具体表现 | 根因 | 修复 |
|---------|---------|------|------|
| DEPLOY-001 | `global.domain: ""` 触发安全校验 | Chart 安全校验硬编码 domain 比较 | override 设为 `"hci.local"` |
| DEPLOY-002 | Ingress host 拒绝裸 IP | K8s spec 要求 DNS 名 | 使用 nip.io 域名 |
| DEPLOY-006 | kb-service 启动崩溃（FastAPI 204 断言） | FastAPI 0.109 行为变更 | 改用 `return Response(status_code=204)` |
| DUAL-002 | `{}` 被解析为 `[]` | YAML 流式空 mapping 歧义 | 用 values-file 传 JSON 字符串 |
| DUAL-008 | ZhipuAI URL 双 `/v1/` 前缀 → 404 | 基础 URL 与 path 拼接设计错误 | 修正 URL 拼接逻辑 |

---

## 八、F 类：代码层数据一致性

### F-1：双重 message\_count 递增（D-1）

```
DB 触发器 tg_update_message_count：INSERT message → auto +1
代码层 add_message()：message_count += 1
两者同时触发 → 每发一条消息，计数 +2（功能正常，统计值偏差随时间累积）
```

根因：**"行为契约"未在代码里标注**——没有注释说明 `message_count` 由触发器维护。

修复：移除手动累加，用 `flush()` + `refresh()` 取触发器生成值，并加注释说明维护者是触发器。

### F-2：双重 commit 竞争（D-2）

```python
async with get_session() as session:    # 正常退出 → commit
    await repo.add_message(...)         # 内部 await session.commit()  ← 提前 commit
# context manager 再次 commit → session 已 INACTIVE → 静默异常或部分提交
```

修复：service 层只调 `flush()`，由 context manager 统一提交。

### F-3：Case→KB payload 字段名错误（P0-3）

```python
payload = {
    "content": summary,            # KB 期望 content_md
    "source_type": "case_summary"  # KB 只接受 kb|sop|realtime
}
# 结果：ingest 422，工单关闭不报错，知识永远不入库
```

### F-4：工单号随机生成（P1-2）

代码用 `random.randint(10000, 99999)` 替代了设计中的"当日递增"策略，存在并发重复 ID 风险。

### F-5：trace\_id VARCHAR(50) 截断

OTel trace_id 是 32 字符 hex，`VARCHAR(50)` 足够存储；但该列曾一度定义为 `VARCHAR(50) NOT NULL`，与 ORM model 的 nullable 不一致，导致 `trace_id` 为空时插入失败。

---

## 九、G 类：服务接口契约不对齐

### 根本原因

微服务之间的接口约定只存在于各自的代码里，没有共享的 OpenAPI schema 做强制约束，改了一边忘改另一边。

### G-1：KBClient 字段名不匹配（P0-2）

```python
# conversation-service（调用方）
return response.json()["results"]   # ← 写错了

# kb-service（提供方）
return {"chunks": [...], "total": n}  # ← 实际是 chunks

# 结果：每次 KB 检索 KeyError，但被 except Exception 吞掉
# → RAG 功能完全失效，系统正常运行，AI 回答质量悄悄变差
```

### G-2：KBClient 缺少内部鉴权头（P0-2）

```python
# 旧代码：无 Authorization 头
response = await self.client.get("/retrieve", params=...)
# kb-service 配置了 INTERNAL_API_TOKEN 验证
# 实际效果：所有 KB 请求返回 401，KB 功能完全失效，无告警
```

### G-3：SOP 响应结构不匹配（P0-2）

```python
# 调用方期望嵌套结构
if response["node"]["matched"]: ...

# 实际返回扁平结构
{"matched": True, "title": "...", "content": "..."}
```

### G-4：Scheduler `/pool-metrics` 字段错配（P1-1）

路由读取 `service.idle_count`，实际方法返回 `idle`。Grafana Dashboard 全部显示 0，没有任何报错，只有错误的数据。

### G-5：ConversationService 未定义成员/常量（P0-1）

```python
async def send_message(self, ...):
    hints = await self.kb_client.search(...)  # AttributeError: 'ConversationService' has no 'kb_client'
    prompt = _SYSTEM_BASE + hints             # NameError: _SYSTEM_BASE undefined
# 服务启动正常，第一次发消息时崩溃
```

### G-6：WebSocket endpoint URL 缺少路径参数

```python
WS_URL = "ws://localhost:8000/ws"  # 缺少 conversation_id
# 实际路由：@app.websocket("/ws/{conversation_id}")
# 连接直接 400，错误消息非常隐晦
```

---

## 十、H 类：运行时静默失效

### H-1：HTTPException 被通用 except 吞掉（R-2）

```python
try:
    result = await some_service()
except Exception as e:       # ← 吞掉了 HTTPException(404)
    raise HTTPException(500, str(e))
# 前端永远看到 500，无法区分"资源不存在"还是"服务崩溃"
```

### H-2：asyncio 后台任务无异常回调（B-2）

```python
asyncio.create_task(self.initialize_pods())
# 失败时只有 "Task exception was never retrieved"
# PodPool 初始化失败，Pool 大小为 0，但 /health 返回 200
```

修复：统一使用 `_safe_create_task()` + `task.add_done_callback(_task_done_callback)`。

### H-3：Pod 资源泄漏（R-1）

```
工单关闭 → 旧代码只更新工单状态，未调用 /api/scheduler/pods/release
→ 所有 Pod 永久停在 active 状态 → Pool 耗尽 → 新工单无可分配 Pod
→ 不重启 scheduler 无法恢复
```

### H-4：SSE 错误只打日志不回传（E-1）

```python
except Exception as e:
    logger.error(f"AI stream error: {e}")
    return  # SSE 流直接关闭，客户端看到"思考中..."然后什么都没有
```

### H-5：pod\_pool.initialize() 空 stub（B-1）

```python
async def initialize(self):
    pass  # TODO: scan existing pods
```

scheduler-service 重启后，原有运行中的 Pod 成为僵尸，scheduler 重复创建新 Pod。

---

## 十一、I 类：工程架构缺陷

### I-1：Scheduler 状态存内存（P-1）

```python
self.allocations: dict[str, str] = {}
# K8s Pod 重启 → allocations 清空 → case 还在，Pod 还在，scheduler 不知道谁对应谁
# → 重新分配 → 两个 Pod 服务同一 case
```

修复：迁移到 Redis Hash `scheduler:allocations`，自然持久化 + 原子操作。

### I-2：全局变量依赖注入（DI-1）

```python
database_manager = None  # 所有 4 个服务都是这个模式

def set_database_manager(db):
    global database_manager
    database_manager = db
# 测试间共享状态、多 worker 竞争条件、无法类型检查
```

修复：全部迁移至 `request.app.state`。

### I-3：sys.path.insert hack（DI-2）

```python
# 15+ 文件顶部
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# IDE 无法解析导入、测试/运行路径不一致、容器环境偶发 ImportError
```

修复：Docker 注入 `PYTHONPATH=/app`，测试层建立标准 `conftest.py`。

### I-4：CORS 安全配置错误（S-1）

```python
# RFC 6454 明确规定：allow_credentials=True 时，allow_origins 不允许是 "*"
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True)
# 带 cookie 的跨域请求全部被浏览器拒绝
```

修复：新增 `ALLOWED_ORIGINS` 环境变量，改用显式 Origin 列表。

### I-5：Helm 缺失 kb-service 模板（P1-3）

`values.yaml` 有 `kbService` 配置，`templates/` 无对应 Deployment/Service。`helm install` 静默成功，第一个请求路由到 kb-service 时才发现服务不存在。

---

## 十二、J 类：容器/运行时环境适配

| 问题 | 触发场景 | 根因 | 修复 |
|------|---------|------|------|
| `crypto.randomUUID()` 白屏 | K3s HTTP Ingress 访问 | Web Crypto API 要求 Secure Context (HTTPS/localhost)，HTTP 路径不满足 | 降级为 `Date.now() + Math.random()` |
| Admin UI JS MIME 错误 | K3s `/admin/` 子路径 | Vite `base: '/'` 生成 `/assets/...`，Nginx alias 路由到错误路径，返回 HTML 被当 JS 解析 | `base: '/admin/'` + nginx alias 适配 |
| OpenClaw `CrashLoopBackOff` | httpGet 探针 | K8s httpGet 对无 `/health` GET 端点的服务返回 405，K8s 认为 Pod 不健康 | 改用 `tcpSocket` 探针 |
| SSE `ReadTimeout` | AI 流式对话 | httpx 默认 5s 读取超时，AI 响应慢时 SSE 连接被主动断开 | `timeout=None` 禁用超时 |
| K3s Traefik 不存在 | 首次部署 | 安装时误用 `--disable traefik`，Ingress Controller 缺失 | 去掉参数重装 K3s |
| Grafana 跨 namespace Secret | obs 命名空间 | K8s Secret 不跨 namespace 共享 | 改为 inline value 写入 Helm values |

---

## 十三、系统健壮性评估

### 已有保障

| 机制 | 效果 |
|------|------|
| CI docs-governance 门禁 | 改代码必须同步文档，防止信息漂移 |
| Helm 三层 values 叠加 | chart 默认 / env repo / local override 分层隔离 |
| Chart 四层拆分（ADR-005） | infra/data/obs/platform 资源归属清晰，消除 ArgoCD 冲突 |
| 迁移脚本 `IF NOT EXISTS` | 幂等执行，重复运行安全 |
| 可观测性全栈 | Prometheus + Grafana + Loki + Tempo 全覆盖 |
| `imagePullPolicy=Never` | 本地环境不依赖外部 Registry，离线可用 |
| Redis 持久化 Scheduler 状态 | Pod 重启后分配关系自动恢复 |
| `_safe_create_task()` | 后台任务异常有回调，不再静默丢失 |

### 待优化的 7 个方向

| 优先级 | 方向 | 现状 | 目标 |
|--------|------|------|------|
| **P0** | ConfigMap 热重载自动化 | 手动 `curl -X POST /-/reload` | ArgoCD PostSync Hook 自动触发 |
| **P0** | DB 密码漂移检测 | 切换 env 后才发现 | pre-upgrade Job 预检 + 自动 `ALTER USER` |
| **P1** | DB 迁移自动化 | 手动按序执行 SQL 文件 | Helm pre-upgrade Job（Alembic/SQL） |
| **P1** | 镜像全链路校验 | build→import→deploy 手工确认 | import 后立即断言 tag 存在 |
| **P2** | 孤立 Pod 清理机制 | 手动 `kubectl delete pod` | 部署脚本 pre-hook 自动清理 |
| **P2** | DNS 配置持久化 | CoreDNS patch WSL 重启后丢失 | 固化进 hci-platform-infra Chart |
| **P3** | 跨服务接口 schema 验证 | 只有文档约定 | 共享 Pydantic models 或 OpenAPI codegen |

---

## 附：问题演变轨迹

```
早期开发 PR（F/G/H/I/J 类）    →    K3s 部署 PR（A/B/C/D/E 类）
─────────────────────────────        ──────────────────────────────
单服务内部问题                        跨服务/跨层问题
代码写错了                            代码对了但环境变了
功能失败（立即发现）                   配置漂移（静默失效）
根因：快速原型遗留技术债               根因：K3s 多层架构新复杂度
```

项目从 Docker 单机迁移到 K3s 多层架构时，新的基础设施层问题（A~E）叠加在原有代码质量问题（F~J）之上，形成了当前双轨并存的局面。其中静默失效模式（G-1、H-1~H-5、全部 B 类）是优先级最高的治理方向。
