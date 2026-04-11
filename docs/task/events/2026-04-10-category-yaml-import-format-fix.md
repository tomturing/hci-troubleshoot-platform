---
status: active
category: task
audience: developer
last_updated: 2026-04-10
owner: team
---

# 分类 YAML 导入修复任务

## 关联方案
[分类 YAML 导入格式修复方案](../../solution/events/2026-04-10-category-yaml-import-format-fix.md)

## 任务清单

### T1: 新增 `_parse_baseline_yaml` 静态方法
- **描述**：在 `CategoryRepository` 中提取解析逻辑，实现四阶段解析（L1 域节点 → 中间层节点 → 叶节点 → 排序）。与 `seed_categories.py` 逻辑等价，但返回 SQLAlchemy upsert 友好的 dict 列表。
- **文件变更**：`backend/kb-service/app/repositories/category_repo.py`
- **验收标准**：调用 `CategoryRepository._parse_baseline_yaml(categories_data)` 返回按 level 排序的记录列表，包含 L1 和中间节点
- **依赖**：无
- **状态**：待开始

### T2: 重构 `import_from_yaml` 主流程
- **描述**：替换现有错误的验证逻辑（`level=cat_data.get("level")`），改为调用 `_parse_baseline_yaml` 解析，然后执行两阶段 upsert（先 parent_id=NULL，再批量 UPDATE parent_id）
- **文件变更**：`backend/kb-service/app/repositories/category_repo.py`
- **验收标准**：`import_from_yaml(content, dry_run=True)` 对基准 YAML 返回 `success=True, errors=[]`
- **依赖**：T1
- **状态**：待开始

### T3: 检查并更新 category_service.py
- **描述**：service 层的 `import_from_yaml` 透传调用可能需要调整 total 计数（now 包括 L1 + 中间节点 + 叶节点）
- **文件变更**：`backend/kb-service/app/services/category_service.py`
- **验收标准**：service 层正常透传，返回 total 包含所有节点数
- **依赖**：T1, T2
- **状态**：待开始

### T4: 更新知识库设计文档
- **描述**：在 `docs/solution/knowledge-base/知识库设计.md` 变更历史追加本次修复条目
- **文件变更**：`docs/solution/knowledge-base/知识库设计.md`
- **验收标准**：文档变更历史有 v3.8 条目
- **依赖**：T2
- **状态**：待开始

## 任务依赖图

```
T1 → T2 → T3 → T4
```

## 文档更新计划

- [x] `docs/requirement/events/2026-04-10-category-yaml-import-format-fix.md`（已创建）
- [x] `docs/solution/events/2026-04-10-category-yaml-import-format-fix.md`（已创建）
- [ ] `docs/solution/knowledge-base/知识库设计.md` — 变更历史 v3.8
