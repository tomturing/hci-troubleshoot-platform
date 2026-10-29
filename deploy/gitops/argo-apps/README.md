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

# 2. 设置设备角色标签（仅首次）
kubectl label ns argocd hci.env.role=dev --overwrite

# 3. 通过角色校验脚本应用（推荐）
bash scripts/ops/argocd-apply-apps.sh --scope local --yes
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

# 2. 设置设备角色标签（仅首次）
kubectl label ns argocd hci.env.role=staging --overwrite

# 3. 注册 prod 集群到 ArgoCD（首次需要）
argocd cluster add <prod-context-name>

# 4. 通过角色校验脚本应用（推荐）
bash scripts/ops/argocd-apply-apps.sh --scope cloud --yes
```

---

## 安全约定

- `local/` 目录的 Application **禁止** `kubectl apply` 到云端集群
- `cloud/` 目录的 Application **禁止** `kubectl apply` 到本地集群
- prod Application 均关闭 `automated` 自动同步，所有变更需人工 `argocd app sync`

## 自动同步的强制不变式（2026-10-29 起）

凡开启 `syncPolicy.automated.selfHeal` 的 Application **必须**显式声明 `syncPolicy.retry`：

```yaml
    retry:
      limit: 5
      backoff:
        duration: 15s
        factor: 2
        maxDuration: 2m
```

原因：`selfHeal` 只恢复漂移，**不重试失败的操作**。迁移 hook Job 因依赖对象缺失、API 抖动等
瞬态原因失败时，没有 retry 的 Application 会停在失败态等人手工介入；本仓 2026-10-29 就出现过
PreSync 卡在 `CreateContainerConfigError` 8 分钟以上无任何进展的情况。显式声明还避免依赖
ArgoCD 版本默认重试次数这种隐式契约。

该规则由 `scripts/ci/check_argocd_app_sync_invariants.py` 在 CI（Helm 校验 job）强制，
与本文件同一 PR 修改。配套的迁移触发与 hook 自愈约束见[部署设计 §9.6](../../../docs/deploy/部署设计.md)。

## 统一入口脚本

`scripts/ops/argocd-apply-apps.sh` 会在执行前校验设备角色：

- `--scope local` 仅允许 `dev` 角色执行
- `--scope cloud` 仅允许 `staging` 角色执行

角色来源优先级：`--role` > `HCI_DEVICE_ROLE` > `argocd` namespace 的 `hci.env.role` 标签。
