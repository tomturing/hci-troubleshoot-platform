# HCI 智能排障平台 - API 接口设计文档

## 文档信息
- **版本**: 1.0
- **作者**: Claude
- **日期**: 2026-02-15
- **协议**: REST API + WebSocket

---

## 1. 接口概述

### 1.1 Base URL

```
开发环境: http://localhost:8000
生产环境: https://api.hci-troubleshoot.example.com
```

### 1.2 通用约定

#### 请求头

```http
Content-Type: application/json
X-Client-ID: <client_id>              # 客户端唯一标识
X-Trace-ID: <trace_id>                # 可选，由客户端提供或网关生成
Authorization: Bearer <token>         # 未来认证功能
```

#### 响应格式

**成功响应**:
```json
{
  "code": 0,
  "message": "success",
  "data": { ... },
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

**错误响应**:
```json
{
  "code": 4001,
  "message": "Case not found",
  "error": "The requested case Q20260215001 does not exist",
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

#### 错误码

| 错误码 | 说明 |
|--------|------|
| 0 | 成功 |
| 1000 | 系统内部错误 |
| 4001 | 资源不存在 |
| 4002 | 请求参数错误 |
| 4003 | 客户端未识别 |
| 4004 | 工单状态不允许操作 |
| 4005 | 会话已过期 |
| 5001 | 数据库错误 |
| 5002 | AI服务不可用 |
| 5003 | Pod调度失败 |

#### 分页参数

```
page: 页码，从1开始
page_size: 每页数量，默认20，最大100
```

#### 分页响应

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [...],
    "total": 100,
    "page": 1,
    "page_size": 20,
    "total_pages": 5
  },
  "trace_id": "...",
  "timestamp": 1708012345
}
```

---

## 2. Case Management API

### 2.1 查询工单列表

**接口**: `GET /api/v1/cases`

**描述**: 根据ClientID查询工单列表

**请求参数**:
```
Query Parameters:
  status: string, optional - 工单状态筛选
  page: integer, default=1
  page_size: integer, default=20
```

**请求示例**:
```bash
curl -X GET "http://localhost:8000/api/v1/cases?status=in_progress&page=1&page_size=10" \
  -H "X-Client-ID: client-abc123"
```

**响应示例**:
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "case_id": "Q20260215001",
        "title": "虚拟机启动失败",
        "description": "虚拟机 vm-web-01 无法启动",
        "status": "in_progress",
        "priority": "high",
        "category": "vm",
        "created_at": "2026-02-15T10:30:00Z",
        "updated_at": "2026-02-15T11:00:00Z",
        "message_count": 12,
        "last_message_at": "2026-02-15T11:00:00Z"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 10,
    "total_pages": 1
  },
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

### 2.2 查询工单详情

**接口**: `GET /api/v1/cases/{case_id}`

**描述**: 获取工单详细信息

**路径参数**:
```
case_id: string, required - 工单ID
```

**请求示例**:
```bash
curl -X GET "http://localhost:8000/api/v1/cases/Q20260215001" \
  -H "X-Client-ID: client-abc123"
```

**响应示例**:
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "case_id": "Q20260215001",
    "client_id": "client-abc123",
    "title": "虚拟机启动失败",
    "description": "虚拟机 vm-web-01 无法启动，报错 libvirt error",
    "status": "in_progress",
    "priority": "high",
    "category": "vm",
    "metadata": {},
    "created_at": "2026-02-15T10:30:00Z",
    "updated_at": "2026-02-15T11:00:00Z",
    "confirmed_at": "2026-02-15T10:31:00Z",
    "resolved_at": null,
    "closed_at": null,
    "conversations": [
      {
        "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
        "started_at": "2026-02-15T10:31:00Z",
        "ended_at": null,
        "message_count": 12
      }
    ]
  },
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

### 2.3 创建工单

**接口**: `POST /api/v1/cases`

**描述**: 创建新的排障工单

**请求体**:
```json
{
  "title": "虚拟机启动失败",
  "description": "虚拟机 vm-web-01 无法启动，报错 libvirt error",
  "priority": "high",
  "category": "vm",
  "metadata": {}
}
```

**请求示例**:
```bash
curl -X POST "http://localhost:8000/api/v1/cases" \
  -H "Content-Type: application/json" \
  -H "X-Client-ID: client-abc123" \
  -d '{
    "title": "虚拟机启动失败",
    "description": "虚拟机 vm-web-01 无法启动",
    "priority": "high",
    "category": "vm"
  }'
```

**响应示例**:
```json
{
  "code": 0,
  "message": "Case created successfully",
  "data": {
    "case_id": "Q20260215001",
    "title": "虚拟机启动失败",
    "description": "虚拟机 vm-web-01 无法启动",
    "status": "created",
    "priority": "high",
    "category": "vm",
    "created_at": "2026-02-15T10:30:00Z"
  },
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

### 2.4 确认工单

**接口**: `PUT /api/v1/cases/{case_id}/confirm`

**描述**: 确认工单，进入排障流程

**路径参数**:
```
case_id: string, required
```

**请求示例**:
```bash
curl -X PUT "http://localhost:8000/api/v1/cases/Q20260215001/confirm" \
  -H "X-Client-ID: client-abc123"
```

**响应示例**:
```json
{
  "code": 0,
  "message": "Case confirmed",
  "data": {
    "case_id": "Q20260215001",
    "status": "confirmed",
    "confirmed_at": "2026-02-15T10:31:00Z"
  },
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

### 2.5 关闭工单

**接口**: `PUT /api/v1/cases/{case_id}/close`

**描述**: 关闭工单

**路径参数**:
```
case_id: string, required
```

**请求体**:
```json
{
  "reason": "问题已解决",
  "resolution": "重启虚拟机后恢复正常"
}
```

**请求示例**:
```bash
curl -X PUT "http://localhost:8000/api/v1/cases/Q20260215001/close" \
  -H "Content-Type: application/json" \
  -H "X-Client-ID: client-abc123" \
  -d '{
    "reason": "问题已解决",
    "resolution": "重启虚拟机后恢复正常"
  }'
```

**响应示例**:
```json
{
  "code": 0,
  "message": "Case closed",
  "data": {
    "case_id": "Q20260215001",
    "status": "closed",
    "closed_at": "2026-02-15T12:00:00Z"
  },
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

---

## 3. Conversation API

### 3.1 查询对话历史

**接口**: `GET /api/v1/cases/{case_id}/messages`

**描述**: 获取工单的对话消息历史

**路径参数**:
```
case_id: string, required
```

**Query参数**:
```
conversation_id: uuid, optional - 指定会话ID
page: integer, default=1
page_size: integer, default=50
```

**请求示例**:
```bash
curl -X GET "http://localhost:8000/api/v1/cases/Q20260215001/messages?page=1&page_size=50" \
  -H "X-Client-ID: client-abc123"
```

**响应示例**:
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "message_id": "123e4567-e89b-12d3-a456-426614174000",
        "role": "user",
        "content": "虚拟机 vm-web-01 无法启动",
        "command": null,
        "command_warning": null,
        "created_at": "2026-02-15T10:32:00Z",
        "trace_id": "hci-1708012345-a1b2c3"
      },
      {
        "message_id": "123e4567-e89b-12d3-a456-426614174001",
        "role": "assistant",
        "content": "我来帮您诊断这个问题。首先，让我们检查虚拟机的状态...",
        "command": null,
        "command_warning": null,
        "created_at": "2026-02-15T10:32:05Z",
        "trace_id": "hci-1708012345-a1b2c3"
      },
      {
        "message_id": "123e4567-e89b-12d3-a456-426614174002",
        "role": "command",
        "content": "请执行以下命令检查虚拟机状态：",
        "command": "virsh list --all",
        "command_warning": "请确认您有足够的权限执行此命令",
        "created_at": "2026-02-15T10:32:10Z",
        "trace_id": "hci-1708012345-a1b2c3"
      }
    ],
    "total": 12,
    "page": 1,
    "page_size": 50,
    "total_pages": 1
  },
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

---

## 4. WebSocket API

### 4.1 连接建立

**接口**: `WS /ws/{client_id}`

**描述**: 建立WebSocket连接进行实时对话

**路径参数**:
```
client_id: string, required - 客户端唯一标识
```

**Query参数**:
```
case_id: string, optional - 绑定到特定工单
```

**连接示例**:
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/client-abc123?case_id=Q20260215001');

ws.onopen = () => {
  console.log('WebSocket connected');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Received:', data);
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('WebSocket closed');
};
```

### 4.2 消息协议

#### 4.2.1 系统消息 (服务端 → 客户端)

**连接成功**:
```json
{
  "type": "system",
  "event": "connected",
  "data": {
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "case_id": "Q20260215001",
    "message": "WebSocket connected successfully"
  },
  "timestamp": 1708012345
}
```

**工单状态变更**:
```json
{
  "type": "system",
  "event": "case_status_changed",
  "data": {
    "case_id": "Q20260215001",
    "old_status": "created",
    "new_status": "confirmed",
    "message": "Case confirmed and ready for troubleshooting"
  },
  "timestamp": 1708012345
}
```

**错误消息**:
```json
{
  "type": "system",
  "event": "error",
  "data": {
    "code": 5002,
    "message": "AI service temporarily unavailable",
    "error": "Please try again later"
  },
  "timestamp": 1708012345
}
```

#### 4.2.2 用户消息 (客户端 → 服务端)

**发送消息**:
```json
{
  "type": "user_message",
  "case_id": "Q20260215001",
  "content": "执行命令后看到虚拟机状态是 shut off",
  "metadata": {
    "client_version": "1.0.0"
  }
}
```

**发送示例**:
```javascript
ws.send(JSON.stringify({
  type: 'user_message',
  case_id: 'Q20260215001',
  content: '执行命令后看到虚拟机状态是 shut off'
}));
```

#### 4.2.3 AI响应 (服务端 → 客户端)

**流式响应开始**:
```json
{
  "type": "assistant_message",
  "case_id": "Q20260215001",
  "message_id": "123e4567-e89b-12d3-a456-426614174003",
  "content": "好的，我看到",
  "is_streaming": true,
  "is_complete": false,
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012345
}
```

**流式响应中**:
```json
{
  "type": "assistant_message",
  "case_id": "Q20260215001",
  "message_id": "123e4567-e89b-12d3-a456-426614174003",
  "content": "虚拟机处于 shut off 状态。",
  "is_streaming": true,
  "is_complete": false,
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012346
}
```

**流式响应结束**:
```json
{
  "type": "assistant_message",
  "case_id": "Q20260215001",
  "message_id": "123e4567-e89b-12d3-a456-426614174003",
  "content": "让我们尝试启动它。",
  "is_streaming": false,
  "is_complete": true,
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012347
}
```

#### 4.2.4 命令建议 (服务端 → 客户端)

```json
{
  "type": "command",
  "case_id": "Q20260215001",
  "message_id": "123e4567-e89b-12d3-a456-426614174004",
  "content": "请执行以下命令启动虚拟机：",
  "command": "virsh start vm-web-01",
  "command_warning": "请确认虚拟机配置正确后再启动",
  "command_type": "shell",
  "is_dangerous": false,
  "trace_id": "hci-1708012345-a1b2c3",
  "timestamp": 1708012348
}
```

#### 4.2.5 心跳 (双向)

**客户端 → 服务端**:
```json
{
  "type": "ping",
  "timestamp": 1708012349
}
```

**服务端 → 客户端**:
```json
{
  "type": "pong",
  "timestamp": 1708012349
}
```

---

## 5. Environment API (未来功能)

### 5.1 提交环境信息

**接口**: `POST /api/v1/cases/{case_id}/environment`

**描述**: 提交客户环境信息

**请求体**:
```json
{
  "env_type": "system",
  "env_data": {
    "os": "CentOS 7.9",
    "kernel": "3.10.0-1160.el7.x86_64",
    "cpu_cores": 16,
    "memory_gb": 64
  }
}
```

### 5.2 查询环境信息

**接口**: `GET /api/v1/cases/{case_id}/environment`

**描述**: 查询工单关联的环境信息

---

## 6. Session API (内部)

### 6.1 创建会话

**接口**: `POST /internal/sessions`

**描述**: 创建新的WebSocket会话 (内部API)

### 6.2 查询会话状态

**接口**: `GET /internal/sessions/{session_id}`

**描述**: 查询会话状态 (内部API)

---

## 7. Health Check API

### 7.1 健康检查

**接口**: `GET /health`

**描述**: 服务健康检查

**响应示例**:
```json
{
  "status": "healthy",
  "service": "api-gateway",
  "version": "1.0.0",
  "timestamp": 1708012345,
  "checks": {
    "database": "ok",
    "redis": "ok",
    "case_service": "ok",
    "conversation_service": "ok",
    "scheduler_service": "ok"
  }
}
```

### 7.2 就绪检查

**接口**: `GET /ready`

**描述**: 服务就绪检查 (K8s readiness probe)

**响应**:
```json
{
  "ready": true
}
```

### 7.3 存活检查

**接口**: `GET /alive`

**描述**: 服务存活检查 (K8s liveness probe)

**响应**:
```json
{
  "alive": true
}
```

---

## 8. 接口调用流程示例

### 8.1 完整排障流程

```javascript
// 1. 查询是否有未完成的工单
const response1 = await fetch('http://localhost:8000/api/v1/cases', {
  headers: {
    'X-Client-ID': 'client-abc123'
  }
});
const { data: cases } = await response1.json();

let caseId;
if (cases.items.length === 0) {
  // 2. 没有工单，创建新工单
  const response2 = await fetch('http://localhost:8000/api/v1/cases', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Client-ID': 'client-abc123'
    },
    body: JSON.stringify({
      title: '虚拟机启动失败',
      description: '虚拟机 vm-web-01 无法启动',
      priority: 'high',
      category: 'vm'
    })
  });
  const { data: newCase } = await response2.json();
  caseId = newCase.case_id;
  
  // 3. 确认工单
  await fetch(`http://localhost:8000/api/v1/cases/${caseId}/confirm`, {
    method: 'PUT',
    headers: {
      'X-Client-ID': 'client-abc123'
    }
  });
} else {
  caseId = cases.items[0].case_id;
}

// 4. 建立WebSocket连接
const ws = new WebSocket(`ws://localhost:8000/ws/client-abc123?case_id=${caseId}`);

ws.onopen = () => {
  console.log('Connected');
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  
  switch (message.type) {
    case 'system':
      console.log('System:', message.data.message);
      break;
      
    case 'assistant_message':
      console.log('AI:', message.content);
      if (message.is_complete) {
        console.log('Message complete');
      }
      break;
      
    case 'command':
      console.log('Command:', message.command);
      console.log('Warning:', message.command_warning);
      break;
  }
};

// 5. 发送消息
ws.send(JSON.stringify({
  type: 'user_message',
  case_id: caseId,
  content: '虚拟机 vm-web-01 无法启动，报错 libvirt error'
}));

// 6. 关闭工单 (问题解决后)
setTimeout(async () => {
  await fetch(`http://localhost:8000/api/v1/cases/${caseId}/close`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      'X-Client-ID': 'client-abc123'
    },
    body: JSON.stringify({
      reason: '问题已解决',
      resolution: '重启虚拟机后恢复正常'
    })
  });
  
  ws.close();
}, 60000);
```

---

## 9. 接口测试

### 9.1 Postman Collection

```json
{
  "info": {
    "name": "HCI Troubleshoot Platform API",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "item": [
    {
      "name": "Create Case",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Content-Type",
            "value": "application/json"
          },
          {
            "key": "X-Client-ID",
            "value": "{{client_id}}"
          }
        ],
        "url": {
          "raw": "{{base_url}}/api/v1/cases",
          "host": ["{{base_url}}"],
          "path": ["api", "v1", "cases"]
        },
        "body": {
          "mode": "raw",
          "raw": "{\n  \"title\": \"虚拟机启动失败\",\n  \"description\": \"测试工单\",\n  \"priority\": \"high\",\n  \"category\": \"vm\"\n}"
        }
      }
    }
  ]
}
```

### 9.2 curl 测试脚本

```bash
#!/bin/bash

BASE_URL="http://localhost:8000"
CLIENT_ID="client-test-001"

# 1. 创建工单
echo "Creating case..."
CASE_RESPONSE=$(curl -s -X POST "$BASE_URL/api/v1/cases" \
  -H "Content-Type: application/json" \
  -H "X-Client-ID: $CLIENT_ID" \
  -d '{
    "title": "测试工单",
    "description": "这是一个测试工单",
    "priority": "medium",
    "category": "vm"
  }')

CASE_ID=$(echo $CASE_RESPONSE | jq -r '.data.case_id')
echo "Case created: $CASE_ID"

# 2. 确认工单
echo "Confirming case..."
curl -s -X PUT "$BASE_URL/api/v1/cases/$CASE_ID/confirm" \
  -H "X-Client-ID: $CLIENT_ID"

# 3. 查询工单
echo "Getting case details..."
curl -s -X GET "$BASE_URL/api/v1/cases/$CASE_ID" \
  -H "X-Client-ID: $CLIENT_ID" | jq

# 4. 关闭工单
echo "Closing case..."
curl -s -X PUT "$BASE_URL/api/v1/cases/$CASE_ID/close" \
  -H "Content-Type: application/json" \
  -H "X-Client-ID: $CLIENT_ID" \
  -d '{
    "reason": "测试完成",
    "resolution": "测试成功"
  }' | jq
```

---

## 10. 接口变更日志

### v1.0 (2026-02-15)
- 初始版本发布
- 实现 Case CRUD API
- 实现 WebSocket 实时通信
- 实现消息查询 API

---

*文档版本: 1.0 | 日期: 2026-02-15*
