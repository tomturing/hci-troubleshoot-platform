---
status: frozen
category: requirement
audience: developer
date: 2026-04-07
topic: 修复 doc-review-agent workflow 权限配置错误
---

# 需求：修复 doc-review-agent workflow 持续失败

> 创建日期：2026-04-07  
> 触发原因：GitHub Actions 中 `.github/workflows/doc-review-agent.yml` 自 PR #100 合并后全部运行失败（36 次连续失败）

---

## 问题描述

`doc-review-agent` workflow 在每次 push/PR 触发后均报 `failure`，GitHub 提示 "This run likely failed because of a workflow file issue"，且 `jobs` 列表为空（没有任何 job 运行）。

## 根本原因

`permissions:` 字段中包含 `models: read`，这是一个 **GitHub Actions 不支持的权限 scope**，导致 workflow 文件解析失败，无法执行任何 job。

```yaml
# ❌ 错误配置
permissions:
  contents: read
  pull-requests: write
  models: read           # 无效字段，GitHub Actions 不认识此 scope
```

`models: read` 是 GitHub Models（AI Marketplace）的访问权限标识，**不属于 GITHUB_TOKEN 的权限 scope**，实际调用 GitHub Models API 只需要使用 token 发起 HTTP 请求，无需声明该权限。

## 期望结果

删除 `models: read` 后，workflow 能正常解析并在 PR 包含文档变更时执行 AI 内容审查。

## 影响范围

仅影响 `.github/workflows/doc-review-agent.yml`，不涉及任何业务代码。
