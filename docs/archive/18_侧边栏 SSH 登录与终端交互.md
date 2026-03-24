# Task 36: 侧边栏 SSH 登录与交互终端页面

## 概述

本文档描述前端 SSH 终端侧边栏的实现细节，以及与后端的接口契约。

## 功能说明

### 用户交互流程

1. 用户点击 Header 中的「终端」按钮，打开右侧抽屉式侧边栏
2. 用户填写 SSH 连接信息（主机、端口、用户名、认证方式）
3. 点击「连接」按钮，建立 SSH 会话
4. 连接成功后，在终端输入区输入命令
5. 命令输出以流式方式显示在终端输出区
6. 用户可复制输出内容或清空输出

### 与命令卡片的联动

- 用户在聊天中收到 AI 返回的命令卡片（Task 35）
- 点击「发送到终端」按钮
- 侧边栏自动打开，命令自动填充到输入框
- 用户可按 Enter 执行或编辑后再执行

## 前端实现

### 组件结构

```
frontend/customer/src/
├── components/
│   ├── TerminalPanel.vue       # 终端面板组件（核心实现）
│   └── ChatWindow.vue          # 聊天窗口（包含侧边栏抽屉）
├── stores/
│   └── chat.ts                 # 状态管理（SSH 连接状态）
└── App.vue                     # 头部入口按钮
```

### 状态管理（chat.ts）

```typescript
// SSH 连接状态
const sshConnectionState = ref<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected')
const sshSessionId = ref<string | null>(null)
const sshErrorMessage = ref('')

// 侧边栏控制
const showTerminalSidebar = ref(false)
const terminalInputCommand = ref('')
```

### TerminalPanel 组件功能

| 功能模块 | 说明 |
|---------|------|
| SSH 登录表单 | 主机、端口、用户名、密码/密钥认证 |
| 连接状态指示 | 未连接/连接中/已连接/错误 |
| 终端输出区 | 按行显示命令和输出，支持自动滚动 |
| 命令输入区 | 支持 Enter 执行、Shift+Enter 换行 |
| 操作按钮 | 复制输出、清空输出、断开连接 |

## 后端接口契约（依赖 Task 37）

### API 列表

| 接口 | 方法 | 描述 |
|------|------|------|
| `/api/terminal/sessions` | POST | 创建 SSH 会话 |
| `/ws/terminal/{session_id}` | WebSocket | 命令执行与输出流 |
| `/api/terminal/sessions/{id}/close` | POST | 关闭会话 |

### 1. 创建 SSH 会话

**请求**
```http
POST /api/terminal/sessions
Content-Type: application/json
X-Client-ID: {client_id}

{
  "host": "192.168.1.100",
  "port": 22,
  "username": "root",
  "password": "secret123",  // 或 private_key
  "private_key": "-----BEGIN RSA PRIVATE KEY-----..."
}
```

**响应**
```json
{
  "session_id": "sess_abc123",
  "status": "connected",
  "message": "SSH 连接成功"
}
```

### 2. WebSocket 命令执行

**连接**
```
WS /ws/terminal/sess_abc123
```

**发送命令**
```json
{
  "type": "command",
  "content": "uname -a"
}
```

**接收输出**
```json
{
  "type": "output",
  "content": "Linux node1 5.4.0-42-generic...",
  "stream": "stdout"
}
```

**命令执行完成**
```json
{
  "type": "exit",
  "code": 0
}
```

### 3. 关闭会话

**请求**
```http
POST /api/terminal/sessions/sess_abc123/close
X-Client-ID: {client_id}
```

**响应**
```json
{
  "status": "closed",
  "message": "会话已关闭"
}
```

## 安全边界

### 前端限制

- ❌ 浏览器端不得直连 SSH（网络和安全限制）
- ❌ 不得在前端存储长期密码/密钥
- ✅ 所有认证信息通过 HTTPS 传输
- ✅ 会话 ID 存储在内存中，页面刷新失效

### 后端要求（Task 37）

- ✅ SSH 密码/密钥在服务端加密存储
- ✅ 会话级凭证，断开后清除
- ✅ WebSocket 连接需要鉴权（X-Client-ID）
- ✅ 命令执行需要审计日志

## 验收标准

### UI 层

- [x] 侧边栏可打开/关闭
- [x] SSH 登录表单可填写
- [x] 连接状态正确显示
- [x] 终端输出区支持复制
- [x] 终端输出区支持清空

### 命令卡片联动

- [x] 「发送到终端」按钮可填充命令到输入框
- [x] 侧边栏自动展开

### 后端依赖

- [ ] Task 37 实现 SSH 代理与会话管理
- [ ] POST /api/terminal/sessions 接口可用
- [ ] WS /ws/terminal/{session_id} 接口可用
- [ ] 命令输出流式返回

## 待办事项

### 前端（本任务完成后）

- [ ] 实际对接后端 WebSocket API
- [ ] 处理连接错误和重试
- [ ] 支持终端复制/粘贴
- [ ] 支持命令历史（上下箭头）

### 后端（Task 37）

- [ ] SSH 连接池管理
- [ ] WebSocket 双向通信
- [ ] 会话超时清理
- [ ] 命令审计日志

## 修改文件清单

| 文件 | 修改内容 |
|------|---------|
| `frontend/customer/src/stores/chat.ts` | 新增 SSH 连接状态管理 |
| `frontend/customer/src/components/TerminalPanel.vue` | 新增 SSH 登录表单、终端输出区 |
| `frontend/customer/src/components/ChatWindow.vue` | 新增侧边栏抽屉 |
| `frontend/customer/src/App.vue` | 新增终端入口按钮 |
