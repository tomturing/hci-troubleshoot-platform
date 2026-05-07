# ops-agent 镜像更新 Skill

> **用途**：将 ops-agent 仓库 `feature-hci` 分支的最新 CI 构建 tag 同步到 hci-platform-env 的 values.yaml，触发 ArgoCD 自动部署。

## 背景

- **ops-agent 仓库**：`https://github.com/P3n9W31/ops-agent`，独立迭代
- **使用分支**：`feature-hci`
- **镜像仓库**：`ghcr.io/p3n9w31/ops-agent:<日期-时间-sha7>`（版本化 tag）
- **环境仓库**：`hci-platform-env`，存放各环境的 `opsAgent.image.tag`

ops-agent 不在本项目 CI 构建范围内。**如果 `P3n9W31/ops-agent` 仓库已配置 `HCI_PLATFORM_ENV_TOKEN` secret，CI 会在构建后自动回写 tag，无需手动操作。** 仅在 PAT 未配置或 CI 失败时才需要本 skill 手动干预。

> 验证自动联动是否生效：
> ```bash
> gh run list --repo P3n9W31/ops-agent --workflow "Build Docker Image" --limit 1
> # 如果 sync-env-repo job 显示 ✅，则无需手动操作
> ```

---

## 执行步骤

### 步骤 1：获取 ops-agent 最新版本化 tag

```bash
# 从最新一次成功的 Build Docker Image workflow 获取版本化 tag
OPS_TAG=$(gh run list --repo P3n9W31/ops-agent --workflow "Build Docker Image" --status success --limit 1 --json databaseId --jq '.[0].databaseId' | \
  xargs -I{} gh run view --repo P3n9W31/ops-agent {} --log | \
  grep "镜像标签: ghcr.io/p3n9w31/ops-agent:" | grep -v latest | head -1 | \
  sed 's/.*ghcr.io\/p3n9w31\/ops-agent://')
echo "最新版本化 tag: ${OPS_TAG}"
```

> **注意**：ops-agent CI 同时推送版本化 tag（如 `20260507-0921-d8c0541`）和 `latest` tag。
> 推荐使用**版本化 tag**，便于追溯和回滚。仅在特殊场景（如测试）才使用 `latest`。

### 步骤 2：确认要更新的环境

询问用户要更新哪个环境：
- `dev`（默认）
- `staging`
- `prod`
- 或 `all`（同时更新所有环境）

### 步骤 3：更新 hci-platform-env 仓库

**方法 A：通过 Python 脚本（推荐，可靠处理多行 YAML）**

```bash
TARGET_ENV="dev"  # 或 staging/prod

# 进入本地仓库（如未克隆则先 clone）
cd /mnt/d/aihci/hci-platform-env  # 或 git clone https://github.com/tomturing/hci-platform-env.git /tmp/hci-platform-env

git pull origin main

VALUES="environments/${TARGET_ENV}/values.yaml"
OPS_TAG="${OPS_TAG}"  # 来自步骤 1

python3 - "$VALUES" <<'PY'
import re, sys, os
f = sys.argv[1]
tag = os.environ.get("OPS_TAG") or "latest"
with open(f) as fp:
    content = fp.read()
# 使用 lambda 函数避免 re.escape 在替换值中引入反斜杠
pattern = r'(opsAgent:.*?image:.*?tag:\s*)"[^"]*"'
new = re.sub(pattern, lambda m: f'{m.group(1)}"{tag}"', content, count=1, flags=re.DOTALL)
if new != content:
    with open(f, 'w') as fp:
        fp.write(new)
    print(f"✅ opsAgent.image.tag → {tag}")
else:
    print("⚠️  未找到 opsAgent.image.tag，跳过")
PY

git add "$VALUES"
git commit -m "chore(${TARGET_ENV}): ops-agent tag -> ${OPS_TAG} [skip ci]"
git push origin main
```

**方法 B：通过 GitHub API（无需本地克隆）**

```bash
TARGET_ENV="dev"  # 或 staging/prod

# 获取当前 values.yaml 内容和 SHA
CURRENT_VALUES=$(gh api repos/tomturing/hci-platform-env/contents/environments/${TARGET_ENV}/values.yaml --jq '.content' | base64 -d)
CURRENT_FILE_SHA=$(gh api repos/tomturing/hci-platform-env/contents/environments/${TARGET_ENV}/values.yaml --jq '.sha')

# 使用 Python 处理 YAML（避免 sed 对多行 YAML 的脆弱性）
OPS_TAG="${OPS_TAG}"
UPDATED_VALUES=$(python3 -c "
import re, os
content = '''$CURRENT_VALUES'''
tag = os.environ.get('OPS_TAG') or 'latest'
pattern = r'(opsAgent:.*?image:.*?tag:\s*)\"[^\"]*\"'
new = re.sub(pattern, lambda m: f'{m.group(1)}\"{tag}\"', content, count=1, flags=re.DOTALL)
print(new)
")

# 提交更新
ENCODED_CONTENT=$(echo "$UPDATED_VALUES" | base64 -w 0)
gh api --method PUT repos/tomturing/hci-platform-env/contents/environments/${TARGET_ENV}/values.yaml \
  -f message="chore(${TARGET_ENV}): ops-agent tag -> ${OPS_TAG} [skip ci]" \
  -f content="$ENCODED_CONTENT" \
  -f sha="$CURRENT_FILE_SHA"
```

### 步骤 4：触发 ArgoCD 同步

```bash
# 方法 A：强制刷新
kubectl annotate application -n argocd hci-platform-${TARGET_ENV} argocd.argoproj.io/refresh="true" --overwrite

# 方法 B：等待自动同步（默认 3 分钟内）
echo "等待 ArgoCD 自动同步..."

# 方法 C：使用 argocd CLI（如果可用）
argocd app sync hci-platform-${TARGET_ENV}
```

### 步骤 5：验证部署状态

```bash
# 检查 Pod 状态（hci-dev / hci-staging / hci-prod）
kubectl get pods -n hci-${TARGET_ENV} -l app.kubernetes.io/name=ops-agent-service

# 检查实际运行的镜像 tag
kubectl get deployment -n hci-${TARGET_ENV} ops-agent-service \
  -o jsonpath='{.spec.template.spec.containers[0].image}'

# 检查 ArgoCD 应用状态
kubectl get application -n argocd hci-platform-${TARGET_ENV} \
  -o jsonpath='{.status.health.status} {.status.sync.status}'
```

---

## 快速一键执行

如果用户只提供环境名称，按以下顺序自动执行：

1. 从 CI 获取 ops-agent feature-hci 分支最新版本化 tag
2. 用 Python 脚本更新对应环境的 values.yaml
3. 触发 ArgoCD 同步
4. 等待 30 秒后验证部署状态

---

## 输入参数

用户调用时可以提供：
- 环境名称：`dev`、`staging`、`prod`、`all`（默认 `dev`）
- 具体 tag：如 `20260507-0921-d8c0541`（默认取最新 CI 构建 tag）

示例调用：
- `/ops-agent-update` → 更新 dev 环境，使用最新 CI 版本化 tag
- `/ops-agent-update staging` → 更新 staging 环境，使用最新 CI 版本化 tag
- `/ops-agent-update all 20260507-0921-d8c0541` → 同时更新所有环境，指定 tag

---

## 错误处理

| 错误 | 处理方式 |
|------|---------|
| GitHub API 认证失败 | 提示用户检查 `gh auth status` |
| 未找到版本化 tag | 检查 `gh run list --repo P3n9W31/ops-agent --workflow "Build Docker Image"` 是否有成功记录 |
| ArgoCD 同步失败 | 检查 Pod 日志，必要时手动 sync |
| 镜像拉取失败（ImagePullBackOff） | 确认 tag 存在：`docker manifest inspect ghcr.io/p3n9w31/ops-agent:<tag>` |