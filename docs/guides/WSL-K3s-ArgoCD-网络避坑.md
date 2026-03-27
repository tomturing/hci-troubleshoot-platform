# WSL + K3s + ArgoCD 网络避坑指南

| 字段     | 内容                        |
|--------|-----------------------------|
| 创建日期 | 2026-03-27                  |
| 适用环境 | WSL2 + K3s + Clash（透明代理）|
| 关键词   | ArgoCD、DNS、Clash、fake-IP、ndots、HTTPS_PROXY |

---

## 问题一：ArgoCD repo-server 无法访问 GitHub（EOF / Connection Reset）

### 现象

```
`git fetch origin --tags --force --prune` failed timeout after 1m30s
failed to list refs: Get "https://github.com/.../info/refs": EOF
wget: got bad TLS record (len:0) while expecting handshake record
```

### 根因（两层叠加）

```
Pod DNS 查询 github.com
  → ndots:5，先尝试 github.com.tail9f1936.ts.net（Tailscale search 域）
  → Tailscale DNS (100.100.100.100) 转发给 Clash
  → Clash 返回 fake-IP 198.18.0.x
  → NodeHosts 里的真实 IP 被绕过
  → Pod TCP 443 连接到 fake-IP → Clash TUN 拦截 → TLS 被重置
```

### 解法 A：Clash 开启局域网连接 + HTTPS_PROXY（推荐，长期方案）

**步骤一**：在 Clash Verge → 设置 → Clash 设置 → **开启"局域网连接"**开关。

**步骤二**（集群运行时操作，无需改配置文件）：
```bash
# 将 Windows 主机 IP 和 Clash 端口替换为实际值（端口在 Clash 设置页"端口设置"中查看）
PROXY="http://172.26.96.1:7897"

kubectl patch deployment argocd-repo-server -n argocd --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"HTTPS_PROXY","value":"'"$PROXY"'"}},
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"HTTP_PROXY","value":"'"$PROXY"'"}},
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"NO_PROXY","value":"10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,localhost,127.0.0.1"}}
]'
```

**效果**：git 通过代理的 `CONNECT` 隧道建立 TLS，流量不经过 Clash TUN，TLS 握手正常。

---

### 解法 B：CoreDNS NodeHosts + repo-server ndots:1（辅助手段，解决 DNS 污染）

> 解法 A 已解决根因。解法 B 是在没有代理时单独暂时缓解 DNS 问题的手段，但无法解决 Clash TUN 拦截 TCP 443 的问题。

**CoreDNS 追加 GitHub 真实 IP**（运行时 patch，不改文件）：
```bash
# 查当前 CoreDNS ConfigMap 并追加 NodeHosts
kubectl edit configmap coredns -n kube-system
# 在 NodeHosts 段追加：
# 140.82.112.4 github.com
# 140.82.114.4 github.com
# 185.199.108.133 raw.githubusercontent.com
kubectl rollout restart deployment coredns -n kube-system
```

**修复 ndots:5 搜索顺序**：
```bash
kubectl patch deployment argocd-repo-server -n argocd --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/dnsConfig","value":{"options":[{"name":"ndots","value":"1"}]}}]'
```

**原理**：`ndots:5` 时 `github.com`（1个点 < 5）先走 search 域，命中 Tailscale 的 `github.com.tail9f1936.ts.net` → fake-IP。改为 `ndots:1` 后 `github.com`（1个点 ≥ 1）直接作为绝对域名解析，走 NodeHosts 返回真实 IP。

---

## 问题二：NodePort 端口冲突

### 现象

```
Service "openclaw" NodePort 30789 already allocated
Service "learningclaw" NodePort 30790 already allocated
```

### 根因

旧的 Helm release（`hci-platform` in `hci-troubleshoot`）未清理，占用了相同 NodePort。

### 解法

```bash
# 确认冲突来源
kubectl get svc -A | grep "30789\|30790"

# 卸载旧 release（需用户确认后执行）
helm uninstall hci-platform -n hci-troubleshoot
```

---

## 问题三：git daemon 路径携带 .git 后缀导致 404

### 现象

使用本地 git daemon（`git://` 协议）时，repo-server 日志：
```
'/mnt/d/aihci/hci-troubleshoot-platform.git' does not appear to be a git repository
```

### 根因

ArgoCD 自动在 repoURL 路径末尾追加 `.git`，但本地目录名无 `.git` 后缀。

### 解法

ArgoCD Application 中的 repoURL 不要带 `.git`：
```yaml
# 错误
repoURL: git://172.26.101.255/hci-troubleshoot-platform.git
# 正确
repoURL: git://172.26.101.255/hci-troubleshoot-platform
```

> ⚠️ git daemon 仅作临时手段。推荐使用解法 A（Clash 代理 + HTTPS），还原为官方 GitHub HTTPS URL。

---

## 环境说明：各命名空间职责

| 命名空间          | 状态   | 用途                                                 |
|-----------------|--------|------------------------------------------------------|
| `argocd`        | 正常   | ArgoCD 控制面（repo-server、application-controller 等）|
| `hci-dev`       | 正常   | hci-platform 主业务服务（API、前端、各微服务）           |
| `hci-observability` | 正常 | 可观测性栈（Prometheus、Grafana、Loki、Tempo）           |
| `hci-troubleshoot`  | 残留 | 旧 Helm 直接部署遗留，Helm release 已卸载，命名空间可手动删除 |
| `kube-system`   | 正常   | K3s 系统组件、CoreDNS 等                               |

**`hci-troubleshoot` 安全删除命令**（确认无残留资源后执行）：
```bash
kubectl get all -n hci-troubleshoot  # 确认为空
kubectl delete namespace hci-troubleshoot
```

---

## WSL 重启后的恢复清单

每次 WSL 重启后，以下集群运行时 patch 需重新执行（均未持久化到文件）：

- [ ] **CoreDNS NodeHosts**：重启后 ConfigMap 是否保留需验证（`kubectl get cm coredns -n kube-system -o yaml | grep github`）
- [ ] **repo-server HTTPS_PROXY**：Deployment 定义已 patch，Pod 重建后自动继承 ✅（Deployment 保留）
- [ ] **repo-server ndots:1**：同上，Deployment patch 持久 ✅
- [ ] **git daemon**：进程级，WSL 重启后需重新启动（如已改用代理方案则不需要）

> **建议**：将 `HTTPS_PROXY` 和 `ndots:1` 以 Helm values override 或 ArgoCD 自身 Helm values 的形式固化，避免依赖运行时 patch。

---

## 问题四：conversation-service 数据库连接失败（500 Internal Server Error）

### 现象

- `GET /api/conversations/case/{case_id}` → 500
- `POST /api/conversations/` → 500
- 日志：`ConnectionError: unexpected connection_lost() call`（SSL 握手阶段）
- 直接 tcp 测试 postgres:5432 可达，但 asyncpg connect 失败

### 根因

`conversation-service` Helm 模板在 `externalDns: true` 时注入自定义 `dnsConfig`，其中搜索域
（`searches`）**硬编码了旧命名空间**：

```yaml
# 错误（deploy/helm/hci-platform/templates/conversation-service/deployment.yaml）
searches:
  - hci-troubleshoot.svc.cluster.local   # ← 应为当前命名空间 hci-dev
  - svc.cluster.local
  - cluster.local
```

Pod 尝试解析 `postgres` 时：

1. 先搜索 `postgres.hci-troubleshoot.svc.cluster.local` → NXDOMAIN（命名空间不存在）
2. 再搜索 `postgres.svc.cluster.local` → NXDOMAIN
3. 最终回退为裸名 `postgres` → 走公网 DNS（114.114.114.114 / 1.2.4.8）
4. **Clash fake-ip 拦截** → 返回 `198.18.0.149`（虚假 IP）
5. asyncpg 连接到 fake-ip，握手失败 → `unexpected connection_lost()`

### 验证命令

```bash
# 在 conversation-service Pod 内验证 DNS 解析
kubectl -n hci-dev exec deployment/conversation-service -- python3 -c "
import socket
print('postgres:', socket.gethostbyname('postgres'))  # 错误时为 198.18.x.x
print('FQDN:', socket.gethostbyname('postgres.hci-dev.svc.cluster.local'))  # 正确
"
```

### 解法

修改 Helm 模板，使用 `hci.namespace` helper 动态生成搜索域：

```yaml
# 正确（deploy/helm/hci-platform/templates/conversation-service/deployment.yaml）
searches:
  - {{ include "hci.namespace" . }}.svc.cluster.local
  - svc.cluster.local
  - cluster.local
```

修改后 `git commit && git push`，ArgoCD 自动 sync 重新部署 conversation-service。

### 已修复版本

Commit: 见 git log — "Fix: conversation-service 搜索域硬编码旧命名空间 hci-troubleshoot"

