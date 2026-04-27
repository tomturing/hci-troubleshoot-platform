---
status: active
category: deploy
audience: developer, operator
last_updated: 2026-04-27
owner: team
---

# staging Prometheus reload hook 修复

## 背景

2026-04-27，ArgoCD Application hci-platform-staging 长时间处于 OutOfSync。

直接原因不是业务 Deployment 同步失败，而是 Helm PostSync Hook Job `post-upgrade-prometheus-reload-1` 一直未完成，导致整次同步卡在 `waiting for completion of hook batch/Job/post-upgrade-prometheus-reload-1`。

## 根因

原实现依赖 `bitnami/kubectl:latest`，通过 `kubectl exec` 进入 Prometheus Pod 执行 `/-/reload`。

在 staging 节点 192.168.0.4 上，kubelet 拉取 docker.io 镜像超时：

```text
Failed to pull image "bitnami/kubectl:latest":
Head "https://registry-1.docker.io/v2/bitnami/kubectl/manifests/latest":
dial tcp 128.242.240.85:443: i/o timeout
```

这说明问题本质不是 Prometheus 本身不可用，而是 hook 设计对外部镜像仓库有不必要依赖。

## 修复

按第一性原理重构 hook：

1. 删除不必要的 ServiceAccount、Role、RoleBinding。
2. 不再 `kubectl exec` 进入 Pod。
3. 改为直接请求集群内 Prometheus Service：`http://prometheus.<obs-namespace>.svc.cluster.local:9090/-/reload`。
4. 使用仓库内已稳定使用的 `curlimages/curl:8.6.0`。
5. 为 Job 增加 `hook-failed` 删除策略，避免失败后残留资源长期阻塞同步判断。

## 结论

该修复把 hook 从“依赖 Pod exec + docker.io 拉镜像 + RBAC”简化为“集群内 HTTP POST”。

同日排查还发现 `argocd-root` 持续显示 `OutOfSync` 的另一个独立根因：

- `deploy/gitops/argo-apps/cloud/argocd-rbac.yaml` 显式声明了 `spec.source.directory.recurse: false`。
- live 的 `Application/argocd-rbac` 会省略这个默认值字段。
- `argocd-rbac` 自己判定该字段等价，所以仍显示 `Synced`。
- `argocd-root` 把子 Application 当普通 YAML 比较，因此持续把 `argocd-rbac` 记为 `OutOfSync`。

对应修复是从 Git 清单中删除这个冗余默认值，避免 root Application 永久漂移。

在 Git 变更生效前，cloud bootstrap 的 `argocd-root` 还增加了临时控制面兜底：

- 对 `Application/argocd-rbac` 的 `/spec/source/directory` 增加 `ignoreDifferences`。
- 这样即使 Kubernetes 继续省略该零值字段，`argocd-root` 也不会再把它视为真实漂移。

收益：

- 同步路径更短，失败面更小。
- 不再依赖 docker.io 可达性。
- 不再需要额外 RBAC。
- 即使 reload 失败，也能让 Hook Job 尽快结束，不再让 ArgoCD 长时间停留在 Running。