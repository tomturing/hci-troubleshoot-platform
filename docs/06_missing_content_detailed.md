# HCI智能排障平台 - 缺失内容详细清单

## 📊 完成度概览

| 模块 | 完成度 | 说明 |
|------|--------|------|
| 架构设计 | 100% | 完整 |
| 数据库设计 | 100% | 完整 |
| Shared模块 | 100% | 完整 |
| Case Service | 100% | 完全可用 |
| API Gateway | 90% | 基础完成，缺少部分路由 |
| Conversation Service | 40% | 基础框架，缺少核心业务逻辑 |
| Scheduler Service | 30% | 基础框架，缺少K8s集成 |
| 前端 | 10% | 仅有目录结构 |
| 测试 | 60% | 单元测试完成，缺少E2E |
| 部署配置 | 50% | Docker完成，K8s未完成 |

**总体完成度: 约60%**

---

## 🔴 高优先级缺失（阻塞MVP运行）

### 1. Conversation Service - Repository层

**文件**: `backend/conversation-service/app/repositories/conversation_repo.py`

**缺失功能**:
```python
class ConversationRepository:
    async def create(self, conversation: Conversation) -> Conversation:
        """创建对话"""
        pass
    
    async def get_by_id(self, conversation_id: UUID) -> Optional[Conversation]:
        """根据ID查询对话"""
        pass
    
    async def get_by_case_id(self, case_id: str) -> List[Conversation]:
        """根据case_id查询所有对话"""
        pass
    
    async def end_conversation(self, conversation_id: UUID) -> Optional[Conversation]:
        """结束对话"""
        pass
```

**工作量**: 2-3小时

**参考**: 完全参照 `backend/case-service/app/repositories/case_repo.py` 的实现模式

---

**文件**: `backend/conversation-service/app/repositories/message_repo.py`

**缺失功能**:
```python
class MessageRepository:
    async def create(self, message: Message) -> Message:
        """创建消息"""
        pass
    
    async def get_by_conversation_id(
        self, 
        conversation_id: UUID,
        limit: int = 50
    ) -> List[Message]:
        """查询对话的所有消息"""
        pass
    
    async def get_context_messages(
        self,
        conversation_id: UUID,
        limit: int = 10
    ) -> List[Message]:
        """获取最近N条消息作为上下文"""
        pass
```

**工作量**: 2-3小时

---

### 2. Conversation Service - Routes层

**文件**: `backend/conversation-service/app/routes/conversations.py`

**缺失功能**:
```python
@router.post("/api/conversations")
async def create_conversation(case_id: str):
    """创建对话会话"""
    pass

@router.post("/api/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: UUID,
    message: MessageCreate
):
    """发送消息到AI，返回流式响应"""
    pass

@router.get("/api/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: UUID):
    """获取对话历史"""
    pass

@router.put("/api/conversations/{conversation_id}/end")
async def end_conversation(conversation_id: UUID):
    """结束对话"""
    pass
```

**工作量**: 3-4小时

**关键点**: 需要实现与OpenClaw的流式通信集成

---

### 3. Conversation Service - Service层

**文件**: `backend/conversation-service/app/services/conversation_service.py`

**缺失功能**:
```python
class ConversationService:
    async def start_conversation(self, case_id: str) -> ConversationResponse:
        """开始新对话"""
        pass
    
    async def send_message(
        self,
        conversation_id: UUID,
        message: str,
        role: MessageRole = MessageRole.USER
    ) -> AsyncGenerator[str, None]:
        """
        发送消息并流式返回AI响应
        1. 保存用户消息
        2. 获取历史上下文
        3. 调用OpenClaw
        4. 流式返回并保存AI响应
        """
        pass
    
    async def get_conversation_history(
        self,
        conversation_id: UUID
    ) -> List[MessageResponse]:
        """获取对话历史"""
        pass
    
    async def prepare_context(
        self,
        conversation_id: UUID
    ) -> Dict[str, Any]:
        """准备对话上下文（最近10条消息）"""
        pass
```

**工作量**: 4-5小时

---

### 4. API Gateway - Cases代理路由

**文件**: `backend/api-gateway/app/routes/cases.py`

**缺失功能**:
```python
"""
将Case相关的REST API请求代理到Case Service
"""

@router.post("/api/cases")
async def create_case_proxy(request: Request):
    """代理创建工单请求"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.CASE_SERVICE_URL}/api/cases",
            json=await request.json(),
            headers={"X-Trace-ID": request.state.trace_id}
        )
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type="application/json"
        )

@router.get("/api/cases")
@router.get("/api/cases/{case_id}")
@router.put("/api/cases/{case_id}/confirm")
@router.put("/api/cases/{case_id}/close")
async def proxy_to_case_service(...):
    """代理所有Case Service的API"""
    pass
```

**工作量**: 2小时

**说明**: 这是可选的，也可以直接让前端调用Case Service

---

## 🟡 中优先级缺失（影响完整功能）

### 5. Scheduler Service - K8s Client

**文件**: `backend/scheduler-service/app/services/k8s_client.py`

**缺失功能**:
```python
from kubernetes import client, config

class K8sClient:
    def __init__(self):
        config.load_incluster_config()  # 或 load_kube_config()
        self.v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
    
    async def create_pod(self, case_id: str) -> str:
        """创建OpenClaw Pod"""
        pass
    
    async def delete_pod(self, pod_name: str):
        """删除Pod"""
        pass
    
    async def get_pod_status(self, pod_name: str) -> PodStatus:
        """获取Pod状态"""
        pass
    
    async def list_pods(self, label_selector: str) -> List[V1Pod]:
        """列出所有Pod"""
        pass
```

**工作量**: 4-5小时

**参考**: kubernetes-python官方文档

---

### 6. Scheduler Service - 核心调度逻辑

**文件**: `backend/scheduler-service/app/services/scheduler.py`

**缺失功能**:
```python
class PodScheduler:
    def __init__(self):
        self.warm_pool: List[str] = []  # 热备Pod列表
        self.busy_pods: Dict[str, str] = {}  # pod_name -> case_id
    
    async def initialize_warm_pool(self):
        """初始化热备池（启动2-3个Pod）"""
        pass
    
    async def assign_pod(self, case_id: str) -> str:
        """
        为Case分配Pod
        1. 优先从热备池分配
        2. 如果热备池空，创建新Pod
        3. 更新Pod状态
        """
        pass
    
    async def release_pod(self, case_id: str):
        """
        释放Pod
        1. 如果热备池未满，放回热备池
        2. 如果热备池已满，销毁Pod
        """
        pass
    
    async def health_check_loop(self):
        """
        定期健康检查
        1. 检查所有Pod状态
        2. 清理不健康的Pod
        3. 维持热备池大小
        """
        pass
    
    async def cleanup_idle_pods(self):
        """清理闲置超过5分钟的Pod"""
        pass
```

**工作量**: 6-8小时

---

### 7. Scheduler Service - Routes

**文件**: `backend/scheduler-service/app/routes/pods.py`

**缺失功能**:
```python
@router.post("/api/pods/assign")
async def assign_pod(case_id: str):
    """为Case分配Pod"""
    pass

@router.delete("/api/pods/release")
async def release_pod(case_id: str):
    """释放Pod"""
    pass

@router.get("/api/pods/status")
async def get_pool_status():
    """获取Pod池状态"""
    pass
```

**工作量**: 2小时

---

### 8. 前端 - Vue项目基础结构

**需要创建的文件**:

```
frontend/
├── src/
│   ├── main.ts                 # Vue应用入口
│   ├── App.vue                 # 根组件
│   ├── router/index.ts         # 路由配置
│   ├── stores/
│   │   ├── case.ts            # Case Store
│   │   └── websocket.ts       # WebSocket Store
│   ├── services/
│   │   ├── api.ts             # API封装
│   │   └── websocket.ts       # WebSocket客户端
│   ├── types/index.ts         # TypeScript类型定义
│   ├── views/
│   │   ├── Home.vue           # 首页
│   │   ├── CaseList.vue       # 工单列表
│   │   └── Chat.vue           # 聊天页面
│   └── components/
│       ├── CaseCard.vue       # 工单卡片
│       ├── MessageList.vue    # 消息列表
│       └── CommandDisplay.vue # 命令展示
├── package.json
├── tsconfig.json
├── vite.config.ts
└── index.html
```

**工作量**: 8-10小时（基础功能）

**关键点**:
- WebSocket实时通信
- 消息流式显示
- 工单状态管理

---

## 🟢 低优先级缺失（增强功能）

### 9. Kubernetes部署配置

**需要创建的文件**:

```
deploy/k8s/
├── namespace.yaml              # 命名空间
├── configmap.yaml             # 配置
├── secret.yaml                # 密钥
├── postgres.yaml              # PostgreSQL StatefulSet
├── redis.yaml                 # Redis Deployment
├── api-gateway.yaml           # API Gateway Deployment + Service
├── case-service.yaml          # Case Service Deployment + Service
├── conversation-service.yaml  # Conversation Service
├── scheduler-service.yaml     # Scheduler Service
├── openclaw.yaml              # OpenClaw Deployment (动态副本)
└── ingress.yaml               # Ingress配置
```

**工作量**: 4-6小时

**参考**: 已有的Docker Compose配置

---

### 10. OpenClaw镜像和配置

**文件**: `/mnt/d/openclaw/Dockerfile`

**缺失内容**:
```dockerfile
FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制OpenClaw代码
COPY . .

# 配置Zhipu AI
ENV ZHIPU_API_KEY=""
ENV MODEL="glm-4"

EXPOSE 8080

CMD ["python", "main.py"]
```

**工作量**: 2-3小时（需要熟悉OpenClaw项目结构）

---

### 11. 部署脚本

**文件**: `deploy/scripts/build.sh`

```bash
#!/bin/bash
# 构建所有Docker镜像

docker build -t hci-api-gateway:latest backend/api-gateway
docker build -t hci-case-service:latest backend/case-service
docker build -t hci-conversation-service:latest backend/conversation-service
docker build -t hci-scheduler-service:latest backend/scheduler-service
docker build -t openclaw:latest /mnt/d/openclaw
```

**文件**: `deploy/scripts/deploy-dev.sh`

```bash
#!/bin/bash
# 部署到开发环境

kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/secret.yaml
# ... 依次部署所有服务
```

**工作量**: 2小时

---

### 12. E2E测试

**文件**: `tests/e2e/test_complete_workflow.py`

**缺失内容**:
- 完整的端到端测试场景
- 从创建工单到AI对话的完整流程测试
- 前端UI自动化测试（使用Playwright/Selenium）

**工作量**: 6-8小时

---

### 13. 监控和可观测性

**当前状态**: 仅有结构化日志和TraceID

**缺失内容**:

1. **Prometheus指标采集**
   - 文件: `backend/shared/utils/metrics.py`
   - 功能: API请求计数、延迟、错误率等

2. **Grafana Dashboard配置**
   - 文件: `deploy/monitoring/grafana-dashboard.json`
   - 功能: 可视化监控大盘

3. **Jaeger链路追踪**
   - 集成OpenTelemetry
   - 配置Jaeger Collector

4. **Loki日志聚合**
   - 配置日志收集
   - 与Grafana集成

**工作量**: 10-12小时

---

## 📝 文档缺失

### 14. API文档增强

**当前**: 基础API设计文档

**缺失**:
- 详细的请求/响应示例
- 错误码说明
- WebSocket消息协议详细定义
- Postman Collection导出

**工作量**: 3-4小时

---

### 15. 运维文档

**缺失文档**:
- 部署手册（生产环境）
- 故障排查手册
- 性能调优指南
- 备份恢复指南
- 安全加固指南

**工作量**: 8-10小时

---

## 💾 数据库相关

### 16. 数据库迁移脚本

**文件**: `database/migrations/001_initial.sql`

**当前**: 仅有init_schema.sql

**缺失**: Alembic迁移脚本，支持版本管理

**工作量**: 2-3小时

---

### 17. 测试数据生成

**文件**: `database/seeds/test_data.sql`

**缺失**:
- 模拟用户数据
- 模拟工单数据
- 模拟对话历史

**工作量**: 2小时

---

## 🔧 代码质量增强

### 18. 错误处理增强

**当前**: 基础错误处理

**需要增强**:
- 自定义异常类
- 统一错误响应格式
- 错误码体系
- 重试机制

**工作量**: 4-5小时

---

### 19. 输入验证增强

**当前**: 基础Pydantic验证

**需要增强**:
- 更严格的业务规则验证
- 自定义验证器
- 数据清洗

**工作量**: 3-4小时

---

### 20. 日志增强

**当前**: 结构化JSON日志

**需要增强**:
- 敏感信息脱敏
- 日志采样（高流量场景）
- 日志级别动态调整

**工作量**: 2-3小时

---

## 📊 总工作量估算

| 类别 | 预估工时 | 优先级 |
|------|---------|--------|
| Conversation Service完整实现 | 10-12小时 | 🔴 高 |
| API Gateway Routes | 2小时 | 🔴 高 |
| Scheduler Service完整实现 | 12-15小时 | 🟡 中 |
| 前端基础实现 | 8-10小时 | 🟡 中 |
| K8s部署配置 | 4-6小时 | 🟡 中 |
| OpenClaw集成 | 2-3小时 | 🟡 中 |
| 测试完善 | 8-10小时 | 🟢 低 |
| 监控系统 | 10-12小时 | 🟢 低 |
| 文档完善 | 11-14小时 | 🟢 低 |
| 代码质量增强 | 9-12小时 | 🟢 低 |

**总计**: 76-106小时（约10-13个工作日）

---

## 🎯 推荐开发顺序

### 第一周：让系统跑起来

1. **Day 1-2**: Conversation Service (Repository + Service + Routes)
2. **Day 3**: API Gateway Cases代理路由
3. **Day 4**: 前端基础页面（工单列表 + 聊天界面）
4. **Day 5**: 端到端测试和调试

### 第二周：完善功能

5. **Day 6-7**: Scheduler Service K8s集成
6. **Day 8**: OpenClaw容器化和集成
7. **Day 9**: K8s部署配置
8. **Day 10**: 性能测试和优化

### 第三周：生产就绪

9. **Day 11-12**: 监控系统搭建
10. **Day 13**: 文档完善
11. **Day 14**: 安全加固
12. **Day 15**: 部署到生产环境

---

## 📞 需要的外部依赖

1. **OpenClaw项目**
   - 位置: `/mnt/d/openclaw`
   - 需要: API接口文档、配置说明

2. **Zhipu API**
   - 需要: API Key
   - 文档: Zhipu AI官方文档

3. **K8s集群**
   - 需要: 集群访问权限
   - 配置: kubeconfig文件

---

## 🎉 总结

**已完成的核心价值**:
- ✅ 完整的架构设计和技术选型
- ✅ 生产级的基础设施代码
- ✅ 完全可运行的Case Service
- ✅ 可扩展的微服务框架

**待完成的主要工作**:
- 🔴 Conversation Service业务逻辑（10-12小时）
- 🟡 Scheduler Service K8s集成（12-15小时）
- 🟡 前端基础实现（8-10小时）

**MVP最小可用版本**:
只需完成红色高优先级项（约15小时），就可以跑通完整流程：
创建工单 → WebSocket连接 → 发送消息 → AI响应 → 关闭工单

这是一个高质量、架构清晰、易于扩展的MVP基础，后续开发可以快速迭代！
