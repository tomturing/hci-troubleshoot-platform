# 分类管理增强：KBD/SOP 统计显示

> 版本: v2.1.0
> 更新时间: 2026-04-19

## 功能概述

分类基线管理页面（CategoryManageView）增强显示已发布 KBD/SOP 的数量统计，帮助管理员直观了解每个分类的知识覆盖情况。

## 功能详情

### 1. 左侧分类列表

- **分类项标签**: 每个分类节点显示 `[SOP:N][KBD:N]` 标签
- **域节点汇总**: L1 域节点显示所有活跃子分类的 SOP/KBD 数量汇总
- **统计栏**: 底部统计栏新增"已发布KBD"总数

### 2. 右侧详情面板

- **标题行布局**: 分类详情 + 状态开关 + 保存按钮（一行展示）
- **基本信息表格**: 4列×2行布局（业务编码、分类名称、所属域、完整路径）
- **已发布 SOP/KBD 列表**: 显示该分类下已发布的条目，包含 `[命中:N]` 标签和"详情"按钮
- **详情弹窗**: 点击"详情"按钮弹出 Markdown 渲染的内容展示

### 3. 后端改动

#### 3.1 分类查询接口增强

- `GET /api/kb/categories`: 返回字段新增 `published_kbd_count` 和 `published_sop_count`
- 使用子查询统计：
  ```sql
  (SELECT COUNT(*) FROM kbd_entry WHERE status = 'published' AND category_id = c.code) AS published_kbd_count,
  (SELECT COUNT(*) FROM sop_document WHERE status = 'published' AND category_id = c.code) AS published_sop_count
  ```

#### 3.2 意图识别过滤优化

- `POST /api/kb/classify/intent`: 只查询 `is_active = TRUE` 的分类节点
- 禁用的分类不再参与意图识别

#### 3.3 新增 KBD 详情接口

- `GET /api/admin/kbd/{kbd_id}`: 返回单条 KBD 完整内容（用于详情弹窗）

### 4. 数据库索引优化

新增部分索引提升分类统计查询性能：
```sql
-- 使用部分索引减少索引体积（仅索引 published 状态）
CREATE INDEX IF NOT EXISTS idx_sop_document_category_published ON sop_document (category_id) WHERE status = 'published';
```

## 前端交互说明

### 状态变更逻辑

- 分类名称不可通过详情面板修改（应通过 YAML 导入统一管理）
- 仅允许修改 `is_active` 状态
- 保存后自动更新统计栏的"启用"和"已发布KBD"数量

### 统计计算规则

- **域节点汇总**: 只统计活跃（is_active=TRUE）子分类的 SOP/KBD 数量
- **已发布KBD总数**: 只统计活跃分类的 KBD 数量总和

## 相关文件

| 文件路径 | 变更类型 | 说明 |
|---------|---------|------|
| `backend/kb-service/app/repositories/category_repo.py` | 修改 | SQL SELECT 添加 created_at 和统计子查询 |
| `backend/kb-service/app/routes/classify.py` | 修改 | 意图识别过滤 is_active=TRUE |
| `backend/kb-service/app/routes/admin.py` | 修改 | 新增 GET /api/admin/kbd/{kbd_id} |
| `frontend/admin/src/views/CategoryManageView.vue` | 修改 | 增加统计显示、详情弹窗、优化编辑逻辑 |
| `database/desired_schema.sql` | 修改 | 新增 sop_document 索引 |