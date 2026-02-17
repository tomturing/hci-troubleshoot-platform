# HCI 智能排障平台 - 架构设计文档

## 文档信息
- **版本**: 1.0
- **作者**: Claude
- **日期**: 2026-02-15
- **状态**: MVP阶段

---

## 1. 系统架构总览

### 1.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        HCI Troubleshoot Platform                         │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                              用户层 (User Layer)                         │
├─────────────────────────────────────────────────────────────────────────┤
│  Web Client (Vue 3 + TypeScript)                                        │
│  - Case Management (查询/创建/确认/关闭)                                  │
│  - WebSocket Connection (实时双向通信)                                   │
│  - Message Display & Command Execution                                  │
│  Client ID: 浏览器生成的唯一标识                                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                              WSS (WebSocket Secure)
                              HTTPS (REST API)
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           网关层 (Gateway Layer)                         │
├─────────────────────────────────────────────────────────────────────────┤
│  API Gateway (FastAPI)                                                  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ • TraceID Generator & Propagation (hci-{timestamp}-{random})      │  │
│  │ • Request Router (路由到对应微服务)                                │  │
│  │ • Session Manager (WebSocket连接管理)                             │  │
│  │ • Auth Service (临时用户/认证用户验证)                             │  │
│  │ • Rate Limiter (请求限流)                                         │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│  Port: 8000                                                             │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           服务层 (Service Layer)                         │
├─────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────┐ │
│  │ Case Service    │  │ Conversation    │  │ Scheduler Service       │ │
│  │ (FastAPI)       │  │ Service         │  │ (FastAPI)               │ │
│  │                 │  │ (FastAPI)       │  │                         │ │
│  │ • Case CRUD     │  │ • Message Store │  │ • OpenClaw Pod 管理     │ │
│  │ • Status Mgmt   │  │ • Context Prep  │  │ • 热备池管理             │ │
│  │ • Query by      │  │ • AI Gateway    │  │ • 按需创建/销毁          │ │
│  │   ClientID      │  │ • Stream Handle │  │ • Health Check          │ │
│  │                 │  │                 │  │                         │ │
│  │ Port: 8001      │  │ Port: 8002      │  │ Port: 8003              │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                             AI层 (AI Layer)                              │
├─────────────────────────────────────────────────────────────────────────┤
│  OpenClaw Pod Pool (K8s Deployment)                                     │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │ Warm Pool (热备池): 2-3 PODs                                     │   │
│  │ ┌──────────┐  ┌──────────┐  ┌──────────┐                        │   │
│  │ │ OpenClaw │  │ OpenClaw │  │ OpenClaw │                        │   │
│  │ │ POD-1    │  │ POD-2    │  │ POD-3    │                        │   │
│  │ │ (Idle)   │  │ (Busy)   │  │ (Idle)   │                        │   │
│  │ └──────────┘  └──────────┘  └──────────┘                        │   │
│  │                                                                   │   │
│  │ On-Demand Pool (按需池): 根据负载动态创建                          │   │
│  │ ┌──────────┐  ┌──────────┐                                      │   │
│  │ │ OpenClaw │  │ OpenClaw │  ...                                 │   │
│  │ │ POD-N    │  │ POD-N+1  │                                      │   │
│  │ └──────────┘  └──────────┘                                      │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  每个OpenClaw Pod:                                                       │
│  • 集成 Zhipu AI API (GLM-4.7)                                          │
│  • 独立的对话上下文管理                                                   │
│  • 支持流式响应                                                          │
│  Port: 8080 (内部)                                                      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           数据层 (Data Layer)                            │
├─────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────┐  ┌───────────────────────────────────┐    │
│  │ Redis 7                 │  │ PostgreSQL 15                     │    │
│  │ • Session Store         │  │ • case (工单表)                   │    │
│  │ • WebSocket Connections │  │ • conversation (对话表)           │    │
│  │ • Pod Status Cache      │  │ • message (消息表)                │    │
│  │ • Rate Limit Counter    │  │ • user (用户表)                   │    │
│  │                         │  │ • environment (环境表)            │    │
│  │ Port: 6379              │  │ • session (会话表)                │    │
│  └─────────────────────────┘  │                                   │    │
│                               │ 所有表包含 trace_id 字段           │    │
│                               │ Port: 5432                        │    │
│                               └───────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      可观测性层 (Observability Layer)                    │
├─────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────┐ │
│  │ Structured      │  │ (Future)        │  │ (Future)                │ │
│  │ Logging         │  │ Prometheus      │  │ Grafana                 │ │
│  │ • JSON Format   │  │ • Metrics       │  │ • Dashboards            │ │
│  │ • trace_id      │  │ • Alerts        │  │ • Visualization         │ │
│  │ • Stdout        │  │                 │  │                         │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.2 调用链路

#### 典型对话流程
```
1. 用户发送消息
   Web Client (ClientID: xxx)
   → WebSocket Message
   
2. 网关处理
   API Gateway
   → Generate TraceID: hci-1708012345-a1b2c3
   → Validate Session
   → Route to Conversation Service
   
3. 服务层处理
   Conversation Service (trace_id: hci-1708012345-a1b2c3)
   → Check Case Status (Call Case Service)
   → Query Pod from Scheduler Service
   → Forward Message to OpenClaw Pod
   
4. AI处理
   OpenClaw Pod (case_id: Q20260215001)
   → Load Context from DB
   → Call Zhipu AI API
   → Stream Response
   
5. 响应返回
   OpenClaw Pod
   → Conversation Service (Save to DB with trace_id)
   → API Gateway
   → Web Client (WebSocket Stream)
```

---

## 2. 核心组件设计

### 2.1 API Gateway

**职责**:
- TraceID 生成和透传
- WebSocket 连接管理
- 请求路由
- 认证授权
- 限流保护

**技术栈**:
- FastAPI (WebSocket支持)
- Redis (Session存储)
- Pydantic (数据验证)

**关键接口**:
```
WebSocket:
  /ws/{client_id}         - WebSocket连接入口

REST API:
  GET  /api/cases          - 查询工单列表
  POST /api/cases          - 创建新工单
  GET  /api/cases/{case_id} - 查询工单详情
  PUT  /api/cases/{case_id}/confirm - 确认工单
  PUT  /api/cases/{case_id}/close   - 关闭工单
```

### 2.2 Case Service

**职责**:
- 工单生命周期管理
- 工单状态维护
- 工单查询

**工单状态机**:
```
created → confirmed → in_progress → resolved → closed
           ↓                ↓
        cancelled      cancelled
```

**数据模型**:
```python
Case:
  - case_id: str (Q + YYYYMMDD + 5位序号)
  - client_id: str
  - status: enum
  - title: str
  - description: str
  - created_at: datetime
  - updated_at: datetime
  - closed_at: datetime
  - trace_id: str
```

### 2.3 Conversation Service

**职责**:
- 消息存储和检索
- 对话上下文预处理
- OpenClaw Pod 通信
- 流式响应处理

**消息类型**:
```python
MessageType:
  - user: 用户消息
  - assistant: AI响应
  - system: 系统消息
  - command: 命令建议
```

**数据模型**:
```python
Conversation:
  - conversation_id: uuid
  - case_id: str
  - started_at: datetime
  - ended_at: datetime
  - trace_id: str

Message:
  - message_id: uuid
  - conversation_id: uuid
  - role: enum (user/assistant/system)
  - content: text
  - metadata: jsonb
  - created_at: datetime
  - trace_id: str
```

### 2.4 Scheduler Service

**职责**:
- OpenClaw Pod 生命周期管理
- 热备池维护
- 按需创建和销毁
- 负载均衡

**调度策略**:
```
1. 热备池 (Warm Pool)
   - 保持 2-3 个空闲 Pod
   - 快速响应 (< 1s)
   - 定期健康检查

2. 按需池 (On-Demand Pool)
   - 负载高时创建
   - 闲置5分钟后销毁
   - 最大限制: 10 Pods

3. 分配算法
   - 优先分配热备池
   - 负载均衡 (最少连接数)
   - 故障转移
```

**Pod状态**:
```python
PodStatus:
  - idle: 空闲，等待分配
  - assigned: 已分配给 Case
  - busy: 正在处理请求
  - unhealthy: 健康检查失败
  - terminating: 正在终止
```

### 2.5 OpenClaw Pod

**职责**:
- 接收对话请求
- 调用 Zhipu AI API
- 管理对话上下文
- 返回流式响应

**集成方式**:
```
OpenClaw (位于 /mnt/d/openclaw)
→ 独立 K8s Pod 部署
→ 内部 HTTP API (Port 8080)
→ 集成 Zhipu AI SDK
```

**关键配置**:
```yaml
OpenClaw Pod:
  Resources:
    CPU: 1 core
    Memory: 2Gi
  Environment:
    - ZHIPU_API_KEY
    - MODEL: glm-4
  Health Check:
    Path: /health
    Interval: 10s
    Timeout: 5s
```

---

## 3. 数据流设计

### 3.1 TraceID 生成与传播

**格式**: `hci-{timestamp}-{random}`
- timestamp: Unix时间戳 (秒)
- random: 6位随机字符串 (a-z0-9)

**示例**: `hci-1708012345-a1b2c3`

**传播路径**:
```
API Gateway (生成)
  ↓ HTTP Header: X-Trace-ID
Micro Services (透传)
  ↓ SQL: INSERT ... trace_id = ?
Database (存储)
  ↓ Log: {"trace_id": "...", ...}
Logs (输出)
```

### 3.2 WebSocket 消息协议

**客户端 → 服务端**:
```json
{
  "type": "user_message",
  "case_id": "Q20260215001",
  "content": "虚拟机启动失败",
  "metadata": {
    "timestamp": 1708012345
  }
}
```

**服务端 → 客户端** (流式):
```json
{
  "type": "assistant_message",
  "case_id": "Q20260215001",
  "content": "我来帮您诊断这个问题...",
  "is_complete": false,
  "metadata": {
    "trace_id": "hci-1708012345-a1b2c3"
  }
}
```

**命令建议**:
```json
{
  "type": "command",
  "case_id": "Q20260215001",
  "content": "请执行以下命令检查虚拟机状态",
  "command": "virsh list --all",
  "warning": "请在执行前确认环境安全",
  "metadata": {
    "trace_id": "hci-1708012345-a1b2c3"
  }
}
```

---

## 4. 部署架构

### 4.1 开发环境 (Docker Compose)

```yaml
services:
  - api-gateway
  - case-service
  - conversation-service
  - scheduler-service
  - postgres
  - redis
  - openclaw (模拟Pod)
```

**优点**:
- 快速启动
- 本地调试方便
- 资源占用少

### 4.2 生产环境 (Kubernetes)

```
Namespace: hci-troubleshoot

Deployments:
  - api-gateway (Replicas: 2)
  - case-service (Replicas: 2)
  - conversation-service (Replicas: 3)
  - scheduler-service (Replicas: 2)
  - openclaw (Dynamic Replicas: 2-10)

StatefulSets:
  - postgres (Replicas: 1)
  - redis (Replicas: 1)

Services:
  - LoadBalancer for API Gateway
  - ClusterIP for internal services

Ingress:
  - HTTPS with TLS
  - WebSocket support
```

---

## 5. 可观测性设计

### 5.1 MVP阶段 (当前)

**结构化日志**:
```json
{
  "timestamp": "2026-02-15T10:30:00Z",
  "level": "INFO",
  "trace_id": "hci-1708012345-a1b2c3",
  "service": "conversation-service",
  "event": "message_sent",
  "case_id": "Q20260215001",
  "duration_ms": 125,
  "status": "success"
}
```

**TraceID 索引**:
```sql
-- PostgreSQL
CREATE INDEX idx_case_trace_id ON case(trace_id);
CREATE INDEX idx_conversation_trace_id ON conversation(trace_id);
CREATE INDEX idx_message_trace_id ON message(trace_id);
```

### 5.2 未来迭代

**指标监控** (Prometheus):
- API请求成功率、延迟
- WebSocket连接数
- OpenClaw Pod 使用率
- 数据库性能指标

**可视化** (Grafana):
- 实时大盘
- 告警面板
- 链路追踪

---

## 6. 安全设计

### 6.1 MVP阶段

**临时用户**:
- ClientID 唯一标识
- Session 超时: 24小时
- 无持久化认证

**认证用户** (未来):
- JWT Token
- Refresh Token
- RBAC 权限控制

### 6.2 数据安全

**传输加密**:
- WSS (WebSocket Secure)
- HTTPS

**敏感数据**:
- API Key 环境变量注入
- 数据库密码 K8s Secret

---

## 7. 性能设计

### 7.1 性能目标

| 指标 | 目标值 |
|------|--------|
| API响应时间 | < 100ms (P95) |
| WebSocket延迟 | < 50ms |
| AI首字响应 | < 2s |
| 并发连接数 | 1000+ |
| Pod启动时间 | < 3s (热备), < 10s (按需) |

### 7.2 优化策略

**缓存**:
- Redis 缓存 Case 状态
- Session 全内存存储

**连接池**:
- PostgreSQL: 20 connections/service
- Redis: 10 connections/service

**流式响应**:
- OpenClaw → Conversation Service (Stream)
- Conversation Service → API Gateway (Stream)
- API Gateway → Web Client (WebSocket Stream)

---

## 8. 扩展性设计

### 8.1 水平扩展

**无状态服务**:
- API Gateway
- Case Service
- Conversation Service
- Scheduler Service

**扩展策略**:
- K8s HPA (Horizontal Pod Autoscaler)
- 基于 CPU/Memory 自动扩缩容

### 8.2 垂直扩展

**OpenClaw Pod**:
- 根据负载调整资源配额
- GPU支持 (未来)

---

## 9. 容错设计

### 9.1 服务容错

**健康检查**:
- Liveness Probe
- Readiness Probe

**重试机制**:
- OpenClaw调用失败自动重试 (3次)
- 指数退避

**熔断器**:
- 服务间调用熔断
- 降级策略

### 9.2 数据容错

**数据库**:
- PostgreSQL WAL备份
- 每日全量备份

**Redis**:
- AOF持久化
- 主从复制 (未来)

---

## 10. 技术债务

### 10.1 MVP阶段已知限制

1. **认证系统**: 仅支持临时用户，无完整认证
2. **监控系统**: 仅日志和TraceID，无指标采集
3. **环境信息**: 未实现自动采集
4. **知识库**: 未实现

### 10.2 后续优化方向

1. **性能优化**: 
   - 引入消息队列 (RabbitMQ/Kafka)
   - 增加缓存层
   
2. **功能增强**:
   - 环境信息自动采集
   - 知识库系统
   - 多租户支持
   
3. **可观测性**:
   - 完整的监控体系
   - 分布式追踪 (Jaeger)
   - 日志聚合 (Loki)

---

## 附录

### A. 技术栈版本

| 技术 | 版本 |
|------|------|
| Python | 3.12 |
| FastAPI | 0.109+ |
| PostgreSQL | 15 |
| Redis | 7 |
| Vue | 3.4+ |
| TypeScript | 5.3+ |
| Docker | 24+ |
| Kubernetes | 1.28+ |

### B. 端口分配

| 服务 | 端口 |
|------|------|
| API Gateway | 8000 |
| Case Service | 8001 |
| Conversation Service | 8002 |
| Scheduler Service | 8003 |
| OpenClaw Pod | 8080 |
| PostgreSQL | 5432 |
| Redis | 6379 |

---

*文档版本: 1.0 | 日期: 2026-02-15*
