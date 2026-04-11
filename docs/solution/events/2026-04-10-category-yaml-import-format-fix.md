---
status: active
category: solution
audience: developer
last_updated: 2026-04-10
owner: team
---

# 分类 YAML 导入格式不一致修复方案

## 背景与需求

见 [需求文档](../../requirement/events/2026-04-10-category-yaml-import-format-fix.md)

## 根本原因分析

存在三个独立实现，各自对 YAML 格式做了不同假设：

| 层 | 文件 | 对 YAML 的理解 | level 来源 |
|---|---|---|---|
| **数据文件** | `category_baseline.yaml` | 叶节点扁平表，字段 `id/domain/label/path` | 无 level（path 长度隐含） |
| **ETL 脚本** | `scripts/kbd/seed_categories.py` | **正确理解**基准格式 | `len(path)` 推断 |
| **API 接口** | `category_repo.py::import_from_yaml` | 假定"带 level/name/parent_id 的完整格式" | 期望 YAML 中明确有 `level` |

## 方案比较

| 方案 | 描述 | 优点 | 缺点 | 评分 |
|---|---|---|---|---|
| **A. 在 YAML 加 level 字段** | 每条记录补 `level: 3` | 改动小 | level 与 path 冗余，破坏 SSOT，维护易不一致 | ★★☆☆☆ |
| **B. 仅修复字段映射** | label→name，len(path)→level | 改动极小 | 不处理 L1/中间层，parent_id 仍为 NULL，树结构缺失 | ★★★☆☆ |
| **C. 统一解析逻辑（选中）** | 将 seed_categories.py 的四阶段逻辑提取为 `_parse_baseline_yaml`，API 和脚本共用 | 彻底解决，API=脚本，SSOT 不变 | 需重构 repo 层 | ★★★★★ |

### 方案 C 详细设计

在 `CategoryRepository` 中新增 `_parse_baseline_yaml` 静态方法，实现四阶段解析：

```
Phase 1: 推断 L1 域节点（从 domain 字段去重）
         code 格式：{domain}-L1，level=1，parent_id=NULL

Phase 2: 从 L3/L4 叶节点 path 提取中间层节点
         code 格式：{domain}-L2-{name_hash}，level=path[:i] 长度

Phase 3: 解析叶节点（YAML 中的 198 条）
         id→code, label→name, len(path)→level, path→path_labels

Phase 4: 按 level 排序后两阶段 upsert
         第一阶段：parent_id=NULL 全量 upsert
         第二阶段：通过 path_labels 查找父节点，批量 UPDATE parent_id
```

### 中间层节点 code 格式设计

避免中文 code 含特殊字符：使用 `{domain}-L{level}-{index}` 格式（按首次出现顺序编号），保证幂等性（相同 path 永远生成相同 code）。

实际使用 `path_str` 的 hash 前 8 位作为唯一标识以确保幂等：
```python
intermediate_code = f"{domain}-L{level}-{hashlib.md5(path_str.encode()).hexdigest()[:8]}"
```

## 影响范围

### 受影响的文件

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `backend/kb-service/app/repositories/category_repo.py` | 修改 | 核心修复：新增 `_parse_baseline_yaml`，重构 `import_from_yaml` |
| `backend/kb-service/app/services/category_service.py` | 检查/可能修改 | service 层透传，可能需要调整 |
| `docs/solution/knowledge-base/知识库设计.md` | 更新变更历史 | — |

### API 兼容性

- `POST /api/kb/categories/import` 接口签名不变（`multipart/form-data`，`file` + `dry_run`）
- 响应格式不变
- 输入接受范围扩大（现在正确支持基准 YAML）
- **向下兼容**：如果有人已经上传"旧格式"（含 level/name 字段），也能兼容——`_parse_baseline_yaml` 优先读 `label`，fallback 到 `name`；优先用 `path` 算 level，缺 `path` 时 fallback 到显式 `level` 字段。

## 风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|---|---|---|---|
| 中间节点 code 冲突 | 中 | 低 | 使用 path md5 hash 保证幂等，ON CONFLICT DO UPDATE |
| parent_id 更新遗漏 | 高 | 低 | 单独 Phase 2 扫描，dry_run 时也校验 parent 是否可找到 |
| 与已有 seed 数据冲突 | 中 | 中 | upsert ON CONFLICT code DO UPDATE，幂等安全 |

## 验收标准

- [ ] `dry_run=True` 上传 `category_baseline.yaml` 返回 `success: true`，`errors: []`
- [ ] 实际导入：L1=5，中间节点>=30，叶节点=198
- [ ] 所有非 L1 节点 `parent_id IS NOT NULL`
- [ ] 重复导入幂等（`created=0, skipped=N`）
