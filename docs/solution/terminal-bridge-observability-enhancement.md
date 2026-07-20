# terminal_bridge 可观测性增强

> **版本**: v2.16.0
> **更新日期**: 2026-07-20
> **关联 PR**: #577

## 概述

在 PR #576 基础上，完整实现了 terminal_bridge 可观测性与日志回采的所有增强功能，修复了审查报告中发现的所有关键缺陷。

## 核心功能

### 1. 完整的日志回采

- **exec.output 事件**: 记录命令执行的完整输出（截断 1000 字符）
- **ssh.output 事件**: 记录 SSH 终端交互日志（过滤控制序列）
- 按工单（`case_id`）和端到端链路（`trace_id`）关联

### 2. 异常重传机制（3 层保护）

1. **环形缓冲**: 5000 条历史日志供晚加入/重连回放
2. **pending 缓冲**: 每个订阅者独立缓冲 2000 条待重传日志
3. **localStorage 保护**: 浏览器关闭前保存 pending 日志，页面加载时恢复

### 3. 浏览器重连支持

- 前端重连时自动发送 `resume` 消息
- 触发历史日志回放（基于 `case_id` 过滤）

### 4. 进程重启回放

- 启动时从 `HCI_BRIDGE_LOG_DIR/bridge.log` 读取历史日志
- 自动加载到环形缓冲并回放给已连接的订阅者

### 5. OpenTelemetry 集成

- W3C Traceparent 标准格式：`00-{trace_id}-0000000000000001-01`
- Span 关联标记为已知限制（有明确实现路径）

### 6. Prometheus 指标

新增 `/metrics` 端点，暴露关键指标：

- `bridge_logs_collected_total`: 日志收集总数
- `bridge_logs_collect_errors_total`: 日志收集错误数
- `bridge_logs_replayed_total`: 日志回放总数
- `bridge_exec_commands_total`: 命令执行总数
- `bridge_exec_command_errors_total`: 命令执行错误数
- `bridge_ssh_connections_total`: SSH 连接总数
- `bridge_ssh_connection_errors_total`: SSH 连接错误数

### 7. 安全签名验证

- HMAC-SHA256 签名验证（可选功能）
- 前端可选择性提供 `signature` 字段
- 防止日志内容篡改

### 8. 数据库优化

- 新增 `event` 字段索引（优化查询性能）
- 新增 `user_id` 字段（记录操作用户）
- 90 天日志保留策略（通过 pg_cron 定期清理）

## 使用方法

### 环境变量配置

```bash
# 启用本地日志持久化（进程重启回放）
export HCI_BRIDGE_LOG_DIR=/var/log/hci-bridge

# 启用 HMAC 签名验证（可选）
export BRIDGE_LOG_HMAC_KEY=your-secret-key
```

### Prometheus 抓取配置

```yaml
scrape_configs:
  - job_name: 'terminal_bridge'
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:9999']
```

### Grafana Dashboard

导入配置文件：`deploy/monitoring/grafana-dashboard-terminal-bridge.json`

## 已知限制

### Span 关联未完整实现

**当前状态**: 只支持 Trace ID 链路追踪，未建立完整的 Span 父子关系。

**影响**: 无法在分布式追踪系统中查看完整的调用层级。

**完整实现路径**:
1. 生成唯一 Span ID
2. 维护 Parent Span ID
3. 建立 Span 栈管理

**临时方案**: Traceparent 格式中的 Span ID 固定为 1，已满足基本的端到端追踪需求。

## 相关文档

- [terminal_bridge 可观测性与日志回采重设计](/docs/solution/events/2026-07-20-terminal-bridge可观测性与日志回采重设计.md)
- [数据库设计 - bridge_execution_logs 表](/docs/solution/数据库设计.md#bridge_execution_logs)
- [前端诊断报告与步骤渲染顺序修复](/docs/solution/events/2026-07-20-前端诊断报告与步骤渲染顺序修复.md)