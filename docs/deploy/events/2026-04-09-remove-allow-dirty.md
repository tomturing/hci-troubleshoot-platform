# 移除 --allow-dirty 标志

**日期**: 2026-04-09
**类型**: Bug Fix
**影响范围**: db-migrate Job

## 问题

db-migrate Job 执行失败：
```
Error: sql/migrate: baseline and allow-dirty are mutually exclusive
```

## 根因

atlas migrate apply 不支持同时使用 `--baseline` 和 `--allow-dirty` 标志。

## 修复

移除 `--allow-dirty` 标志。对于已有 DB（使用 `--baseline`），atlas 会自动检测已有表并跳过。

## 关联 PR

- tomturing/hci-troubleshoot-platform#131