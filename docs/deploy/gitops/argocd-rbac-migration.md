# argocd-rbac 拆分迁移指南

## 背景

原设计中 `argocd-ops` Application 包含 RBAC 资源和 PreSync Job：
- PreSync Job 需要 Role 权限才能执行
- Role 在 Sync 阶段才同步 → 循环依赖导致死锁

解决方案：将 RBAC 拆分至独立的 `argocd-rbac` Application。

## 同步顺序控制

通过 `argocd.argoproj.io/sync-wave` annotation 控制 App of Apps 中的同步顺序：

| Application | sync-wave | 说明 |
|------------|-----------|------|
| argocd-rbac | -1 | 先于 argocd-ops 同步 |
| argocd-ops | 0 | 默认，在 argocd-rbac 之后 |

## 迁移步骤

### 从"Role 不存在导致 argocd-ops 死锁"状态迁移

如果集群处于 `argocd-ops OutOfSync + PreSync Job Forbidden` 状态：

**步骤 1：手动 apply RBAC 资源**

```bash
kubectl apply -f deploy/gitops/argocd-rbac/
```

**步骤 2：创建 argocd-rbac Application**

```bash
kubectl apply -f deploy/gitops/argo-apps/local/argocd-rbac.yaml
```

**步骤 3：等待 argocd-rbac 同步完成**

```bash
kubectl get application argocd-rbac -n argocd
# 状态应为 Synced + Healthy
```

**步骤 4：触发 argocd-ops 同步**

```bash
kubectl patch application argocd-ops -n argocd --type merge -p '{"operation":{"sync":{"revision":"HEAD"}}}'
```

### 正常状态下的自动同步

PR 合并后，ArgoCD 会自动：
1. 发现 `argocd-rbac.yaml` → 创建 Application（sync-wave: -1）
2. 同步 `argocd-rbac` → RBAC 权限就绪
3. 同步 `argocd-ops`（sync-wave: 0）→ PreSync Job 执行成功

## 回滚步骤

如需回滚到旧设计（RBAC 在 argocd-ops 内）：

**步骤 1：删除 argocd-rbac Application**

```bash
kubectl delete application argocd-rbac -n argocd
```

**步骤 2：恢复 argocd-ops 的 RBAC 配置**

```bash
# 从 Git 恢复旧版本的 argocd-repo-server-copyutil-watchdog.yaml
kubectl apply -f deploy/gitops/argocd-ops/argocd-repo-server-copyutil-watchdog.yaml
```

**步骤 3：触发 argocd-ops 同步**

```bash
kubectl patch application argocd-ops -n argocd --type merge -p '{"operation":{"sync":{"revision":"HEAD"}}}'
```

## RBAC 权限说明

`argocd-repo-server-watchdog` Role 包含以下权限：

| 资源 | 权限 | 用途 |
|-----|------|------|
| pods | get, list, watch, delete | watchdog 删除 CrashLoopBackOff Pod |
| deployments | get, list, watch | kubectl rollout status 需要 |
| deployments (argocd-repo-server) | patch, update | PreSync Job patch probe 配置 |
| configmaps | get, list | PreSync Job 读取配置 |
| configmaps (argocd-cmd-params-cm) | patch, update | PreSync Job patch parallelism |

**注意**：带 `resourceNames` 的规则不匹配 collection 级请求（list/watch），因此拆分成两条规则。

## 变更历史

- 2026-04-22 v1.0 初版