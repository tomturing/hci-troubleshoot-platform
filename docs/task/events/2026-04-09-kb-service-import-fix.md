---
status: active
category: task
audience: developer
last_updated: 2026-04-09
owner: team
---

# kb-service 导入功能修复任务

## 关联方案
[方案文档](../../solution/events/2026-04-09-kb-service-import-fix.md)

## 任务清单

### T1: 修复 categories.py 文件上传处理
- **描述**：修改 `/api/kb/categories/import` 端点，正确处理 multipart/form-data
- **文件变更**：`backend/kb-service/app/routes/categories.py`
- **验收标准**：admin-ui 可以成功导入分类 YAML
- **依赖**：无
- **状态**：✅ 已完成

### T2: 更新 .env.example 文档
- **描述**：添加 Docker Compose 和 K3s 两种环境的 token 配置说明
- **文件变更**：`scripts/kbd/.env.example`
- **验收标准**：文档清晰说明如何获取正确的 token
- **依赖**：无
- **状态**：✅ 已完成

### T3: 更新 import_sop.py 用法说明
- **描述**：添加完整的前置操作步骤
- **文件变更**：`scripts/kbd/import_sop.py`
- **验收标准**：文档包含 Docker Compose 和 K3s 两种环境的操作指南
- **依赖**：无
- **状态**：✅ 已完成

### T4: 提交代码并创建 PR
- **描述**：提交修改，通过 CI，创建 PR
- **文件变更**：无
- **验收标准**：PR 创建成功，CI 通过
- **依赖**：T1, T2, T3
- **状态**：进行中

## 文档更新计划
- [x] 创建需求事件文档
- [x] 创建方案事件文档
- [x] 创建任务事件文档