---
status: active
category: guide
audience: developer
last_updated: 2026-03-27
owner: team
supersedes: 21_本地K3s部署日志.md, WSL-K3s-ArgoCD-网络避坑.md
---

# 本地 K3s 部署指南

> **适用场景**：在 WSL2 + K3s 本地开发环境中构建镜像并部署 HCI 排障平台全栈服务。
>
> **相关文档**：
> - [reference/K8s部署规格.md](../reference/K8s部署规格.md) — Helm Chart 参数速查
> - [guides/生产运维指南.md](生产运维指南.md) — 生产环境操作手册
> - [adr/002-GitOps双仓模型.md](../adr/002-GitOps双仓模型.md) — 双仓架构决策背景

---

## 一、环境信息

| 字段             | 内容                                    |
|----------------|-----------------------------------------|
| 操作系统         | WSL2 Ubuntu 24.04                       |
| k3s 版本        | v1.34.5+k3s1                            |
| Helm 版本       | v3.20.1                                 |
| Docker 版本     | 28.2.2                                  |
| 部署节点         | sz（单节点，control-plane+worker）       |
| 本机 WSL IP     | 172.26.101.255（`ip a` 查看 eth0 地址）  |

---

## 二、快速部署

### 2.1 镜像构建与导入

```bash
# 1. 构建所有业务镜像（脚本自动跳过未克隆的 openclaw 仓库）
cd /mnt/d/aihci/hci-troubleshoot-platform
bash scripts/ops/k3s-build.sh

# 2. 导入到 k3s containerd（k3s 不使用 Docker daemon）
IMAGE_TAG="$(git log -1 --format='%cd-%h' --date='format:%Y.%m.%d-%H%M')"
for svc in api-gateway case-service conversation-service scheduler-service kb-service customer-ui admin-ui; do
  docker save "hci-${svc}:${IMAGE_TAG}" | sudo -n k3s ctr images import -
done
```

> **注意**：k3s containerd 导入后镜像前缀为 `docker.io/library/`。由于 `imagePullPolicy=Never`，k8s 调度时自动补全前缀，正常工作。

### 2.2 Helm 部署（单仓模式）

创建或确认 `.local/values-prod.override.yaml` 包含以下最小配置：

```yaml
global:
  domain: "hci.local"       # 避免触发 Helm chart 弱密码安全校验
  publicUrl: "http://172.26.101.255.nip.io"  # 必须是 DNS 名，不能是裸 IP

dataLayer:
  manage: true              # 让 Helm 管理 postgres + redis StatefulSet

openclaw:
  enabled: false            # 本地无 openclaw 镜像时关闭
learningclaw:
  enabled: false
productionclaw:
  enabled: false

config:
  assistantRegistryJson: '{}'  # 防止 scheduler 创建孤立 openclaw pool pods
```

执行部署：

```bash
helm upgrade --install hci-platform deploy/helm/hci-platform \
  -f deploy/helm/hci-platform/values.yaml \
  -f deploy/helm/hci-platform/values-dev.yaml \
  -f .local/values-prod.override.yaml \
  --namespace hci-dev \
  --create-namespace \
  --kubeconfig /etc/rancher/k3s/k3s.yaml \
  --timeout 15m
```

### 2.3 双仓模式（GitOps）部署

使用独立的 `hci-platform-env` 环境仓库：

```bash
# env repo 须在 ../hci-platform-env 路径
bash scripts/ops/k3s-deploy-dualrepo.sh --env dev
```

**双仓 Helm values 叠加顺序**：

```
1. deploy/helm/hci-platform/values.yaml          ← chart 默认值
2. hci-platform-env/environments/dev/values.yaml  ← env repo 差异值
3. .local/values-dualrepo-local.yaml              ← 本地覆盖（不入 git）
```

### 2.4 验证健康状态

```bash
# 查看所有 Pod
sudo -n k3s kubectl get pods -n hci-dev

# 检查核心服务健康
curl -s http://172.26.101.255.nip.io/api/health | python3 -m json.tool
curl -s http://172.26.101.255.nip.io/api/cases/health | python3 -m json.tool
```

---

## 三、访问地址

部署完成后（`routingMode=path`，需 DNS 可达 nip.io）：

| 服务         | URL                                         |
|------------|---------------------------------------------|
| 客户端 UI   | http://172.26.101.255.nip.io/               |
| 管理控制台   | http://172.26.101.255.nip.io/admin/         |
| API Gateway | http://172.26.101.255.nip.io/api            |
| Grafana     | http://172.26.101.255.nip.io/grafana/       |

> **Grafana 路由说明**：`hci-platform-obs` chart 默认启用 Ingress（`ingress.enabled: true`），
> 创建独立的 `grafana-ingress`，使用 Traefik priority 确保高于主站 `/` 回退路由，
> 避免被 customer-ui 吞掉。如 Grafana 页面显示 customer-ui 内容，检查 `grafana-ingress` 是否存在：
> ```bash
> kubectl get ingress -n hci-observability
> ```

> **nip.io 无法访问时**：在本机 Windows hosts 文件添加 `172.26.101.255  172.26.101.255.nip.io`，或使用 NodePort 直连：
> ```bash
> # kubectl port-forward 方式
> sudo -n k3s kubectl port-forward svc/customer-ui 8080:80 -n hci-dev &
> sudo -n k3s kubectl port-forward svc/api-gateway 8000:8000 -n hci-dev &
> ```

### 3.2 数据库连接

通过 NodePort 直连 PostgreSQL（端口 30054）：

| 字段 | 值 |
|------|---|
| 主机 | `<节点IP>` 或 `localhost`（WSL 内） |
| 端口 | `30054` |
| 数据库 | `hci_troubleshoot` |
| 用户名 | `hci_admin` |
| 密码 | 见 `hci-platform-env/environments/dev/values.yaml` → `secrets.postgresPassword` |

```bash
# psql 命令行连接
psql -h localhost -p 30054 -U hci_admin -d hci_troubleshoot

# JDBC URL（DBeaver / DataGrip）
jdbc:postgresql://localhost:30054/hci_troubleshoot
```

> **注意**：NodePort 需在环境配置中启用。配置字段位置：
> - `hci-platform-data/values.yaml` → `postgres.nodePort: 30054`（data chart 默认值）
> - `hci-platform-env/environments/dev/values.yaml` → 覆盖实际端口值
>
> **Service 架构说明**：`postgres`（Headless，`clusterIP: None`）用于 StatefulSet governing service 和集群内 DNS 解析（`postgres-0.postgres.hci-dev.svc`）；
> `postgres-external`（NodePort）仅用于外部客户端访问，两者独立，避免 ArgoCD sync 时因 `clusterIP` 不可变导致 SyncFailed。

---

## 四、已知部署问题索引

> 按问题 ID 索引，`DEPLOY-*` 为单仓首次部署，`DUAL-*` 为双仓 GitOps 部署。

### 4.1 快速汇总

| ID | 级别 | 阶段 | 问题描述 | 状态 |
|----|------|------|---------|------|
| DEPLOY-001 | ⚠️ INFO | helm lint | `global.domain: ""` 触发 chart 安全校验告警 | ✅ 已修复 |
| DEPLOY-002 | ❌ 阻断 | helm install | Ingress host 设为裸 IP，k8s 拒绝（必须是 DNS 名） | ✅ 已修复 |
| DEPLOY-003 | ❌ 阻断 | Pod 调度 | openclaw/learningclaw `ErrImageNeverPull` | ✅ 已修复 |
| DEPLOY-004 | ⚠️ 残留 | Pod 调度 | productionclaw-pool-* 孤立 Pod `ImagePullBackOff` | ✅ 已修复 |
| DEPLOY-005 | ℹ️ 预期 | 运行时 | conversation-service 健康返回 `degraded` | ℹ️ 预期行为 |
| DEPLOY-006 | ❌ 阻断 | Pod 启动 | kb-service `AssertionError: Status code 204 must not have a response body` | ✅ 已修复 |
| DEPLOY-007 | ℹ️ 非阻断 | 镜像构建 | Docker buildx 权限错误，自动降级 legacy builder | ℹ️ 非阻断 |
| DEPLOY-008 | ❌ 阻断 | 运行时 | case-service `database: unavailable`（postgres 未被 Helm 部署） | ✅ 已修复 |
| DUAL-001 | ❌ 阻断 | Pod 调度 | K3s 无法拉取 ghcr.io 镜像（imagePullPolicy 不为 Never） | ✅ 已修复 |
| DUAL-002 | ❌ 阻断 | Helm upgrade | `assistantRegistryJson: {}` 被 Helm 解析为空列表 `[]` | ✅ 已修复 |
| DUAL-003 | ❌ 阻断 | 运行时 | postgres 密码不匹配（PVC 持久化旧密码） | ✅ 已修复 |
| DUAL-004 | ⚠️ 告警 | 运行时 | OpenTelemetry exporter 无法连接 Tempo（observability 未部署） | ℹ️ 预期 |
| DUAL-005 | ⚠️ 告警 | 浏览器访问 | Clash TUN fake-IP 劫持 nip.io DNS → `ERR_EMPTY_RESPONSE` | ✅ 已修复 |
| DUAL-006 | ❌ 阻断 | 运行时 | 数据库 schema 落后于代码版本（缺少 `close_reason` 等字段） | ✅ 已修复 |
| DUAL-007 | ❌ 阻断 | 运行时 | AI 对话报错：`Server disconnected`（openclaw Service 不存在） | ✅ 已修复 |
| DUAL-008 | ❌ 阻断 | 运行时 | ZhipuAI 直连 URL 多出 `/v1/` 前缀 → 404 | ✅ 已修复 |
| DUAL-009 | ❌ 严重 | helm upgrade | 误操作 `dataLayer.manage=false` 导致 postgres/redis StatefulSet 被 Helm 删除 | ✅ 已修复 |
| DUAL-010 | ❌ 阻断 | 运行时 | learningclaw `No API key found`（secretKeyRef 引用旧 key 名） | ✅ 已修复 |
| DUAL-011 | ❌ 阻断 | 运行时 | openclaw CrashLoopBackOff（`${ZAI_API_KEY}` 占位符未替换） | ✅ 已修复 |
| DUAL-012 | ⚠️ 告警 | 运行时 | openclaw Control UI `pairing required`（设备配对机制） | ✅ 已修复 |
| NET-001 | ❌ 阻断 | ArgoCD repo-server | GitHub 拉取失败（EOF / TLS Reset）——Clash fake-IP 劫持 | ✅ 已解决 |
| NET-002 | ❌ 阻断 | Helm install | NodePort 30789/30790 冲突（旧 Release 未清理） | ✅ 已解决 |
| NET-003 | ❌ 阻断 | ArgoCD git:// | git daemon 路径 `.git` 后缀导致 404 | ✅ 已解决 |
| NET-004 | ❌ 阻断 | 运行时（AI 对话） | conversation-service DNS 搜索域硬编码旧命名空间 | ✅ 已修复 |

---

## 五、问题详情

### DEPLOY-001：global.domain 空字符串触发 Helm chart 安全校验

**根因**：`secret.yaml` 模板在 `ne domain "hci.local" && postgresPassword == "dev_password_123"` 时调用 `fail`。  
**修复**：`.local/values-prod.override.yaml` 中将 `global.domain` 设为 `"hci.local"`。

---

### DEPLOY-002：publicUrl 使用裸 IP，Ingress host 验证失败

**错误日志**（helm install）：
```
Ingress.networking.k8s.io "hci-ingress" is invalid:
  spec.rules[0].host: must be a DNS name, not an IP address
```

**根因**：`ingress.yaml` 模板从 `publicUrl` 提取 hostname 写入 `spec.rules[].host`，裸 IP 不是合法 DNS 名。  
**修复**：将 `publicUrl` 改为 `"http://172.26.101.255.nip.io"`（nip.io 自动 DNS 映射回裸 IP）。

---

### DEPLOY-003：hci-openclaw 镜像未构建导致 Pod 失败

**错误现象**：`openclaw-* ErrImageNeverPull`、`learningclaw-0 Init:ErrImageNeverPull`

**根因**：`hci-openclaw` 镜像需克隆独立的 `openclaw` 仓库并构建，本地无此镜像。  
**修复**：
```yaml
# .local/values-prod.override.yaml
openclaw:
  enabled: false
learningclaw:
  enabled: false
productionclaw:
  enabled: false
config:
  assistantRegistryJson: '{}'   # 防止 scheduler 继续创建热备 Pod
```

---

### DEPLOY-004：productionclaw-pool-* 孤立 Pod 无法被 Helm 清理

**根因**：scheduler-service 根据 `assistantRegistryJson` 动态创建 Pod，这些 Pod 不被 Helm 管理。  
**修复**：
```bash
sudo -n k3s kubectl delete pod -l app=productionclaw-pool -n hci-dev
```

---

### DEPLOY-006：kb-service 启动崩溃（FastAPI 204 状态码断言）

**错误日志**：
```
AssertionError: Status code 204 must not have a response body
```

**根因**：FastAPI 0.109.0 在 `@router.delete(..., status_code=204)` 时断言不允许有响应体，但 `response_class=Response` 无法绕过路由注册阶段的检查。  
**修复**（`backend/kb-service/app/routes/review.py`）：
```python
# 修复前（错误）
@router.delete("/{atom_id}", status_code=204, response_class=Response)
async def delete_atom(...) -> None: ...

# 修复后（正确）
@router.delete("/{atom_id}")
async def delete_atom(...) -> Response:
    ...
    return Response(status_code=204)
```

---

### DEPLOY-008：case-service database unavailable（postgres 未部署）

**根因**：`values.yaml` 默认 `dataLayer.manage: false`，postgres/redis StatefulSet 不被 Helm 渲染。  
**修复**：
```yaml
# .local/values-prod.override.yaml
dataLayer:
  manage: true
```

> ⚠️ **高危警告**：后续 `helm upgrade` 时**必须始终**保持 `dataLayer.manage=true`，否则 Helm Reconcile 会删除已运行的 postgres/redis StatefulSet（见 DUAL-009）。

---

### DUAL-001：K3s containerd 无 ghcr.io 镜像（双仓模式）

**根因**：env repo `values.yaml` 将 `imageRegistry` 设为 `ghcr.io/...`，K3s 本地没有该路径的镜像。  
**修复**（将本地镜像重标签为 ghcr.io 路径）：
```bash
TAG="2026.03.24-1646-0693d81"
for svc in api-gateway case-service conversation-service scheduler-service kb-service customer-ui admin-ui; do
  sudo -n k3s ctr images tag \
    "docker.io/library/hci-${svc}:${TAG}" \
    "ghcr.io/tomturing/hci-troubleshoot-platform/hci-${svc}:latest"
done
```

---

### DUAL-002：Helm 将 `{}` 解析为空列表

**根因**：`--set "config.assistantRegistryJson={}"` 和 `--set-string` 均将 `{}` 解析为 YAML 空序列 `[]`。  
**修复**：用临时 values 文件绕过（已内置到 `k3s-deploy-dualrepo.sh`）：
```yaml
# /tmp/hci-override.yaml
config:
  assistantRegistryJson: '{}'
```

---

### DUAL-003：postgres 密码不匹配（PVC 持久化旧密码）

**现象**：`password authentication failed for user "hci_admin"`

**根因**：postgres 密码写入 PVC 的 `pg_authid` 系统表，切换 env 后 K8s Secret 更新，但 DB 内部密码未变。  
**修复**：
```bash
# 将数据库密码同步为 K8s Secret 中的值
sudo -n k3s kubectl exec -n hci-dev postgres-0 -- \
  psql -U hci_admin -d hci_db -c "ALTER USER hci_admin PASSWORD '<new_password>';"

# 重启所有 Deployment 重置连接池
sudo -n k3s kubectl rollout restart deployment -n hci-dev
```

> **教训**：重新部署或切换 env 时，若 postgres PVC 已存在且密码变更，**必须手动 `ALTER USER` 同步密码**，或先删除 PVC 重建（会丢失数据）。

---

### DUAL-006：数据库 schema 落后于代码版本

**现象**：`UndefinedColumnError: column "close_reason" of relation "case" does not exist`

**根因**：本地 Postgres 以 `init_schema.sql` 初始化（旧 schema），新版代码依赖后续迁移脚本添加的字段。  
**修复**（按顺序执行所有迁移脚本）：
```bash
for sql in migrate_evaluation_v1.sql migrate_kb_v3.sql migrate_p4_v1.sql migrate_tool_audit_log.sql; do
  cat database/$sql | \
    sudo -n k3s kubectl exec -i -n hci-dev postgres-0 -- \
    psql -U hci_admin -d hci_db
done
```

> **注意**：迁移脚本均使用 `ADD COLUMN IF NOT EXISTS`，重复执行安全。`init_schema.sql` 仅供全新初始化，更新已有数据库须按版本顺序补跑迁移。

---

### DUAL-009：误操作 dataLayer.manage=false 导致 postgres/redis 被 Helm 删除

**根因**：Helm Reconcile 将「之前由该 Release 管理后因条件关闭不再渲染」的 StatefulSet 视为孤立资源并删除。PVC 因 `Retain` 策略保留，数据未丢失。  
**恢复**：
```bash
helm upgrade --install hci-platform ... --set dataLayer.manage=true
# Helm 重新渲染 StatefulSet，postgres-0/redis-0 重建并挂载原 PVC
```

---

### NET-001：ArgoCD repo-server 无法访问 GitHub（EOF / TLS Reset）

**现象**：
```
failed to list refs: Get "https://github.com/.../info/refs": EOF
wget: got bad TLS record (len:0) while expecting handshake record
```

**根因（两层叠加）**：
```
Pod DNS 查询 github.com
  → ndots:5，先尝试搜索 Tailscale 域 → Clash 返回 fake-IP 198.18.0.x
  → NodeHosts 真实 IP 被绕过
  → Pod TCP 443 连接到 fake-IP → Clash TUN 拦截 → TLS 重置
```

**解法 A（推荐，长期方案）：Clash 开启局域网连接 + HTTPS_PROXY**

步骤一：Clash Verge → 设置 → **开启"局域网连接"**。

步骤二：给 repo-server 注入代理（将 `172.26.96.1:7897` 替换为实际 Windows 主机 IP 和 Clash 端口）：
```bash
PROXY="http://172.26.96.1:7897"
kubectl patch deployment argocd-repo-server -n argocd --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"HTTPS_PROXY","value":"'"$PROXY"'"}},
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"HTTP_PROXY","value":"'"$PROXY"'"}},
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"NO_PROXY","value":"10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,localhost,127.0.0.1"}}
]'
```

**解法 B（辅助）：CoreDNS NodeHosts + ndots:1**

> 解法 A 已解决根因。解法 B 仅在无代理时作为 DNS 污染的临时缓解手段。

```bash
# 追加 GitHub 真实 IP 到 CoreDNS NodeHosts
kubectl edit configmap coredns -n kube-system
# 在 NodeHosts 段追加：
# 140.82.112.4 github.com
# 140.82.114.4 github.com
# 185.199.108.133 raw.githubusercontent.com
kubectl rollout restart deployment coredns -n kube-system

# 修复 ndots:5 搜索顺序
kubectl patch deployment argocd-repo-server -n argocd --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/dnsConfig","value":{"options":[{"name":"ndots","value":"1"}]}}]'
```

---

### NET-002：NodePort 端口冲突

**现象**：
```
Service "openclaw" NodePort 30789 already allocated
```

**根因**：旧的 Helm release（`hci-platform` in `hci-troubleshoot`）未清理，占用了相同 NodePort。  
**修复**：
```bash
# 确认冲突来源
kubectl get svc -A | grep "30789\|30790"

# 卸载旧 release
helm uninstall hci-platform -n hci-troubleshoot
```

---

### NET-003：git daemon 路径 .git 后缀导致 404

**现象**：
```
'/mnt/d/aihci/hci-troubleshoot-platform.git' does not appear to be a git repository
```

**根因**：ArgoCD 自动在 repoURL 末尾追加 `.git`，但本地目录无此后缀。  
**修复**：
```yaml
# ArgoCD Application
repoURL: git://172.26.101.255/hci-troubleshoot-platform   # 不带 .git
```

> ⚠️ git daemon 仅作临时手段，推荐改用解法 A（Clash 代理 + HTTPS GitHub URL）。

---

### NET-004：conversation-service DNS 搜索域硬编码旧命名空间

**现象**：`asyncpg connect` 报 `unexpected connection_lost()`（SSL 握手失败）

**根因**：Helm 模板 `externalDns: true` 时注入的 `dnsConfig.searches` 硬编码了旧命名空间 `hci-troubleshoot`，导致 `postgres` 解析走公网 DNS → Clash fake-IP → asyncpg 握手失败。

**修复**（`deploy/helm/hci-platform/templates/conversation-service/deployment.yaml`）：
```yaml
# 使用 hci.namespace helper 动态注入当前命名空间
searches:
  - {{ include "hci.namespace" . }}.svc.cluster.local
  - svc.cluster.local
  - cluster.local
```

---

## 六、各命名空间职责

| 命名空间              | 状态   | 用途                                                  |
|---------------------|--------|-------------------------------------------------------|
| `argocd`            | 正常   | ArgoCD 控制面（repo-server、application-controller 等）|
| `hci-dev`           | 正常   | hci-platform 主业务服务（API、前端、各微服务）            |
| `hci-observability` | 正常   | 可观测性栈（Prometheus、Grafana、Loki、Tempo）            |
| `hci-troubleshoot`  | 残留   | 旧 Helm 直接部署遗留，可手动删除                          |
| `kube-system`       | 正常   | K3s 系统组件、CoreDNS 等                                |

**`hci-troubleshoot` 安全删除**：
```bash
kubectl get all -n hci-troubleshoot   # 确认为空
kubectl delete namespace hci-troubleshoot
```

---

## 七、WSL 重启后恢复清单

每次 WSL 重启后，以下**运行时 patch** 需重新确认（均未持久化到文件）：

- [ ] **CoreDNS NodeHosts**：验证是否保留（`kubectl get cm coredns -n kube-system -o yaml | grep github`）
- [ ] **repo-server HTTPS_PROXY**：Deployment patch 持久，Pod 重建后自动继承 ✅
- [ ] **repo-server ndots:1**：同上，Deployment patch 持久 ✅
- [ ] **git daemon**（如在使用）：进程级，WSL 重启后需重新启动

> **建议**：将 `HTTPS_PROXY` 和 `ndots:1` 用 Helm values override 或 ArgoCD 自身配置固化，消除对运行时 patch 的依赖。

---

## 八、数据库 Schema 检查清单

本地重用已有 PVC 重新部署或切换 env 前必须确认：

- [ ] postgres 密码与 K8s Secret 一致（否则执行 `ALTER USER`）
- [ ] 已执行所有迁移脚本（`migrate_evaluation_v1.sql`、`migrate_kb_v3.sql`、`migrate_p4_v1.sql`、`migrate_tool_audit_log.sql`）
- [ ] conversation 表已包含 P4 字段（`diagnostic_stage`、`pending_confirm` 等）

**验证 conversation 表 P4 字段**：
```bash
sudo -n k3s kubectl exec -n hci-dev postgres-0 -- \
  psql -U hci_admin -d hci_db -c "\d conversation" | grep -E "diagnostic_stage|pending_confirm"
```

**手动补充 conversation P4 字段**（无对应迁移脚本，需手动执行）：
```sql
ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS diagnostic_stage VARCHAR(8) NOT NULL DEFAULT 'S0',
    ADD COLUMN IF NOT EXISTS category_l1 VARCHAR(100),
    ADD COLUMN IF NOT EXISTS category_l2 VARCHAR(100),
    ADD COLUMN IF NOT EXISTS category_id VARCHAR(32),
    ADD COLUMN IF NOT EXISTS hypothesis JSONB,
    ADD COLUMN IF NOT EXISTS react_state JSONB,
    ADD COLUMN IF NOT EXISTS pending_confirm JSONB;
```
