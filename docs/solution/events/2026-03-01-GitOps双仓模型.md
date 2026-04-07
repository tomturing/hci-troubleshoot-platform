---
status: accepted
date: 2026-03-01
deciders: team
---

# ADR-002：GitOps 双仓模型设计

## 状态

**已采纳**（2026-03），环境仓库接入进行中

---

## 背景

项目采用 K3s + Helm 部署，已引入 ArgoCD 作为 GitOps 控制器。需要决定：应用代码仓库与部署配置（环境声明）是否放在同一个仓库。

---

## 决策问题

> 应用代码与环境配置（image tag、env values）放同一仓库还是分开两个仓库？

---

## 评估选项

### 选项 A：单仓（应用代码 + 环境配置在一起）

```
hci-troubleshoot-platform/
  deploy/helm/hci-platform/values.yaml  ← 含 image.tag
  deploy/gitops/argo-apps/              ← ArgoCD Applications
```

**优势**：简单，一个 PR 同时包含代码和配置变更
**劣势**：
- 环境声明（image.tag）每次新构建都触发代码仓库 commit，污染 git log
- 无法实现"env 晋级审批"（prod 值和 dev 值在一个 commit 里）
- ArgoCD 直接监听代码仓库，任何代码 push 都触发同步评估，噪声大
- 违背 GitOps "环境状态独立可审计" 原则

### 选项 B：双仓（✅ 已采纳）

```
应用仓库（代码）: hci-troubleshoot-platform
  → CI 构建镜像 → 触发环境仓库 PR

环境仓库（声明）: hci-platform-env
  environments/
    dev/values.yaml     ← CI 自动 PR
    staging/values.yaml ← 人工审批晋级
    prod/values.yaml    ← 发布窗口 + 审批
  → ArgoCD 监听此仓库 → 同步集群
```

**优势**：
- 代码仓库和环境仓库职责清晰，互不干扰
- 环境晋级流程可独立审批控制（PR review）
- 集群状态变更历史与代码变更历史分开，各自清晰可追溯
- 满足"谁、何时、改了什么环境配置"的完整审计链

**劣势**：
- 多一个仓库需要维护 PAT 权限
- 首次构建需配置 `ENV_REPO_PAT` Secret

---

## 决策

**采用双仓模型（选项 B）**。

**兜底机制**：保留 `scripts/k3s-release.sh` 作为极端情况下的应急通道，但日常发布必须走 GitOps 路径。

---

## 实施状态

- [x] Helm Chart 模板已完成（`deploy/helm/hci-platform/`）
- [x] ArgoCD Application 模板已创建（`deploy/gitops/argo-apps/`）
- [x] 环境仓库骨架模板已创建（`deploy/gitops/env-repo-template/`）
- [x] 环境仓库同步工作流已创建（`.github/workflows/env-repo-sync.yml`）
- [ ] 环境仓库 `hci-platform-env` 独立创建并完成接入
- [ ] ArgoCD 从应用仓库切换到环境仓库

---

## 后果

**正面**：
- 每次发布可追溯完整链路（代码 PR + 环境声明 PR + ArgoCD 同步记录）
- 环境晋级流程标准化

**负面/注意事项**：
- 如果 `scripts/k3s-release.sh` 被日常使用，会造成集群状态与环境仓库不一致（配置漂移）
- 维护两个仓库的 PAT 需要严格权限最小化

---

## 相关资源

- 环境仓库骨架：`deploy/gitops/env-repo-template/`
- 同步工作流：`.github/workflows/env-repo-sync.yml`
- 发布手册：`docs/guides/发布手册.md`
