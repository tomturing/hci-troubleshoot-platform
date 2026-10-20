---
status: frozen
category: deploy
audience: developer
last_updated: 2026-04-09
owner: team
---

# Atlas 二进制路径修复

## 问题

db-migrate Job 启动失败：
```
exec: "atlas": executable file not found in $PATH
```

## 根因

atlas 二进制位于 `/atlas`（镜像 Entrypoint），但 Helm 模板配置 `command: ["atlas"]` 使用相对路径。

## 修复

| 文件 | 变更 |
|------|------|
| `deploy/helm/hci-platform/templates/hooks/db-migrate-job.yaml` | `command: ["atlas"]` → `command: ["/atlas"]` |

## 关联 PR

- PR #128
