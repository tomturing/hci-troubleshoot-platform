# ProductionClaw 工具清单 · TOOLS

> 以下工具在本 Pod 中可用。我在排障过程中可以主动调用这些 API 获取知识。
> 所有调用均附带 CASE_ID 和 trace_id，以保证可追溯性。

---

## 一、KB Service API（知识库检索，最常用）

**基础地址**：`${KB_SERVICE_URL}` （通常为 `http://kb-service:8004`）

### 1.1 语义检索（主力工具）
```http
POST /api/kb/search
Authorization: Bearer ${INTERNAL_API_TOKEN}
X-Trace-ID: {trace_id}
X-Case-ID: {CASE_ID}
Content-Type: application/json

{
  "query": "具体的故障描述或症状关键词",
  "top_k": 5,
  "category": "storage|network|vm|cluster|compute"  // 可选，按类型过滤
}

响应：
{
  "chunks": [
    {
      "chunk_id": "uuid",
      "doc_id": "uuid",
      "content": "相关知识片段",
      "score": 0.87,
      "source": "https://support.sangfor.com.cn/...",
      "doc_type": "web_case",
      "category": "storage"
    }
  ],
  "count": 5
}
```

**什么时候调用**：
- Session 启动时（用工单描述作为 query）
- 工程师提供了新的错误信息或症状
- 当前排障方向遇到瓶颈、需要新思路时

### 1.2 SOP 匹配（特殊场景）
```http
POST /api/kb/sop/match
Authorization: Bearer ${INTERNAL_API_TOKEN}
Content-Type: application/json

{
  "query": "故障描述"
}

响应：
{
  "matched": true,
  "sop_title": "HCI 存储集群脑裂处理 SOP",
  "sop_content": "完整 SOP 文档内容...",
  "confidence": 0.91
}
```

**什么时候调用**：Session 启动时自动调用一次

---

## 二、Conversation Service API（对话记录）

**基础地址**：`${CONVERSATION_SERVICE_URL}` （通常为 `http://conversation-service:8002`）

> 注意：对话历史主要由 Conversation Service 管理，我不需要手动调用。
> 以下接口仅在特殊场景使用（如工单被转接，需要读取前序对话）。

### 2.1 获取本工单历史（仅读取自己的）
```http
GET /internal/conversations/by-case/{CASE_ID}
Authorization: Bearer ${INTERNAL_API_TOKEN}
X-Trace-ID: {trace_id}
```

---

## 三、Case Service API（工单信息）

**基础地址**：`${CASE_SERVICE_URL}` （通常为 `http://case-service:8001`）

### 3.1 获取工单详情
```http
GET /internal/cases/{CASE_ID}
Authorization: Bearer ${INTERNAL_API_TOKEN}
X-Trace-ID: {trace_id}

响应：
{
  "case_id": "Q20260305001",
  "title": "...",
  "description": "...",
  "status": "in_progress",
  "assistant_type": "productionclaw",
  "created_at": "..."
}
```

---

## 四、环境变量速查

| 变量名 | 说明 | 来源 |
|---|---|---|
| `CASE_ID` | 本 Pod 服务的工单 ID | Scheduler 注入 |
| `CASE_TITLE` | 工单标题 | Scheduler 注入 |
| `CASE_DESCRIPTION` | 工单初始描述 | Scheduler 注入 |
| `CASE_CREATED_AT` | 工单创建时间 | Scheduler 注入 |
| `POD_NAME` | 本 Pod 名称 | Downward API |
| `KB_SERVICE_URL` | 知识库服务地址 | ConfigMap |
| `CASE_SERVICE_URL` | 工单服务地址 | ConfigMap |
| `CONVERSATION_SERVICE_URL` | 对话服务地址 | ConfigMap |
| `INTERNAL_API_TOKEN` | 内部 API 鉴权 Token | Secret |
| `LEARNINGCLAW_SERVICE_URL` | LearningClaw 服务地址（未来用）| ConfigMap |

---

## 五、我没有的工具（不要尝试调用）

- ❌ Web 浏览器 / 外部网络（安全隔离，拒绝连接）
- ❌ KB Service 写入接口（我只读，LearningClaw 才写）
- ❌ 其他工单的对话接口
- ❌ K8s API（我不管理 Pod）
- ❌ SSH / 直连 HCI 集群（未来有执行沙箱时才会开放）

---

## 六、调用规范

所有 HTTP 调用必须携带：
```http
Authorization: Bearer ${INTERNAL_API_TOKEN}
X-Trace-ID: {从 Conversation Service 透传的 trace_id}
X-Case-ID: {CASE_ID}
X-Source: productionclaw
```

超时设置：
- KB Search：5s（检索要快，不能阻塞对话）
- Case / Conversation 读取：5s
