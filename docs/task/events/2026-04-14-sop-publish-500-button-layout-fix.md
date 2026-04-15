---
status: active
category: task
audience: developer
last_updated: 2026-04-14
owner: admin
---

# SOP 发布 500 + 管理台布局修复任务

## 关联方案
[方案事件文档](../solution/events/2026-04-14-sop-publish-500-button-layout-fix.md)

## 任务清单

### T1: 修复 API Gateway SOP 发布代理
- **文件变更**：`backend/api-gateway/app/routes/kb.py`
- **内容**：
  1. `sop_approve_proxy` 使用独立 600s 超时
  2. 捕获 `httpx.TimeoutException`，返回 504 + 可读消息
  3. `response.json()` 加容错处理
- **状态**：待开始

### T2: 修复 kb-service SOP 发布长事务
- **文件变更**：`backend/kb-service/app/routes/admin.py`
- **内容**：
  1. 三段式事务：短事务查询 → 无事务 embedding → 短事务批量更新
  2. 外层 try-except，将未捕获异常转为 HTTPException with readable detail
- **状态**：待开始

### T3: 修复前端过滤栏按钮对齐
- **文件变更**：`frontend/admin/src/views/SopManageView.vue`、`KbdReviewView.vue`
- **内容**：按钮容器改为 flex + nowrap
- **状态**：待开始

### T4: 修复 KBD 操作列宽度
- **文件变更**：`frontend/admin/src/views/KbdReviewView.vue`
- **内容**：`width="220"` 改为 `width="260"`，加 nowrap 样式
- **状态**：待开始
