---
status: frozen
category: deploy
audience: operator
date: 2026-04-07
topic: 修复 doc-review-agent workflow 权限配置错误
---

# 部署：doc-review-agent workflow 修复上线

> 创建日期：2026-04-07  
> 关联任务：[2026-04-07-修复doc-review-agent权限配置错误](../../../task/events/2026-04-07-修复doc-review-agent权限配置错误.md)

---

## 变更内容

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `.github/workflows/doc-review-agent.yml` | 删除 1 行 | 移除无效权限声明 `models: read` |

## 部署方式

本次变更为 GitHub Actions workflow 配置变更，**无需任何手动部署操作**。

合并 PR 到 `main` 分支后，GitHub Actions 自动使用最新 workflow 文件，下次触发即生效。

## 回滚方式

若合并后出现新问题，直接 revert PR 即可恢复。
