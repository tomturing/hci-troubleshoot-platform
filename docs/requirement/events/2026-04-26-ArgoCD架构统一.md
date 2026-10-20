---
status: active
category: requirement
audience: all
last_updated: 2026-04-26
owner: devops
---

# ArgoCD App of Apps架构统一

## 变更历史
| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-26 | v1.0 | 初版 | 本文档 |

## 背景与问题
- **业务背景**：HCI平台采用多环境部署(dev/staging/prod)，每个环境有独立K3s集群和ArgoCD实例
- **核心痛点**：dev和staging环境的ArgoCD架构不一致，导致运维复杂度增加、配置变更不可追溯
- **影响范围**：所有ArgoCD Application管理，特别是argocd-rbac的GitOps同步

### 问题详情
| 环境 | App of Apps架构 | argocd-rbac管理 |
|------|-----------------|-----------------|
| dev | argocd-root(bootstrap/) → 管理argo-apps/local/ | 自动GitOps同步 |
| staging | argocd-ops双源 → 管理argo-apps/cloud/ | 手动创建，不在GitOps范围！ |

**staging的argocd-rbac Application是手动创建的，不在App of Apps管理范围内**：
- Git中修改argocd-rbac不会触发staging自动更新
- 与dev设计原则违背，架构不一致
- 存在隐藏风险：误删argocd-rbac无法自动恢复

## 需求描述
- **功能概述**：统一staging和dev的ArgoCD架构，确保所有Application由GitOps管理
- **用户场景**：运维人员修改Git中的Application配置，所有环境自动同步
- **预期收益**：降低运维复杂度，提高配置一致性，减少人为错误

## 功能需求
1. 将argocd-rbac.yaml添加到argo-apps/cloud/目录
2. 创建argocd-root.yaml（bootstrap/目录）作为App of Apps入口
3. 重构argocd-ops.yaml为单职责（只管理运维配件）
4. 添加sync-wave annotation确保RBAC先于ops同步

## 验收标准
- [ ] staging环境的argocd-rbac Application由GitOps自动管理
- [ ] argocd-rbac配置变更可通过Git提交自动同步到集群
- [ ] dev和staging的ArgoCD架构文档完全一致
- [ ] ArgoCD sync顺序正确（rbac先于ops）

## 约束条件
- 技术约束：必须遵循ArgoCD官方App of Apps最佳实践
- 资源约束：无额外资源需求
- 依赖约束：依赖现有ArgoCD RBAC配置（argocd-rbac/）

## 风险与假设
- **已知风险**：重构期间argocd-ops可能短暂OutOfSync
- **假设条件**：现有argocd-rbac Application可被GitOps接管