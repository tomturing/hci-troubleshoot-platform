---
status: frozen
category: solution
audience: developer
date: 2026-04-07
topic: 修复 doc-review-agent workflow 权限配置错误
---

# 方案：删除无效 `models: read` 权限声明

> 创建日期：2026-04-07  
> 关联需求：[2026-04-07-修复doc-review-agent权限配置错误](../../../requirement/events/2026-04-07-修复doc-review-agent权限配置错误.md)

---

## 问题分析

GitHub Actions `permissions:` 字段仅支持以下合法 scope：

| Scope | 说明 |
|-------|------|
| `actions` | Actions 本身 |
| `contents` | 仓库内容读写 |
| `deployments` | 部署状态 |
| `id-token` | OIDC token |
| `issues` | Issues 读写 |
| `packages` | 包 registry |
| `pull-requests` | PR 读写 |
| `security-events` | 安全事件 |
| `statuses` | commit 状态 |

`models: read` **不属于上述任何合法值**，GitHub Actions 解析 `permissions:` 时遇到未知字段会直接导致 workflow 文件解析失败，触发 "workflow file issue" 错误，所有 job 不会执行。

## 方案选择

### 方案 A（采用）：删除 `models: read` 行

直接删除无效的权限声明。调用 `https://models.inference.ai.azure.com` 的 GitHub Models API 仅需 HTTP Authorization header 携带 Bearer token，不需要 GITHUB_TOKEN 权限声明中有任何特殊字段。

**优点**：最小改动，符合 OWASP 最小权限原则。  
**缺点**：无。

### 方案 B（放弃）：替换为 `id-token: read`

`id-token` 用于 OIDC 联合认证，与调用 GitHub Models API 无关。添加多余权限违反最小权限原则，放弃。

## 最终修改

```diff
 permissions:
   contents: read
   pull-requests: write   # 写 PR 审查评论
-  models: read           # 调用 GitHub Models API
```

修改后 `permissions` 仅保留实际需要的两项，workflow 解析正常，GitHub Models API 调用逻辑未变。
