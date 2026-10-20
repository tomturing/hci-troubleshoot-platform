---
status: frozen
category: deploy
audience: developer
last_updated: 2026-04-09
owner: team
---

# Atlas HOME 环境变量修复

## 问题

db-migrate Job 失败：
```
failed to create modcache index dir: mkdir /nonexistent: read-only file system
```

## 根因

atlas 运行时需要写入缓存目录，默认使用 `$HOME`。容器以 `nobody` 用户运行时，`$HOME` 为 `/nonexistent`（不存在且只读）。

## 修复

| 文件 | 变更 |
|------|------|
| `deploy/helm/hci-platform/templates/hooks/db-migrate-job.yaml` | 添加 `HOME: /tmp` 环境变量 |

## 关联 PR

- PR #129
