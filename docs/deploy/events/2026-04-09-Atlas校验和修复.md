# Atlas Checksum 格式修复

**日期**: 2026-04-09
**类型**: Bug Fix
**影响范围**: db-migrate Job

## 问题

db-migrate Job 执行 `atlas migrate apply` 时失败：

```
Error: checksum file format invalid
```

## 根因

`database/atlas-migrations/atlas.sum` 文件有多余空行，导致 Atlas 无法解析 checksum。

原文件格式（错误）：
```
h1:Vev7IQRZJG+ZYvLnlmSs6rw8RLJuuaTRbaGCFnlI+KY=

20260408000000_baseline.sql h1:Bs81j4yrgjM7wOyiQfUBhrVvAIeze6QRujMrPg71JcM=
```

## 修复

运行 `atlas migrate hash` 重新生成 checksum 文件，移除多余空行：

```bash
docker run --rm -v ./database/atlas-migrations:/atlas-migrations -e HOME=/tmp \
  --entrypoint="" arigaio/atlas:latest \
  /atlas migrate hash --dir file:///atlas-migrations
```

修复后格式：
```
h1:8n3UWPLXOW8kbv1OpU2MGdXvsSsRklBbPXQ3d5lYSfA=
20260408000000_baseline.sql h1:hZ0f9u2Y3iYAq5MmIwQvq6lkLWO5rxOr8Mld/oyte7k=
```

## 关联 PR

- tomturing/hci-troubleshoot-platform#130