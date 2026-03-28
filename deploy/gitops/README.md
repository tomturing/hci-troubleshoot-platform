# GitOps 资产目录

> 本目录包含 ArgoCD Application 清单和环境仓库骨架模板。
>
> **双仓模型**：代码仓库（本仓）负责代码和 CI，环境仓库（`hci-platform-env`）存储 Helm values。
> ArgoCD 监听环境仓库实现自动同源。

## 目录结构

```
deploy/gitops/
├── argo-apps/          ← ArgoCD Application 清单
│   ├── hci-platform-infra.yaml          集群级资源（全局单一）
│   ├── hci-platform-data-dev.yaml       数据层 dev
│   ├── hci-platform-data-staging.yaml   数据层 staging
│   ├── hci-platform-data-prod.yaml      数据层 prod
│   ├── hci-platform-obs.yaml            可观测性栈（全局单一）
│   ├── hci-platform-dev.yaml            业务服务 dev
│   ├── hci-platform-staging.yaml        业务服务 staging
│   └── hci-platform-prod.yaml           业务服务 prod
├── argocd-ops/         ← ArgoCD 运维清单（watchdog 等）
├── env-repo-template/  ← 环境仓库骨架（复制到 hci-platform-env）
└── local/              ← 本地密钥（不提交）
```

## 首次部署顺序

```bash
# 1. 集群级资源（StorageClass + ClusterRole，每个集群仅需一次）
kubectl apply -f deploy/gitops/argo-apps/hci-platform-infra.yaml

# 2. 数据层（PostgreSQL + Redis）
kubectl apply -f deploy/gitops/argo-apps/hci-platform-data-dev.yaml

# 3. 可观测性栈
kubectl apply -f deploy/gitops/argo-apps/hci-platform-obs.yaml

# 4. 业务服务
kubectl apply -f deploy/gitops/argo-apps/hci-platform-dev.yaml
```

## 环境仓库接入

1. 新建独立环境仓库（示例：`hci-platform-env`）
2. 将 `env-repo-template/` 下内容复制到环境仓库根目录
3. 修改 `argo-apps/*.yaml` 中的 `repoURL` 和 `targetRevision`
4. 配置 ArgoCD 仓库凭据（见 [local/README.md](local/README.md)）
