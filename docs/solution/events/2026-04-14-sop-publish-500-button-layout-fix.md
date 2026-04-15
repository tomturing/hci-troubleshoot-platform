---
status: active
category: solution
audience: developer
last_updated: 2026-04-14
owner: admin
---

# SOP 发布 500 + 管理台布局修复方案

## 背景与需求
见 [需求事件文档](../requirement/events/2026-04-14-sop-publish-500-button-layout-fix.md)

## 根因分析

### 问题1：SOP 发布显示 "HTTP 500"（非具体原因）
**根因链路**：
1. `approve_sop_document`（kb-service）在单个 `async_session` 中对 N 个 chunk 串行调用外部 embedding 服务
2. 194 个 chunk × 每次 ~300ms = 约 58s，恰好触发 API Gateway `httpx.AsyncClient(timeout=60.0)` 超时
3. `httpx.TimeoutException` 是 `TimeoutException` 的子类，**不是** `RequestError` 子类，现有 `except httpx.RequestError` 无法捕获
4. 未捕获异常由 Starlette 的 `ServerErrorMiddleware` 处理，返回 **plain text** `"Internal Server Error"`
5. 前端 `resp.json().catch(() => ({}))` 解析失败，`err = {}`，`detail = undefined`，回退到 `HTTP ${resp.status}` = "HTTP 500"

### 问题2：按钮不同高
`el-col :span="4"` + 两个内联 `el-button` ——当相邻 `el-select`/`el-input` 因窗口变化或浏览器缩放而变高时，`el-row` 整体行高增加，但 `el-col` 内没有明确 flex 对齐，导致两个按钮保持 top 对齐而不是 middle 对齐。

### 问题3：KBD 操作列按钮换行
固定 `width="220"` 对于 4 个 `size="small" text` 按钮在高字体/缩放比下不足，按钮换行。

## 方案设计

### 方案A（选中）：最小化改动 + 可靠性修复

**后端 kb-service** (`backend/kb-service/app/routes/admin.py`)：
1. 将整个 `approve_sop_document` 函数体包裹在 `try-except Exception as exc` 中，将所有未捕获异常转为 `HTTPException(status_code=500, detail=str(exc))`
2. 重构事务：**三段式**：
   - 短事务1：查询 document + chunks（验证）
   - 无事务：遍历 chunks 生成 embedding（长耗时操作）
   - 短事务2：批量更新 chunks + document 状态

**后端 API Gateway** (`backend/api-gateway/app/routes/kb.py`)：
1. SOP 发布代理使用独立超时 `timeout=600.0`
2. 在 `_sop_proxy` 中额外捕获 `httpx.TimeoutException`，返回 504
3. `sop_approve_proxy` 中对 `response.json()` 加 `.catch()` 容错

**前端** (`frontend/admin/src/views/SopManageView.vue`, `KbdReviewView.vue`)：
1. 过滤栏按钮列加 `display: flex; gap: 8px; align-items: center; flex-wrap: nowrap`
2. KBD 操作列 `width` 从 220 改为 260，加 `class` 设置 `white-space: nowrap`

### 为什么不选方案B（彻底重构用 Celery/任务队列）？
- 引入 Celery + Redis Worker 需要新增基础设施，改动过大
- MVP 阶段优先保证功能可用

## 影响范围
- `backend/api-gateway/app/routes/kb.py`
- `backend/kb-service/app/routes/admin.py`
- `frontend/admin/src/views/SopManageView.vue`
- `frontend/admin/src/views/KbdReviewView.vue`

## 验收标准
- [ ] SOP 发布失败时显示具体错误文本（非 "HTTP 500"）
- [ ] 按钮在缩放范围 80%-150% 内始终同行同高
- [ ] KBD 4 按钮在缩放范围内不换行
