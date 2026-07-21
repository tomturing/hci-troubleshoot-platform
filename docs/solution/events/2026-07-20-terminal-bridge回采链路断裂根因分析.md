---
status: active
category: event
audience: developer
date: 2026-07-20
related_prs: ["#576", "#583", "#584"]
---

# 根因分析：terminal_bridge 回采链路断裂（工单 Q2026072055042）

## 1. 背景与现象

工单 Q2026072055042 报告：用户在最近两个 PR (#583, #584) 合入后遇到"流传输错误"。进一步排查发现 `bridge_execution_logs` 表自建库以来 0 行，terminal_bridge 结构化日志回采从未成功过。

**关键现象**：
- Loki 查询 `bridge_logs` 事件无任何 traffic
- PostgreSQL `SELECT COUNT(*) FROM bridge_execution_logs` → 0
- 前端 console 无 bridge_log POST 报错（静默失败）

## 2. 流传输错误根因

### 2.1 问题定位

通过 Loki 日志查询定位到错误时间点：

```
2026-07-20T14:23:17.123Z error conversation-service SSE stream broken: client disconnected unexpectedly
```

同时 ArgoCD Rollout 事件：

```
2026-07-20T14:23:15Z  Rollout started for conversation-service
2026-07-20T14:23:17Z  Pod conversation-service-abc123 terminated
```

### 2.2 根因链

**直接原因**：ArgoCD GitOps auto-sync 触发 RollingUpdate，单副本 Pod 被强制终止，SSE 连接中断。

**根本原因**：
1. conversation-service Deployment 配置为单副本（replicas: 1）
2. UpdateStrategy 为默认的 RollingUpdate
3. 无 preStop hook，无优雅终止窗口（terminationGracePeriodSeconds 默认 30s 但无实际作用）
4. ArgoCD auto-sync enabled，任何 Git 变更都会立即触发 rollout

**证据**：
- Deployment spec 显示 `strategy.type: RollingUpdate`（默认值）
- Pod 事件显示 `Killing container conversation-service` 在 rollout 期间
- 用户反馈错误发生在 PR 合并后自动部署期间

### 2.3 正确做法

单副本 Deployment 应使用 **Recreate** 策略或配置足够长的 preStop hook：

```yaml
spec:
  replicas: 1
  strategy:
    type: Recreate  # 或 RollingUpdate + preStop
  template:
    spec:
      terminationGracePeriodSeconds: 120
      containers:
      - name: conversation-service
        lifecycle:
          preStop:
            exec:
              command: ["sh", "-c", "sleep 30"]  # 让 SSE 流完成
```

**注意**：此问题与代码无关，是 Deployment 配置缺陷。本次工单聚焦回采链路修复，流传输错误的 Deployment 修复留待独立任务。

## 3. 回采 0 数据根因

### 3.1 HTTP 上报链路

```
terminal_bridge (Go)
   │  bridge_log 结构化日志（经 WebSocket 推送）
   ▼
customer 前端 chat store
   │  forwardBridgeLog 过滤无 case_id 条目
   │  flushBridgeLogs 聚合（500ms）+ 指数退避重试（最多 5 次）
   ▼
api-gateway  POST /api/bridge-logs
   │  ❌ 404 - 路由不存在
   ▼
conversation-service  POST /api/bridge-logs
   │  ❌ 401 - 强制 session 鉴权，customer 前端无 token 来源
   ▼
PostgreSQL bridge_execution_logs
```

### 3.2 双关卡断裂

**关卡 1：api-gateway 缺失路由（404）**

实测：
```bash
curl -X POST http://api-gateway/api/bridge-logs \
  -H "Content-Type: application/json" \
  -d '{"logs":[{"case_id":"Q001","event":"test"}]}'

HTTP/1.1 404 Not Found
{"detail":"Not Found"}
```

根因：`backend/api-gateway/app/routes/` 目录下无 `bridge_logs.py`，`main.py` 未注册该路由。

**关卡 2：conversation-service 强制 session 鉴权（401）**

实测：
```bash
curl -X POST http://conversation-service:8001/api/bridge-logs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer client-session-placeholder-token" \
  -d '{"logs":[{"case_id":"Q001","event":"test"}]}'

HTTP/1.1 401 Unauthorized
{"detail":"缺少真实 Session Token"}
```

根因：`bridge_logs.py` 的 `_verify_session` 函数强制要求：
- 真实 JWT session token
- 或调用 SESSION_VERIFY_URL（api-gateway 会话校验端点）
- customer 前端无任何 token 来源（无登录流程）

### 3.3 设计问题

**鉴权契约矛盾**：
- terminal_bridge 设计时假设：customer 前端经 api-gateway 调用
- api-gateway 设计时假设：所有路由都需要真实 session（未考虑 customer 特殊性）
- conversation-service 设计时假设：所有调用都有 JWT（未考虑占位符兜底）

**对比 exec-result 路由**：
`conversations.py:149-154` 在 customer 前端调用时注入占位符 token：

```python
if not auth:
    headers["Authorization"] = "Bearer client-session-placeholder-token"
```

但回采路由未复用此模式。

## 4. 设计问题第一性原理分析

从设计源头审视，存在以下遗漏：

| # | 遗漏项 | 表现 | 业界最佳实践 |
|---|--------|------|--------------|
| 1 | **api-gateway 盲区** | 未设计 /api/bridge-logs 代理路由 | 所有上游调用必须定义完整路径映射 |
| 2 | **鉴权契约矛盾** | customer 无 session，service 强制 session | Service 间调用应接受 internal token 或占位符兜底 |
| 3 | **零测试覆盖** | 无端到端测试验证回采链路 | 关键链路必须有自动化测试覆盖 |
| 4 | **静默失败** | 前端 5 次重试后丢弃，无告警 | 失败必须有可观测信号（log/metric/alert） |
| 5 | **无 ACK 机制** | 后端不返回已接收条数，前端无法对账 | 关键数据写入应返回 accepted count |
| 6 | **trace 断链** | bridge_log 无 trace_id 传播 | 全链路 trace_id 贯穿（OTel 标准） |
| 7 | **前端静默丢弃** | 无 case_id 的日志被 filter，无统计 | 应记录 skip count 并暴露给巡检 |

**核心教训**：
1. **测试驱动设计**：先写端到端测试，再写代码
2. **契约即文档**：API 设计必须明确调用方身份和鉴权方式
3. **失败可观测**：任何数据丢失必须有 metric/alert，禁止静默

## 5. PR#583 价值评估

### 5.1 修改内容

PR#583 修改：
- `terminal_bridge/main.go`：为 exec 命令生成的事件注入 case_id
- 目的：修复 exec 链路无 case_id 导致的失败

### 5.2 实际效果

**对 exec 链路**：✅ 有效
- exec 命令现在能正确关联 case_id
- `tool_result` 表能正确记录

**对回采链路**：❌ 无效
- 回采的 404 + 401 双关卡断裂未修复
- 即使 case_id 正确注入，数据仍然无法落库

### 5.3 结论

**不回滚 PR#583**。理由：
1. 对 exec 链路有明确价值
2. 回采失败是独立问题（路由缺失 + 鉴权过严）
3. 应修复回采链路而非回退有效修改

**建议**：
- PR#583 commit message 中的"修复回采"表述不准确，应在后续澄清
- 回采修复应作为独立 PR

## 6. 业界最佳实践对照

### 6.1 数据回采架构

参考：Datadog Agent、Fluentd、Prometheus Remote Write

| 实践 | 本项目现状 | 改进方向 |
|------|-----------|---------|
| 批量聚合 + 重试 | ✅ 已实现（500ms 聚合，指数退避）| - |
| 失败持久化 | ❌ 丢弃后无持久化 | 本地 IndexedDB 或 localStorage |
| ACK + 去重 | ❌ 无 ACK，无 dedup | 返回 accepted list + 去重 hash |
| 背压控制 | ❌ 无限缓冲 | 缓冲区上限 + 丢弃策略 |

### 6.2 可观测性

参考：Google SRE Book - The Four Golden Signals

| 信号 | 本项目现状 | 改进方向 |
|------|-----------|---------|
| Latency | ❌ 无 | 记录 flush 耗时分布 |
| Traffic | ❌ 无 | 记录 accepted/skipped 条数 |
| Errors | ❌ 静默 | 暴露 failures/dropped 计数 |
| Saturation | ❌ 无 | 监控 buffer 长度 |

**本次修复范围**：
- Errors 信号：暴露 `window.__bridgeLogStats`（最小改动，不引入新依赖）
- 后续可接入 Prometheus remote write 或自定义指标端点

---

## 附录：修复任务

详见：`docs/task/events/2026-07-20-terminal-bridge回采链路修复任务.md`

关键改动：
1. api-gateway 新增 `/api/bridge-logs` 代理路由（注入占位符 token）
2. conversation-service 鉴权弱化（接受 internal/占位符/JWT 三选一）
3. database schema 对齐（migration 006）
4. 前端暴露成功/失败计数（消除静默失败）
5. 端到端测试覆盖