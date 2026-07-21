---
status: active
category: event
audience: developer
date: 2026-07-20
related_prs: ["#576", "#583", "#584"]
related_task: "T-OBS-06"
---

# 任务：terminal_bridge 回采链路修复

> 对应方案文档：[../solution/events/2026-07-20-terminal-bridge回采链路断裂根因分析.md](../solution/events/2026-07-20-terminal-bridge回采链路断裂根因分析.md)

## 任务元信息

| 字段 | 值 |
|------|-----|
| 任务 ID | T-OBS-06 |
| 优先级 | P0 |
| 状态 | 进行中 |
| 负责人 | ZCode |
| 创建日期 | 2026-07-20 |

## 背景

工单 Q2026072055042 暴露 `bridge_execution_logs` 表自建库以来 0 行，回采链路从未成功。根因是 HTTP 上报链路两道关卡断裂：
1. api-gateway 无 `/api/bridge-logs` 代理路由 → 404
2. conversation-service 强制 session 鉴权 → 401（customer 前端无 token 来源）

详细根因见：[根因分析文档](../solution/events/2026-07-20-terminal-bridge回采链路断裂根因分析.md)

## 目标

1. 回采链路端到端打通：terminal_bridge → 前端 → api-gateway → conversation-service → DB
2. 消除静默失败：前端暴露成功/失败计数到 `window.__bridgeLogStats`
3. 测试覆盖：api-gateway + conversation-service + 前端单元测试

## 文件清单

### 新建文件

| # | 文件路径 | 说明 |
|---|---------|------|
| 1 | `backend/api-gateway/app/routes/bridge_logs.py` | 代理路由，转发到 conversation-service，注入占位符 token |
| 2 | `database/data-migrations/006_align_bridge_execution_logs_schema.sql` | 对齐生产表与 desired_schema（类型/索引/DEFAULT）|
| 3 | `backend/api-gateway/tests/unit/test_bridge_logs.py` | api-gateway 单元测试（4 tests）|
| 4 | `backend/conversation-service/tests/unit/test_bridge_logs.py` | conversation-service 单元测试（11 tests）|
| 5 | `frontend/customer/src/stores/__tests__/chat.bridge-logs.spec.ts` | 前端单元测试（5 tests）|

### 修改文件

| # | 文件路径 | 改动说明 |
|---|---------|---------|
| 1 | `backend/api-gateway/app/main.py` | 注册 `bridge_logs.router` |
| 2 | `backend/conversation-service/app/routes/bridge_logs.py` | 鉴权弱化：接受 internal/占位符/JWT，对齐 exec-result 路由 |
| 3 | `frontend/customer/src/stores/chat.ts` | 暴露成功/失败计数到 `window.__bridgeLogStats`（类型化）|
| 4 | `docs/solution/可观测性设计.md` | 新增 §7 terminal_bridge 回采章节 |
| 5 | `docs/task/可观测性任务.md` | 新增 T-OBS-06 任务条目 |

## 实现步骤

### 步骤 1：api-gateway 代理路由

**新建**：`backend/api-gateway/app/routes/bridge_logs.py`

```python
router = APIRouter(prefix="/api/bridge-logs", tags=["bridge-logs"])

@router.post("")
async def proxy_bridge_logs(request: Request):
    payload = await request.json()
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    else:
        headers["Authorization"] = "Bearer client-session-placeholder-token"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{CONVERSATION_SERVICE_URL}/api/bridge-logs",
            json=payload, headers=headers,
        )
        return JSONResponse(content=response.json(), status_code=response.status_code)
```

**修改**：`backend/api-gateway/app/main.py`
- import 块加入 `bridge_logs`
- `app.include_router(bridge_logs.router)`

### 步骤 2：conversation-service 鉴权弱化

**修改**：`backend/conversation-service/app/routes/bridge_logs.py`

将 `_verify_session` 改为 `_check_session_or_internal`：

```python
_PLACEHOLDER_TOKEN = "client-session-placeholder-token"

def _check_session_or_internal(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer Token")
    token = authorization[7:].strip()
    if token == settings.INTERNAL_API_TOKEN:
        return "internal"
    if token == _PLACEHOLDER_TOKEN:
        return "customer"
    if token.count(".") == 2:  # JWT
        payload = _decode_jwt_payload(token)
        uid = payload.get("sub") or payload.get("user_id") or payload.get("user")
        if uid:
            return str(uid)
    raise HTTPException(401, "Token 无效")
```

### 步骤 3：database schema 对齐

**新建**：`database/data-migrations/006_align_bridge_execution_logs_schema.sql`

```sql
-- 类型对齐 desired_schema
ALTER TABLE bridge_execution_logs ALTER COLUMN case_id TYPE varchar(32);
ALTER TABLE bridge_execution_logs ALTER COLUMN case_id DROP NOT NULL;
ALTER TABLE bridge_execution_logs ALTER COLUMN trace_id TYPE varchar(64);
ALTER TABLE bridge_execution_logs ALTER COLUMN custom_ui TYPE varchar(255);
ALTER TABLE bridge_execution_logs ALTER COLUMN user_id TYPE varchar(64);
ALTER TABLE bridge_execution_logs ALTER COLUMN node_ip TYPE varchar(64);
ALTER TABLE bridge_execution_logs ALTER COLUMN level TYPE varchar(16);
ALTER TABLE bridge_execution_logs ALTER COLUMN event TYPE varchar(64);
ALTER TABLE bridge_execution_logs ALTER COLUMN level SET DEFAULT 'INFO';

-- 索引对齐 desired_schema 命名
DROP INDEX IF EXISTS idx_bridge_logs_case_id;
DROP INDEX IF EXISTS idx_bridge_logs_trace_id;
DROP INDEX IF EXISTS idx_bridge_logs_custom_ui;
DROP INDEX IF EXISTS idx_bridge_logs_created_at;
DROP INDEX IF EXISTS idx_bridge_logs_event;

CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_case_time ON bridge_execution_logs (case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_trace ON bridge_execution_logs (trace_id);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_custom_ui ON bridge_execution_logs (custom_ui);
```

### 步骤 4：前端可观测性自检

**修改**：`frontend/customer/src/stores/chat.ts`

在 `flushBridgeLogs` 成功/失败分支暴露计数：

```typescript
interface BridgeLogStats {
  successes?: number
  ingested?: number
  lastSuccessAt?: string
  failures?: number
  dropped?: number
  lastError?: string
  lastErrorAt?: string
}

function getBridgeLogStats(): BridgeLogStats { ... }
function setBridgeLogStats(stats: BridgeLogStats): void { ... }

// 成功时
setBridgeLogStats({
  ...getBridgeLogStats(),
  successes: (getBridgeLogStats().successes || 0) + 1,
  ingested: (getBridgeLogStats().ingested || 0) + batch.length,
  lastSuccessAt: new Date().toISOString(),
})

// 失败时
setBridgeLogStats({
  ...getBridgeLogStats(),
  failures: (getBridgeLogStats().failures || 0) + 1,
  dropped: bridgeLogRetryCount > BRIDGE_LOG_MAX_RETRY ? batch.length : 0,
  lastError: String(e).slice(0, 200),
  lastErrorAt: new Date().toISOString(),
})
```

### 步骤 5：测试覆盖

**新建**：`backend/api-gateway/tests/unit/test_bridge_logs.py`
- `test_proxy_bridge_logs_forwards_payload`
- `test_proxy_bridge_logs_injects_placeholder_token`
- `test_proxy_bridge_logs_forwards_existing_auth`
- `test_proxy_bridge_logs_upstream_error_passthrough`

**新建**：`backend/conversation-service/tests/unit/test_bridge_logs.py`
- 8 个鉴权测试（accepts internal/placeholder/JWT，rejects missing/invalid/...）
- 3 个落库测试（skips no-case_id，inserts valid，503 when DB not ready）

**新建**：`frontend/customer/src/stores/__tests__/chat.bridge-logs.spec.ts`
- `test_flushBridgeLogs_posts_to_bridge_logs_endpoint`
- `test_forwardBridgeLog_drops_entries_without_case_id`
- `test_flushBridgeLogs_success_exposes_stats`
- `test_flushBridgeLogs_retries_on_failure`
- `test_flushBridgeLogs_drops_after_max_retry`

## 验收标准

1. `POST /api/bridge-logs` 经 api-gateway 返回 202（不再 404）
2. conversation-service 接受占位符/internal token（不再 401）
3. 端到端：terminal_bridge 发 bridge_log → 前端 flush → DB `bridge_execution_logs` 有行
4. migration 006 幂等可执行，执行后表结构与 desired_schema 一致
5. api-gateway + conversation-service + frontend 单元测试全部通过
6. `ruff check` / `frontend build` / `frontend test` 无报错
7. 文档归档完成，现行全量文档同步

## 不改动项

- **不回滚 PR#583**（对 exec 链路有效）
- **不改 terminal_bridge main.go**（发送侧已正确）
- **不改前端 apiClient 拦截器**（customer 无 token 来源，靠网关兜底是既有架构决策）
- **不改 exec-result 等其他路由**（超出本次范围）
- **不改 Deployment 配置**（流传输错误的 Deployment 修复留待独立任务）

## 关联文档

- [根因分析](../solution/events/2026-07-20-terminal-bridge回采链路断裂根因分析.md)
- [可观测性设计](../可观测性设计.md)
- [terminal_bridge 设计](../events/2026-07-20-terminal-bridge可观测性与日志回采重设计.md)