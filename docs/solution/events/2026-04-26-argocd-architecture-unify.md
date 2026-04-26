---
status: active
category: solution
audience: developer
last_updated: 2026-04-26
owner: devops
---

# ArgoCD App of Apps架构统一方案

## 背景与需求
详见 [需求文档](../requirement/events/2026-04-26-argocd-architecture-unify.md)

## 方案概述 (WHAT)
将staging环境的ArgoCD架构统一为与dev一致的App of Apps模式：
- 创建独立的argocd-root Application（bootstrap/目录）
- 将argocd-rbac.yaml添加到argo-apps/cloud/目录
- 重构argocd-ops.yaml为单职责（不再使用sources多源）

## 详细设计

### 架构变更

**当前staging架构（问题）**：
```
argo-apps/cloud/argocd-ops.yaml（双源配置）
    ├─ 管理 argocd-ops/（运维配件）
    └─ 管理 argo-apps/cloud/（App of Apps）← 循环引用问题
        └─ ❌ 缺少 argocd-rbac.yaml
```

**目标架构（统一）**：
```
bootstrap/argocd-root.yaml（独立目录，避免循环引用）
    └─ 管理 argo-apps/cloud/
        ├─ argocd-rbac.yaml (sync-wave: -1) ← 新增
        ├─ argocd-ops.yaml (sync-wave: 0) ← 重构为单职责
        └─ 其他业务Application
```

### 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `deploy/gitops/bootstrap/argocd-root-cloud.yaml` | 新增 | staging的App of Apps入口 |
| `deploy/gitops/argo-apps/cloud/argocd-rbac.yaml` | 新增 | RBAC Application定义 |
| `deploy/gitops/argo-apps/cloud/argocd-ops.yaml` | 修改 | 移除sources多源，改为单源 |

### 关键技术点

1. **argocd-root配置**：
   ```yaml
   spec:
     source:
       path: deploy/gitops/argo-apps/cloud
       repoURL: https://github.com/tomturing/hci-troubleshoot-platform.git
   ```

2. **argocd-rbac.yaml添加sync-wave**：
   ```yaml
   metadata:
     annotations:
       argocd.argoproj.io/sync-wave: "-1"
   ```

3. **argocd-ops.yaml重构**：
   - 移除 `sources:` 多源配置
   - 改为单源 `source:` 只管理 `argocd-ops/` 目录

## 决策依据 (WHY)

### 方案选择
| 方案 | 优点 | 缺点 | 评分 |
|------|------|------|------|
| **方案A：argocd-root架构（选中）** | 与dev一致、避免循环引用、GitOps完整 | 需新增文件、需手动apply bootstrap | ★★★★☆ |
| 方案B：保持双源架构 | 无需新增文件 | 循环引用、argocd-rbac仍需手动管理 | ★★☆☆☆ |
| 方案C：直接apply argocd-rbac | 最简单 | 不在GitOps范围、不可追溯 | ★☆☆☆☆ |

### 为什么选择方案A？
1. **一致性**：与dev环境架构完全一致，运维知识可复用
2. **避免循环引用**：argocd-root定义在独立目录(bootstrap/)，不管理自己
3. **GitOps完整性**：所有Application由Git管理，变更可追溯

### 为什么不选方案B和C？
- 方案B的sources多源会导致"管理者管理自己"的循环引用问题
- 方案C无法通过GitOps自动同步，违背GitOps原则

### 权衡与妥协
- 需手动apply argocd-root.yaml一次，之后自动管理
- argocd-ops需重构，但变更范围小

## 影响范围

### 受影响的模块
- ArgoCD Application管理（staging集群）
- argocd-repo-server-watchdog RBAC同步

### 需要更新的文档
- [ ] `docs/deploy/argocd-setup.md` - 更新架构说明
- [ ] `README.md` - 更新部署拓扑

## 实施计划
1. 创建argocd-rbac.yaml到cloud目录
2. 创建argocd-root-cloud.yaml到bootstrap目录
3. 修改argocd-ops.yaml移除多源
4. 手动apply argocd-root-cloud.yaml
5. 验证GitOps同步正常

## 风险与缓解
| 集群 | 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|------|---------|
| staging | argocd-ops短暂OutOfSync | 低 | 中 | 快速apply argocd-root后自愈 |

## 测试策略
- 修改argocd-rbac Role配置，观察staging是否自动同步
- 检查sync-wave顺序：argocd-rbac先同步，argocd-ops后同步

## 验收标准
- [ ] argocd-rbac Application由argocd-root管理
- [ ] Git修改argocd-rbac后staging自动同步
- [ ] ArgoCD控制台显示argocd-rbac Source来自Git