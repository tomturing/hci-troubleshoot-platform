# 本地密钥目录（不提交）

> 目的：存放本机正式密钥文件，例如 Argo 仓库凭据。
> 
> 安全规则：
> 1. 本目录下 `*.yaml/*.yml` 已在 `.gitignore` 中忽略。
> 2. 仅允许本地创建，不得提交到远程仓库。

---

## 推荐用法（方案 B）

1. 从模板复制本地正式文件：

```bash
cp deploy/gitops/argocd-repo-creds.example.yaml deploy/gitops/local/argocd-repo-creds.yaml
```

2. 编辑本地正式文件，填写真实 PAT。

3. 应用到集群：

```bash
kubectl apply -f deploy/gitops/local/argocd-repo-creds.yaml
```

4. 验证：

```bash
kubectl -n argocd get secret -l argocd.argoproj.io/secret-type=repo-creds
kubectl -n argocd get applications
```

---

## 清理建议

1. 若 PAT 轮换，更新本地文件并重新 `kubectl apply`。
2. 若机器迁移，手动转移本地文件并重新校验权限。
