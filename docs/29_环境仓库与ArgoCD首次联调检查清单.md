# 29_环境仓库与ArgoCD首次联调检查清单

> 用途：用于方案 21 Day 3-4 首次打通时的逐项检查，避免“流程有文档但联调失败”。

---

## 1. 前置检查

1. 集群可访问：`kubectl cluster-info` 正常。
2. Argo CD 命名空间存在：`kubectl get ns argocd`。
3. 代码仓库可访问：`tomturing/hci-troubleshoot-platform`。
4. 环境仓库可访问：`tomturing/hci-platform-env`。
5. GitHub Secret 已配置：`ENV_REPO_PAT`。
6. 可选仓库变量：`ENV_REPO_NAME`（如不配置，默认使用 `tomturing/hci-platform-env`）。

---

## 2. Argo Application 检查

1. 执行创建：

```bash
kubectl apply -f deploy/gitops/argo-apps/hci-platform-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/hci-platform-staging.yaml
kubectl apply -f deploy/gitops/argo-apps/hci-platform-prod.yaml
```

2. 状态检查：

```bash
kubectl -n argocd get applications
```

3. 期望结果：
- dev/staging：`Synced` 且 `Healthy`（允许短时进度状态）
- prod：存在应用对象，但默认不自动同步

---

## 3. 环境同步工作流检查

1. 进入 GitHub Actions，手动触发 `Env Repo Sync`。
2. 输入参数示例：
- `target_env=dev`
- `image_tag=2026.03.19-verify`
- `services_csv=apiGateway,conversationService`
3. 期望结果：
- 环境仓库自动创建 PR
- PR 仅包含目标环境 `values.yaml` 中 tag 变更

---

## 4. GitOps 同步与发布验证

1. 合并环境仓库 PR。
2. 观察 Argo 应用同步状态。
3. 检查目标命名空间工作负载是否滚动到新 tag。

验证命令：

```bash
kubectl -n hci-dev get deploy
kubectl -n hci-dev get pods
```

---

## 5. 失败回退检查

1. 触发一次错误 tag 演练（仅 dev/staging）。
2. 确认可通过回退环境仓库 commit 恢复。
3. 回退后再次验证应用状态与服务可用性。

---

## 6. 首次联调完成定义

1. 完成 1 次 dev 环境端到端同步（工作流 -> 环境 PR -> Argo 同步 -> 集群生效）。
2. 完成 1 次 staging 环境端到端同步。
3. 完成 1 次回退演练并恢复成功。
4. 产出联调记录并登记在方案 21 执行台账。
