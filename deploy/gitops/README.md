# GitOps 模板目录

> 本目录用于方案 21 Day 3 的接入资产，提供：
> 1. Argo CD Application 模板（dev/staging/prod）
> 2. 环境仓库骨架模板（environments/*/values.yaml）
>
> 使用建议：
> 1. 新建独立环境仓库（示例：hci-platform-env）。
> 2. 将 env-repo-template 下内容复制到环境仓库根目录。
> 3. 根据实际仓库地址与分支，修改 argo-apps/*.yaml 中的 repoURL/targetRevision。
> 4. 执行 `kubectl apply -f deploy/gitops/argo-apps/` 创建 Argo 应用。
