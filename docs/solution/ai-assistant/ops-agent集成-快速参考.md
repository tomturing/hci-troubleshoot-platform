# Ops-Agent 集成 - 快速参考

## TL;DR

### 推荐方案

**松耦合集成**，通过 OpenAI-compatible API 适配，最小侵入。

### 关键信息

| 项目 | 内容 |
|------|------|
| **文档位置** | `docs/solution/ai-assistant/ops-agent集成-松耦合方案.md` |
| **预计工期** | 5天 |
| **主要变更** | 新增OA服务端点，HCI-TP端新增适配器 |
| **回滚时间** | <1分钟（禁用OA助手） |

---

## 快速决策

### 为什么松耦合？

| 理由 | 说明 |
|------|------|
| **风险低** | 两个项目独立，5分钟回滚 |
| **改动小** | 适配器层，核心逻辑不变 |
| **可演进** | 各自独立发展 |
| **协议复用** | 用现有的 AIAssistantClient |

---

## 核心架构

```
HCI-TP Conversation Svc
    ↓ (AIAssistantClient)
OA Service (OpenAI-compatible API)
    ↓
OA Agent Core (现有逻辑)
```

---

## 主要变更

### OA端

```
新增：
├── ops_agent/server/
│   ├── main.py          # FastAPI应用
│   ├── openai_compat.py # OpenAI兼容端点
│   └── otel_integration.py # OpenTelemetry集成
└── Dockerfile.ops-server
```

### HCI-TP端

```
修改：
├── backend/conversation-service/app/services/ai_client.py # 新增OpsAgentAssistant
├── backend/conversation-service/app/config.py # 新增OA配置
└── backend/conversation-service/app/main.py # 注册OA助手

新增：
└── deploy/helm/hci-platform/templates/ops-agent-service/
    ├── service.yaml
    ├── deployment.yaml
    └── configmap.yaml
```

---

## 下一步

### 1. 审核文档

阅读完整方案：
```
docs/solution/ai-assistant/ops-agent集成-松耦合方案.md
```

### 2. 如需实施

按文档的**实施时间线**分阶段执行，每阶段验证后再进行下一阶段。

### 3. 先做最小验证

```bash
# 1. 实现OA的OpenAI-compatible端点
# 2. 在本地Docker环境测试端到端集成
# 3. 确认工作正常后再部署到测试环境
```
