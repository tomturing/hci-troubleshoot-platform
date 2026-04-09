---
status: frozen
category: deploy
audience: developer
last_updated: 2026-04-09
owner: team
---

# db-migrate 镜像构建修复

## 部署概述

修复 db-migrate 镜像构建失败问题，使 ArgoCD PreSync Hook 能够正常执行数据库迁移。

## 问题

CI 构建 db-migrate 镜像失败：
```
ERROR: ghcr.io/ariga/atlas:latest: not found
```

## 根因

`Dockerfile.migrations` 使用的基础镜像 `ghcr.io/ariga/atlas:latest` 不存在。

Atlas 官方镜像位于 Docker Hub：`arigaio/atlas:latest`

## 修复内容

| 文件 | 变更 |
|------|------|
| `Dockerfile.migrations` | `FROM ghcr.io/ariga/atlas:latest` → `FROM arigaio/atlas:latest` |
| `deploy/helm/hci-platform/values.yaml` | 镜像地址 `ghcr.io/sangfor-hci/...` → `ghcr.io/tomturing/...` |

## 关联 PR

- PR #127: fix(db): 修复 db-migrate 镜像构建失败

## 后续步骤

1. 合并 PR #127
2. CI 构建并推送 db-migrate 镜像
3. 触发 ArgoCD 同步，执行数据库迁移
4. 验证数据库状态（17 张业务表）