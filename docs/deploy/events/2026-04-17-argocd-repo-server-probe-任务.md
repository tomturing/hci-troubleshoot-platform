---
status: active
category: task
audience: developer
last_updated: 2026-04-17
owner: team
---

# ArgoCD repo-server Probe 优化任务

## 关联方案
[方案文档](../events/2026-04-17-argocd-repo-server-probe-优化.md)

## 任务清单

### T1: 创建 StrategicMergePatch 文件
- **描述**：在 `deploy/gitops/argocd-ops/` 创建 repo-server probe patch YAML
- **文件变更**：新增 `deploy/gitops/argocd-ops/argocd-repo-server-probe-patch.yaml`
- **验收标准**：YAML 格式正确，包含 liveness/readiness probe 配置
- **依赖**：无
- **预计耗时**：10分钟
- **状态**：待开始

### T2: 更新部署设计文档
- **描述**：在 `docs/deploy/部署设计.md` 变更历史节追加本次变更记录
- **文件变更**：修改 `docs/deploy/部署设计.md`
- **验收标准**：变更历史节包含本次变更
- **依赖**：T1
- **预计耗时**：5分钟
- **状态**：待开始

### T3: 提交 PR
- **描述**：提交 hotfix PR，CI 验证通过后合并
- **文件变更**：无
- **验收标准**：PR 合入 main 分支
- **依赖**：T1, T2
- **预计耗时**：10分钟
- **状态**：待开始

### T4: 验证效果
- **描述**：合并后验证 ArgoCD Application 状态
- **文件变更**：无
- **验收标准**：所有 Application Synced + Healthy
- **依赖**：T3
- **预计耗时**：5分钟
- **状态**：待开始

## 任务依赖图

```
T1 → T2 → T3 → T4
```

## 执行顺序
1. T1: 创建 patch 文件
2. T2: 更新文档
3. T3: 提交 PR
4. T4: 验证效果

## 测试计划
- Helm 模板校验：CI 自动执行
- 功能验证：合并后检查 ArgoCD Application 状态

## 变更历史

| 日期 | 状态 | 变更 |
|------|------|------|
| 2026-04-17 | 初版 | 任务规划完成 |