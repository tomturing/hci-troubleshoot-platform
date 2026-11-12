---
status: active
category: deploy
audience: operator
last_updated: 2026-11-13
owner: team
---

# ArgoCD App of Apps 架构说明

## 当前架构

```
deploy/gitops/
├── bootstrap/                  # 手动 apply 的 Root Application
│   └── argocd-root.yaml        # App of Apps 入口，管理 argo-apps/local/
│
├── argo-apps/local/            # 子 Applications（被 argocd-root 管理）
│   ├── argocd-rbac.yaml        # sync-wave: -1（先同步）
│   ├── argocd-ops.yaml         # sync-wave: 0（后同步）
│   ├── hci-platform-dev.yaml
│   ├── hci-platform-obs-dev.yaml
│   ├── hci-platform-infra-dev.yaml
│   ├── hci-platform-data-dev.yaml
│   ├── hci-platform-dsh-dev.yaml   # DSH 独立命名空间 hci-dsh
│   └── hci-sim-dev.yaml
│
├── argocd-ops/                 # 运维资源（被 argocd-ops App 管理）
│   ├── argocd-repo-server-copyutil-watchdog.yaml
│   ├── argocd-repo-server-probe-patch.yaml (PreSync Hook)
│   └── pod-anomaly-observability.yaml
│
└── argocd-rbac/                # RBAC 资源（被 argocd-rbac App 管理）
│   └── argocd-repo-server-watchdog-rbac.yaml
```

## 同步顺序控制

通过 `argocd.argoproj.io/sync-wave` annotation 控制子 Application 同步顺序：

| Application | sync-wave | 说明 |
|------------|-----------|------|
| argocd-rbac | -1 | 先同步，确保 SA/Role 就绪 |
| argocd-ops | 0 | PreSync Job 可正常执行（已有 RBAC 权限）|
| 其他 Apps | 默认 | 业务应用 |

## 依赖关系

```
argocd-rbac (sync-wave: -1)
    ↓ 先同步完成
argocd-ops (sync-wave: 0)
    ↓ PreSync Job 执行（使用 argocd-repo-server-watchdog SA）
其他 Apps
```

**关键点**：PreSync Hook Job 需要 `argocd-repo-server-watchdog` ServiceAccount，该 SA 由 argocd-rbac 创建。sync-wave 确保依赖顺序正确。

## Bootstrap 步骤

**新集群初始化**：

```bash
# 步骤 1：手动 apply Root Application
kubectl apply -f deploy/gitops/bootstrap/argocd-root.yaml

# 步骤 2：等待所有子 Applications 同步完成
kubectl get application -n argocd -w

# 状态应为：
# argocd-rbac  Synced/Healthy
# argocd-ops   Synced/Healthy
# 其他 Apps    Synced/Healthy
```

**存量集群重新对齐（必做）**：Root Application 不被自己管理。若集群中的
`argocd-root` 对象是早期手工版本（缺 `spec.syncPolicy`），则 Git 里新增的
子 Application 永远不会被创建，且 `selfHeal` 不会生效。判据：

```bash
kubectl get application argocd-root -n argocd \
  -o jsonpath='{.spec.syncPolicy.automated}'
# 输出为空即表示控制权已失效，必须重新 apply 一次 bootstrap 文件
```

合入涉及子 Application 的变更后，重新执行
`kubectl apply -f deploy/gitops/bootstrap/argocd-root.yaml`，再用
`kubectl get applications -n argocd -w` 确认全部 `Synced/Healthy`。
子 Application 的 Helm releaseName 必须显式声明：单源 Application 缺省以
Application 名作为 releaseName，会改变 `spec.selector` 这个不可变字段并导致
同步永久失败。

**首次同步 PreSync Hook 可能失败的情况**：

如果集群首次 bootstrap 时 RBAC 还未就绪：
```bash
# 手动先创建 RBAC
kubectl apply -f deploy/gitops/argocd-rbac/

# 触发 argocd-ops 同步
kubectl patch application argocd-ops -n argocd --type merge -p '{"operation":{"sync":{"revision":"HEAD"}}}'
```

## 设计原则

1. **Root Application 在独立目录**：避免循环引用
2. **sync-wave 控制顺序**：确保依赖关系正确
3. **PreSync Hook 需要 RBAC**：通过 sync-wave 确保权限先就绪
4. **Root 的 syncPolicy 属于运行时状态**：bootstrap 对象不被 Git 自动更新，
   升级 Root 定义必须重新 apply；子 Application 必须显式钉死 Helm releaseName

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-22 | v2.0 | 架构重构：argocd-root 只管理 argo-apps/local/ | — |
| 2026-04-22 | v1.1 | argocd-ops 改为子 Application，使用 sync-wave | — |
| 2026-04-22 | v1.0 | argocd-rbac 从 argocd-ops 拆分 | — |
| 2026-11-13 | v2.1 | 补齐 DSH/hci-sim 子应用；明确存量集群重新 bootstrap 契约与 Helm releaseName 不可变约束 | [dev 与 staging 能力面对齐](events/2026-11-13-dev与staging能力面对齐与GitOps漂移根治.md) |