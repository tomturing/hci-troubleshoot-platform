---
status: active
category: task
audience: developer
last_updated: 2026-04-26
owner: devops
---

# ArgoCD架构统一任务清单

## 关联方案
详见 [方案文档](../solution/events/2026-04-26-argocd-architecture-unify.md)

## 任务清单

### T1: 创建argocd-rbac.yaml到cloud目录
- **描述**：将argocd-rbac Application定义添加到argo-apps/cloud/目录，确保staging环境GitOps管理
- **文件变更**：
  - 新增 `deploy/gitops/argo-apps/cloud/argocd-rbac.yaml`
- **验收标准**：
  - 文件存在且内容正确（sync-wave: -1）
  - 与local/argocd-rbac.yaml结构一致
- **依赖**：无
- **预计耗时**：15分钟
- **状态**：待开始

### T2: 创建argocd-root-cloud.yaml到bootstrap目录
- **描述**：创建staging环境的App of Apps入口Application，定义在独立目录避免循环引用
- **文件变更**：
  - 新增 `deploy/gitops/bootstrap/argocd-root-cloud.yaml`
- **验收标准**：
  - 文件存在且source.path指向argo-apps/cloud
  - 包含正确的syncPolicy配置
- **依赖**：无
- **预计耗时**：15分钟
- **状态**：待开始

### T3: 重构argocd-ops.yaml为单职责
- **描述**：移除sources多源配置，改为单源只管理argocd-ops/目录，添加sync-wave: 0
- **文件变更**：
  - 修改 `deploy/gitops/argo-apps/cloud/argocd-ops.yaml`
- **验收标准**：
  - 使用单源source（非sources多源）
  - 添加sync-wave: "0" annotation
  - 移除App of Apps职责（由argocd-root接管）
- **依赖**：T1（argocd-rbac必须先就绪）
- **预计耗时**：20分钟
- **状态**：待开始

### T4: 提交代码并推送到远程
- **描述**：将所有变更提交到Git，推送到远程仓库
- **文件变更**：无新增，提交已有变更
- **验收标准**：
  - Commit消息使用中文
  - 追加环境标识
  - 推送成功
- **依赖**：T1, T2, T3
- **预计耗时**：10分钟
- **状态**：待开始

### T5: 手动apply argocd-root-cloud.yaml
- **描述**：在staging集群手动apply bootstrap文件，触发GitOps管理
- **文件变更**：无
- **验收标准**：
  - argocd-root Application创建成功
  - 自动发现并管理cloud目录下的所有Application
- **依赖**：T4（代码必须先推送）
- **预计耗时**：10分钟
- **状态**：待开始

### T6: 验证GitOps同步正常
- **描述**：修改argocd-rbac Role配置，验证staging自动同步
- **文件变更**：
  - 临时修改 `deploy/gitops/argocd-rbac/argocd-repo-server-watchdog-rbac.yaml`
- **验收标准**：
  - ArgoCD检测到变更并自动同步
  - Role配置更新成功
- **依赖**：T5
- **预计耗时**：15分钟
- **状态**：待开始

### T7: 更新文档
- **描述**：更新架构文档和README，反映新的ArgoCD架构
- **文件变更**：
  - 更新 `docs/deploy/argocd-setup.md`（如有）
  - 更新 `README.md`
- **验收标准**：
  - 文档描述与实际架构一致
- **依赖**：T6
- **预计耗时**：15分钟
- **状态**：待开始

## 任务依赖图
```
T1 ──┬── T3 ──┬── T5 ── T6 ── T7
     │        │
T2 ──┴── T4 ──┘
```

## 执行顺序建议
1. 第一批（可并行）：T1, T2
2. 第二批：T3
3. 第三批：T4
4. 第四批：T5
5. 第五批：T6
6. 第六批：T7

## 文档更新计划
按照文档管理规范，以下文档需要在任务完成后同步更新：
- [ ] `docs/deploy/argocd-setup.md` - 更新ArgoCD架构说明（如有）
- [ ] `README.md` - 更新部署拓扑描述

## 测试计划
- GitOps同步测试：修改argocd-rbac Role，观察自动同步
- sync-wave顺序验证：检查ArgoCD同步日志