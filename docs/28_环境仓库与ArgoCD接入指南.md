# 28_环境仓库与ArgoCD接入指南

> 目标：完成方案 21 Day 3-4 的环境仓库与 Argo CD 接入，形成可执行的 GitOps 部署闭环。

---

## 1. 接入目标

1. 应用仓库负责代码、镜像构建与质量门禁。
2. 环境仓库负责环境声明（dev/staging/prod）。
3. Argo CD 监听环境声明并同步到 K3s 集群。

---

## 2. 前置条件

1. 已完成方案 21 Day 1-2 基线：
- PR 模板
- CI 门禁
- 发布与回滚 SOP
2. 集群可访问且具备安装 Argo CD 权限。
3. 已准备环境仓库（建议名称：hci-platform-env）。

---

## 3. 目录与模板来源

1. Argo Application 模板：
- [deploy/gitops/argo-apps/hci-platform-dev.yaml](../deploy/gitops/argo-apps/hci-platform-dev.yaml)
- [deploy/gitops/argo-apps/hci-platform-staging.yaml](../deploy/gitops/argo-apps/hci-platform-staging.yaml)
- [deploy/gitops/argo-apps/hci-platform-prod.yaml](../deploy/gitops/argo-apps/hci-platform-prod.yaml)

2. 环境仓库骨架模板：
- [deploy/gitops/env-repo-template/environments/dev/values.yaml](../deploy/gitops/env-repo-template/environments/dev/values.yaml)
- [deploy/gitops/env-repo-template/environments/staging/values.yaml](../deploy/gitops/env-repo-template/environments/staging/values.yaml)
- [deploy/gitops/env-repo-template/environments/prod/values.yaml](../deploy/gitops/env-repo-template/environments/prod/values.yaml)

---

## 4. 实施步骤

## Step 1：初始化环境仓库

1. 新建私有仓库 `hci-platform-env`。
2. 将 `deploy/gitops/env-repo-template/environments/*` 复制到环境仓库根目录。
3. 按环境填写镜像 tag 与必要差异配置。

## Step 2：安装或确认 Argo CD

```bash
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

## Step 3：调整 Application 模板

在以下文件中替换占位符：

1. `repoURL: https://github.com/<org>/hci-troubleshoot-platform.git`
2. `repoURL: https://github.com/<org>/hci-platform-env.git`
3. `targetRevision: main`（如使用 release 分支请改为对应分支）

## Step 4：创建 Application

```bash
kubectl apply -f deploy/gitops/argo-apps/hci-platform-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/hci-platform-staging.yaml
kubectl apply -f deploy/gitops/argo-apps/hci-platform-prod.yaml
```

## Step 5：同步策略校验

1. dev/staging：应为自动同步。
2. prod：应为手动同步（人工审批后再执行）。

## Step 6：配置环境仓库同步工作流（可选，推荐）

本仓库提供手动触发的环境同步工作流模板：

1. [/.github/workflows/env-repo-sync.yml](../.github/workflows/env-repo-sync.yml)
2. [/scripts/sync-env-repo-tags.sh](../scripts/sync-env-repo-tags.sh)

使用前请先完成：

1. 将 `repository: <org>/hci-platform-env` 替换为真实仓库地址。
2. 在 GitHub Secrets 中配置 `ENV_REPO_PAT`（对环境仓库具备写权限）。

使用方式：

1. 进入 Actions -> `Env Repo Sync`。
2. 填写 `target_env`、`image_tag`、`services_csv`。
3. 工作流会自动修改环境仓库 values 并创建 PR。

---

## 5. 验收标准

1. 修改环境仓库 `environments/dev/values.yaml` 中镜像 tag 后，dev 自动部署成功。
2. staging 可自动同步并通过冒烟验证。
3. prod 无人工审批时不会自动部署。
4. Argo UI 与集群状态一致，无长期 OutOfSync。
5. 手动触发 `Env Repo Sync` 可成功在环境仓库生成 PR。

---

## 6. 常见问题

1. 问题：Argo 无法拉取私有仓库。
- 处理：在 Argo CD 配置仓库凭据（PAT 或 SSH key）。

2. 问题：Application 一直 OutOfSync。
- 处理：检查 values 路径、ref 名称（`$values/...`）与分支是否一致。

3. 问题：prod 被自动发布。
- 处理：检查 prod Application 是否误配了 `automated` 字段。

---

## 7. 与方案 21 的关系

1. 本文是方案 21 的 Day 3-4 操作指南。
2. 发布前后检查与回滚仍以 [24_发布检查清单与回滚SOP.md](24_发布检查清单与回滚SOP.md) 为准。
3. 执行过程记录到 [25_方案21执行台账模板.md](25_方案21执行台账模板.md)。
4. 首次联调执行建议参考 [29_环境仓库与ArgoCD首次联调检查清单.md](29_环境仓库与ArgoCD首次联调检查清单.md)。
