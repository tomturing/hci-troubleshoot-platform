---
status: frozen
category: solution
audience: developer
last_updated: 2026-04-21
owner: team
---

# ArgoCD PreSync Hook 镜像修复方案

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-21 | v1.0 | 初版 | [任务文档](../task/events/2026-04-21-ArgoCD-PreSync-Hook镜像修复任务.md) |

---

## 问题概述

**根因**：PR#170（commit 732610f）的 PreSync Hook Job 设计复制了 `argocd-repo-server-copyutil-watchdog.yaml` 的镜像 `quay.io/argoproj/argocd:v3.3.6`，但该镜像不含 `kubectl` 工具。

**现象**：
- 7个 `argocd-repo-server-probe-patch-*` pods 处于 Error 状态
- Job 日志：`kubectl: not found`
- 每次 ArgoCD Sync 触发 PreSync Hook 都失败

---

## 方案设计

### 业界最佳实践参考

| 来源 | 推荐做法 |
|------|---------|
| ArgoCD 官方文档 | PreSync Hook 使用包含目标工具的镜像 |
| K8s 社区实践 | kubectl 版本 ≤ 集群版本（避免 API 不兼容） |
| DevOps 规范 | 禁止 latest tag，使用固定版本号 |

### 方案选择

| 方案 | 镜像 | 优点 | 缺点 | 评分 |
|------|------|------|------|------|
| **A（选中）** | `rancher/kubectl:v1.33.9` | 官方工具镜像、版本偏移合规（±1 minor） | 需单独维护版本 | ★★★★☆ |
| B | 参考 copyutil-watchdog 用 openssl + SA token | 不依赖外部镜像 | 实现复杂、需重写逻辑 | ★★☆☆☆ |
| C | 构建自定义镜像（argocd + kubectl） | 功能完整 | 维护成本高、偏离官方 | ★☆☆☆☆ |

### 为什么选择方案 A？

1. **符合业界标准**：`rancher/kubectl` 是社区广泛使用的工具镜像
2. **版本偏移合规**：kubectl v1.33 与集群 v1.34 相差 1 minor，在 Kubernetes 官方支持范围（±1 minor）
3. **标签清晰**：版本号标签（如 `v1.33.9`）便于引用和审计
4. **修复成本低**：只需改一行镜像配置

### 为什么不选方案 B 和 C？

- **方案 B**：copyutil-watchdog 使用 openssl + SA token 调用 K8s API，逻辑复杂，probe-patch 需要执行多个 kubectl patch 命令，重写成本远高于换镜像
- **方案 C**：构建自定义镜像偏离官方路径，维护负担增加，不符合"简化优先"原则

### 为什么不用 bitnami/kubectl？

`bitnami/kubectl` 镜像使用 sha256 标签而非版本号标签（如 `1.31`），版本号标签不存在于 Docker Hub：
```bash
# Docker Hub 查询显示只有 latest 和 sha256 标签
curl -s "https://hub.docker.com/v2/repositories/bitnami/kubectl/tags?page_size=20" | jq -r '.results[].name'
# 输出：latest, sha256-xxx...
```
使用 `bitnami/kubectl:1.31` 会导致 `ErrImagePull: not found`。

### 为什么不用 rancher/kubectl:v1.32.13？

虽然 v1.32 ≤ v1.34，但两者相差 2 个 minor，超出 Kubernetes 官方版本偏移支持范围（±1 minor）。正确的选择应与 apiserver 保持同 minor 或相差最多 1 个 minor。

---

## 详细变更

### 文件：`deploy/gitops/argocd-ops/argocd-repo-server-probe-patch.yaml`

```yaml
# 改动前（PR#195 初步修复，失败）
containers:
  - name: patch
    image: bitnami/kubectl:1.31  # 错误：版本标签不存在

# 改动后（PR#196 最终修复）
containers:
  - name: patch
    image: rancher/kubectl:v1.33.9  # 正确：±1 minor 版本偏移合规
    imagePullPolicy: IfNotPresent
    resources:  # 新增：资源限制
      requests:
        cpu: 50m
        memory: 64Mi
      limits:
        cpu: 100m
        memory: 128Mi

# 新增：超时保护（需大于 rollout timeout + 缓冲）
spec:
  template:
    spec:
      activeDeadlineSeconds: 180  # rollout timeout 120s + 缓冲
```

---

## 影响范围

### 受影响的模块
- `argocd-ops` Application：PreSync Hook 执行

### 需要更新的文档
- [x] `docs/deploy/pitfalls/_index.md` - 分配 D-005 编号
- [x] `docs/deploy/pitfalls/k8s.md` - 添加 D-005 条目

---

## 验收标准

- [ ] PreSync Hook Job 正常执行（无 Error pods）
- [ ] probe 配置正确应用（liveness `/healthz`, readiness `/healthz?full=true`)
- [ ] D-005 避坑指南已记录

---

## 参考案例

- PR#170 PreSync Hook 失败（7个 Error pods）
- copyutil-watchdog 成功案例（使用 openssl + SA token，不含 kubectl）

---

*更新日期: 2026-04-21*