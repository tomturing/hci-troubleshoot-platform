# LearningClaw 工具清单 · TOOLS

> 以下工具和 API 均已在环境中可用，直接调用，无需额外授权。

---

## 一、Web 浏览工具（Web MCP）

用于访问 Sangfor 案例库和其他在线文档。

**案例库入口**：
```
https://support.sangfor.com.cn/cases/list?product_id=33&type=1&category_id=36402
```

**使用方式**：直接使用 Web MCP 工具访问 URL，读取页面内容

**注意事项**：
- 每次请求超时 60s
- 遇到验证码或登录拦截，停止并记录到 memory，等待人工处理
- 不要在案例库网站上做任何写操作

---

## 二、KB Service API（知识库服务）

**基础地址**：`${KB_SERVICE_URL}` （环境变量，通常为 `http://kb-service:8004`）

### 2.1 写入知识（摄入）
```http
POST /api/kb/ingest
Authorization: Bearer ${INTERNAL_API_TOKEN}
X-Trace-ID: {trace_id}
Content-Type: application/json

{
  "title": "故障标题",
  "category": "storage|network|compute|vm|cluster|other",
  "doc_type": "web_case | sop | production_case | extracted",
  "source_url": "https://...",           // 网页案例来源
  "source_case_id": "Q2026XXXXXXXXX",   // 工单来源（二选一）
  "content": "结构化的 Markdown 内容",
  "metadata": {
    "product_version": "HCI 6.x",
    "severity": "high|medium|low",
    "resolved": true,
    "tags": ["虚拟机", "存储", "启动失败"]
  }
}

响应：
{ "doc_id": "uuid", "chunk_count": 3, "status": "ok" }
```

### 2.2 搜索验证（去重检查）
```http
POST /api/kb/search
Authorization: Bearer ${INTERNAL_API_TOKEN}
Content-Type: application/json

{
  "query": "待摄入内容的摘要",
  "top_k": 3,
  "threshold": 0.95
}

响应：
{ "chunks": [...], "count": 2 }
// 如果 count > 0 且 score > 0.95，说明已存在相似内容，跳过摄入
```

### 2.3 更新知识
```http
PUT /api/kb/documents/{doc_id}
Authorization: Bearer ${INTERNAL_API_TOKEN}
Content-Type: application/json

{ "content": "更新后的内容", "metadata": {...} }
```

### 2.4 知识库统计
```http
GET /api/kb/stats
Authorization: Bearer ${INTERNAL_API_TOKEN}

响应：
{
  "total_documents": 7234,
  "total_chunks": 28916,
  "by_category": { "storage": 1200, "vm": 2100, ... },
  "by_doc_type": { "web_case": 7000, "sop": 10, "production_case": 224 }
}
```

---

## 三、Case Service API（工单服务）

**基础地址**：`${CASE_SERVICE_URL}` （通常为 `http://case-service:8001`）

### 3.1 获取已关闭工单列表（仅用于提炼知识）
```http
GET /internal/cases?status=resolved&limit=50&after_id={last_processed_id}
Authorization: Bearer ${INTERNAL_API_TOKEN}

响应：
{ "cases": [{ "case_id": "Q2026...", "title": "...", "status": "resolved" }] }
```

### 3.2 获取工单详情
```http
GET /internal/cases/{case_id}
Authorization: Bearer ${INTERNAL_API_TOKEN}
```

---

## 四、Conversation Service API（对话服务）

**基础地址**：`${CONVERSATION_SERVICE_URL}` （通常为 `http://conversation-service:8002`）

### 4.1 获取工单完整对话历史
```http
GET /internal/conversations/by-case/{case_id}
Authorization: Bearer ${INTERNAL_API_TOKEN}

响应：
{
  "conversation_id": "uuid",
  "messages": [
    { "role": "user", "content": "...", "created_at": "..." },
    { "role": "assistant", "content": "...", "created_at": "..." }
  ]
}
```

---

## 五、环境变量速查

| 变量名 | 说明 | 示例 |
|---|---|---|
| `KB_SERVICE_URL` | 知识库服务地址 | `http://kb-service:8004` |
| `CASE_SERVICE_URL` | 工单服务地址 | `http://case-service:8001` |
| `CONVERSATION_SERVICE_URL` | 对话服务地址 | `http://conversation-service:8002` |
| `INTERNAL_API_TOKEN` | 内部 API 鉴权 Token | （Secret 注入）|
| `LEARNINGCLAW_MODE` | 运行模式 | `batch` / `event` / `manual` |
| `TRIGGER_CASE_ID` | 触发案例 ID（event 模式）| `Q20260305XXXXX` |
| `MANUAL_TASK` | 手动任务说明（manual 模式）| 自由文本 |
| `WEB_MCP_URL` | Web MCP 服务地址 | `http://web-mcp:3100` |
