# Admin 路由冲突与 Token 鉴权修复方案

| 字段 | 值 |
|------|------|
| 日期 | 2026-04-07 |
| 需求文档 | [需求](../../requirement/events/2026-04-07-Admin路由冲突与Token鉴权修复需求.md) |
| 影响服务 | kb-service, api-gateway |

## 根因分析

### 问题 1：路由冲突

```
classify.py:    router.get("/categories")    prefix=/api/kb          → GET /api/kb/categories
categories.py:  router.get("")               prefix=/api/kb/categories → GET /api/kb/categories
```

两者注册在**同一路径**。FastAPI 按注册顺序匹配：`classify.router` 在 `main.py` L104，`categories.router` 在 L106 → classify 永远先匹配。

classify.py 的 `GET /categories` 返回 `{"categories": [...], "total": N}`（平铺），前端期望 `{"domains": {...}}`（分组）→ 数据为空。

**全仓库搜索确认该端点零调用者**：
- conversation-service 调的是 `/categories/grouped`（categories.py）
- classify.py 自身 LLM 分类通过 `fetch_categories_for_classify()` 直接查 DB
- 前端期望 domains 格式（categories.py）

### 问题 2：Token 不匹配

前端 `VITE_INTERNAL_API_TOKEN` 是构建时环境变量，admin-ui Dockerfile 未注入 → JS 回退到硬编码 `hci-dev-internal-token`。K8s Secret 实际值为 `dev-internalapi-api-token-2026`。

**架构层面**：浏览器不应持有内部服务 token。网关代理应使用自身凭证调用下游。

## 方案

### 修复 1：删除死代码端点

**删除** classify.py 的 `GET /categories` + 关联模型 `CategoryItem`/`CategoriesResponse` + 无用 import（`Query`, `select`, `KbCategory`）。

选择"删除"而非"重命名"的原因：
- YAGNI — 零调用者，不应保留
- 减少 API 表面积和认知负担
- 保留两个 POST 端点（`/classify` 和 `/classify/intent`）不变

### 修复 2：网关注入 Token

api-gateway 新增 `_internal_auth_headers()` 函数：
- 使用网关自身 `settings.INTERNAL_API_TOKEN` 调用 kb-service
- 防御性校验：token 为空/None 时直接返回 HTTP 500
- categories_router（5 端点）和 kbd_router（3 端点）改用此函数

### 不采纳方案

| 被排除方案 | 原因 |
|-----------|------|
| 调换 main.py 注册顺序 | 两个 handler 仍注册同路径，future regression 风险 |
| 重命名为 `/category-tree` | 保留无人使用的端点，增加维护成本 |
| 加旧路由兼容别名 | 零调用者，兼容无意义 |
| 前端注入正确 token | 架构反模式：浏览器不应持有内部服务 token |

## 影响范围

| 文件 | 变更 |
|------|------|
| `backend/kb-service/app/routes/classify.py` | 删除 GET 端点 + CategoryItem/CategoriesResponse + 无用 import（约 -175 行） |
| `backend/api-gateway/app/routes/kb.py` | 新增 `_internal_auth_headers()`（含防御性校验）；8 处代理改用网关 Token |
| `docs/solution/架构设计.md` | v6.4.3 版本记录 |
