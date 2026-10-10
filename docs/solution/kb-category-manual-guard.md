# 分类树手动入口护栏（create / delete / reparent）

> 关联：PR#1129（分类 code 形态契约收口与漂移自愈）的后续补强。
> 背景：#1129 只覆盖了**导入侧**校验（yaml 导入 + seed）+ S0 消费侧放行告警，
> 但**手动侧** `category_repo.create / delete / update_parent_recursive` 仍存在漂移入口：
> `create` 可手输任意不合规 code；`delete`/`reparent` 不检查源父退化（中间层被删空成隐形叶子）。

## 设计原则（第一性原理 + 对抗性审查）

- **入口硬阻止**：`create` 一旦手输不合规 code 立即拒绝，不让错误数据进入库。
- **退化软警示**：`delete`/`reparent` 不阻断合法操作（删叶子、拖拽合法），但在源父退化时
  记录 `repo_category_parent_degenerated` 告警事件，交由巡检 `verify_kb_category_tree.py` 的 E1 捕获。
- **不硬阻止退化**：直接硬阻止删除/拖拽会妨碍有依赖的合法操作；软警示 + 巡检闭环更稳。

## 契约来源

`shared/utils/category_code.py` 的 `is_leaf_code(code)`（正则 `^[一-鿿A-Za-z0-9-]+-\d+$`，
叶子或中间层 code 均符合「前缀-纯数字」结尾）。手动 `create` 的 `code` 非 None 时必须满足该契约。

## 改动点

| 方法 | 行为 |
|---|---|
| `create` | `code` 非 None 且 `not is_leaf_code(code)` → `ValueError` 拒绝创建 |
| `delete` | 删除后若原父节点变为「无子的中间层」（`not is_leaf_code(parent.code)` 且无子）→ `logger.warning(event="repo_category_parent_degenerated")` |
| `update_parent_recursive` | 移动后若原父（非新父）变为「无子的中间层」→ 同上告警事件 |

## 测试

`backend/kb-service/tests/test_categories.py`：
- `test_create_rejects_nonconforming_code` / `test_create_accepts_valid_leaf_code`
- `test_delete_emits_parent_degeneration_warning` / `test_update_parent_recursive_emits_parent_degeneration_warning`

## 注意（持久化）

手动扁平化/改 DB 不持久：下次基线重导会按 `category_baseline.yaml` 的 `path` 重新派生中间层并还原。
要永久改变单枝结构，必须改 yaml 的 `path` 源头，而非仅改 DB。
