# Admin 路由冲突与 Token 鉴权修复任务

| 字段 | 值 |
|------|------|
| 日期 | 2026-04-07 |
| 分支 | `hotfix/admin-route-token-fix-v2` |
| 方案文档 | [方案](../../solution/events/2026-04-07-Admin路由冲突与Token鉴权修复方案.md) |

## 任务清单

### T1: 删除 classify.py 死代码端点

- [x] 删除 `GET /categories` handler 函数及路由装饰器
- [x] 删除 `CategoryItem` Pydantic model
- [x] 删除 `CategoriesResponse` Pydantic model
- [x] 清理不再使用的 import（`Query`, `select`, `KbCategory`）
- [x] 保留 `POST /classify` 和 `POST /classify/intent` 不变
- [x] 验证文件从 704 行降为 529 行

### T2: api-gateway Token 注入

- [x] 新增 `_internal_auth_headers()` 函数
- [x] 防御性校验：token 为空/None 时返回 HTTP 500（`"内部配置错误：INTERNAL_API_TOKEN 未设置"`）
- [x] `categories_router` 5 个端点改用 `_internal_auth_headers()`
- [x] `kbd_router` 3 个端点改用 `_internal_auth_headers()`
- [x] 保留 `_forward_headers(request)` 用于 `/api/v1/kb/*` 路由

### T3: 文档

- [x] 需求文档 `docs/requirement/events/`
- [x] 方案文档 `docs/solution/events/`
- [x] 任务文档 `docs/task/events/`（本文档）
- [ ] 部署文档 `docs/deploy/events/`
- [ ] 验证文档 `docs/verify/events/`
- [ ] 更新 `docs/solution/架构设计.md` v6.4.3 条目

## 完成标准

1. `GET /api/kb/categories` 返回 `{"domains": {...}}` 格式
2. KBD 审核页 `GET /api/admin/kbd/entries` 返回 200
3. classify.py 仅保留两个 POST 路由
4. CI 全绿（ruff lint + docs-governance + 单测）
