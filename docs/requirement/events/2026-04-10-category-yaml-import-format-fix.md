---
status: active
category: requirement
audience: all
last_updated: 2026-04-10
owner: team
---

# 分类 YAML 导入格式不一致修复需求

## 背景与问题

### 核心痛点

`POST /api/kb/categories/import` 接口实现与 `category_baseline.yaml` 文件格式不匹配：

| 位置 | 字段期望 | 实际 YAML 字段 | 结果 |
|---|---|---|---|
| `category_repo.py:268` | `cat_data.get("level")` | YAML 无 `level` 字段 | → None，验证失败 |
| `category_repo.py:290` | `cat_data.get("name")` | YAML 使用 `label` | → 空字符串 |
| `category_repo.py:296` | `cat_data.get("path_labels")` | YAML 使用 `path` | → 空列表 |
| `category_repo.py:270` | `level not in {1,2,3,4}` | None not in {1,2,3,4} | → 全部报错 |

原因：`import_from_yaml` 设计时预设了一个**从未存在过的"API 专用格式"**（要求 YAML 中明确包含 `level`、`name`、`parent_id` 字段），但实际只有 `category_baseline.yaml` 这一个文件可用，格式永久不匹配。

同期的 `data-pipeline/kbd/seed_categories.py` 正确处理了基准 YAML，但未被 API 层复用。

### 影响范围

- admin-ui 分类 YAML 导入功能完全不可用（上传即报错）
- 系统上线后无法通过界面更新分类基线，只能手动跑脚本

## 功能需求

1. `import_from_yaml` 兼容 `category_baseline.yaml` 的标准格式（字段：`id/label/domain/path`）
2. 从 `path` 长度推断 `level`（与 `seed_categories.py` 保持一致）
3. 自动生成 L1 域节点（`虚拟机-L1` 等 5 条）
4. 从叶节点 `path` 提取中间层节点（L2 分组节点，约 32 条）
5. 两阶段 upsert 后正确更新 `parent_id` 关系
6. API 导入结果与脚本导入结果完全一致

## 验收标准

- [ ] 上传 `category_baseline.yaml` 到 `POST /api/kb/categories/import?dry_run=true`，返回 `success: true`，`errors: []`
- [ ] 实际导入后 `kb_category` 表包含 L1（5条）+ 中间节点（约 32 条）+ 叶节点（198条）
- [ ] 所有非 L1 节点的 `parent_id` 均非 NULL
- [ ] 幂等：重复导入结果不变，不产生重复记录
- [ ] 已有 `data-pipeline/kbd/seed_categories.py` 导入的数据，再次通过 API 导入不冲突

## 约束条件

- `category_baseline.yaml` 格式不变（真相源，不修改文件）
- 现有 `seed_categories.py` 功能不受影响
- YAML 中无 `level` 字段是正确设计（path 长度隐含层级，不引入冗余字段）
