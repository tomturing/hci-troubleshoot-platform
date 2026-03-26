# deploy/gitops/argo-apps/

本目录按 **ArgoCD 实例**分目录管理 Application 定义。  
执行 `kubectl apply -f <目录>/` 时只影响当前连接的集群，杜绝误操作。

---

## 目录结构

```
argo-apps/
  local/     ← 本地 WSL dev 环境（kubectl context = local k3s）
  cloud/     ← 云端 staging + prod 环境（kubectl context = cloud ArgoCD VM）
```

---

## local/ — 本地 WSL ArgoCD

**适用场景：** 开发人员本地调试，单 VM k3s，ArgoCD 独立运行。

| 文件 | Application | 说明 |
|------|-------------|------|
| `hci-platform-infra-dev.yaml` | hci-platform-infra-dev | 集群级 StorageClass + RBAC（dev 集群一次性安装） |
| `hci-platform-data-dev.yaml` | hci-platform-data-dev | dev 数据层 postgres + redis（hci-dev ns） |
| `hci-platform-obs-dev.yaml` | hci-platform-obs-dev | dev 可观测性栈（hci-observability ns） |
| `hci-platform-dev.yaml` | hci-platform-dev | dev 业务服务（hci-dev ns） |

**Bootstrap 命令（首次或重建后）：**
```bash
# 1. 确认 kubectl context 指向本地集群
kubectl config current-context

# 2. 应用所有本地 Application
kubectl apply -f deploy/gitops/argo-apps/local/
```

---

## cloud/ — 云端 ArgoCD（Hub-Spoke）

**适用场景：** 云端 ArgoCD 以 hub 角色同时管理 staging（本集群）和 prod（外部集群 192.168.0.3:6443）。

| 文件 | Application | 目标集群 |
|------|-------------|---------|
| `hci-platform-infra-staging.yaml` | hci-platform-infra-staging | staging（本集群） |
| `hci-platform-data-staging.yaml` | hci-platform-data-staging | staging |
| `hci-platform-obs-staging.yaml` | hci-platform-obs-staging | staging |
| `hci-platform-staging.yaml` | hci-platform-staging | staging |
| `hci-platform-infra-prod.yaml` | hci-platform-infra-prod | prod（192.168.0.3:6443） |
| `hci-platform-data-prod.yaml` | hci-platform-data-prod | prod |
| `hci-platform-obs-prod.yaml` | hci-platform-obs-prod | prod |
| `hci-platform-prod.yaml` | hci-platform-prod | prod（手动同步） |

**Bootstrap 命令（首次或迁移后）：**
```bash
# 1. 确认 kubectl context 指向云端 ArgoCD 所在集群
kubectl config current-context

# 2. 注册 prod 集群到 ArgoCD（首次需要）
argocd cluster add <prod-context-name>

# 3. 应用所有云端 Application
kubectl apply -f deploy/gitops/argo-apps/cloud/
```

---

## 安全约定

- `local/` 目录的 Application **禁止** `kubectl apply` 到云端集群
- `cloud/` 目录的 Application **禁止** `kubectl apply` 到本地集群
- prod Application 均关闭 `automated` 自动同步，所有变更需人工 `argocd app sync`
