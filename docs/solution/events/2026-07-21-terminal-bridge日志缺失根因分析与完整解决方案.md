---
status: categorized
categorized: terminal-bridge
audience: developer
date: 2026-07-21
related_prs:
  - "#587"
  - "#588"
---

# terminal_bridge 日志缺失根因分析与完整解决方案

## 一、背景与现象

### 1.1 问题发现

**工单 Q2026072160299** 验证发现，PR #586 合入后 `bridge_execution_logs` 表有数据，但：
- terminal_bridge 终端显示大量日志（50+ 行）
- 数据库只有 1 条日志（ssh.connected）
- **98% 的日志丢失**

### 1.2 核心问题

1. **执行返回数据缺失**：执行命令后，不知道返回了什么数据
2. **错误信息不明确**："终端会话缺失或桥未运行"太笼统，无法定位具体失败原因
3. **无法排障**：缺少关键信息，无法通过数据库日志排查端到端链路问题

---

## 二、第一性原理：日志的核心价值

### 2.1 根本需求

排障需要的核心能力：
1. **端到端链路追踪**：用户触发 → agent 推理 → 命令执行 → 结果返回 → 错误处理
2. **故障根因定位**：哪个环节失败？为什么失败？具体错误是什么？
3. **历史数据回溯**：过去执行了什么命令？返回了什么？失败原因？
4. **性能分析**：命令执行耗时？重试次数？超时原因？

### 2.2 业界最佳实践对比

| 维度 | 业界标准 | 当前实现 | 差距 |
|------|----------|----------|------|
| **结构化日志** | OpenTelemetry，统一 JSON 格式 | ✅ 有 logEntry 结构 | ✅ 基本符合 |
| **全链路追踪** | trace_id + span_id 链路 | ⚠️ 只有 trace_id | ❌ 不完整 |
| **事件完整性** | 开始/进行中/结束三阶段 | ❌ 只有开始 | ❌ 严重缺失 |
| **错误上下文** | 错误堆栈、重试次数、超时原因 | ❌ 笼统错误信息 | ❌ 无法定位 |
| **数据保留** | 输入 + 输出 + 耗时 + 退出码 | ❌ 只有输入 | ❌ 无法分析 |

---

## 三、根因拆解

### 3.1 临时会话日志未存储（预期行为）

**terminal_bridge 输出**：
```
Line 10-50: ssh.output 事件（40+ 条）
  - case_id: "ssh-create-temp"（临时会话）
  - event: "ssh.output"
```

**前端处理**：
```typescript
// chat.ts line 153
function forwardBridgeLog(entry: Record<string, unknown>) {
    if (!entry.case_id) return  // 过滤空 case_id
    // ssh-create-temp 不是真实工单，也被过滤
}
```

**说明**：临时会话的日志不属于任何工单，不应存储。这是预期行为。

---

### 3.2 exec 日志未发送（核心问题）❌

**现象**：
```
[Bridge] EXEC_ISOLATED_CMD: "acli task get -k '启动虚拟机失败' -l 100"
```

**缺失数据**：
- ❌ 命令是否执行成功
- ❌ 返回了什么数据
- ❌ 执行耗时
- ❌ 退出码

**根因**：

```go
// terminal_bridge main.go line 350-438
func (s *SSHSession) execCommandIsolated(...) {
    sendMsg(ws, OutMessage{Type: "exec_stdout", ...})  // ✅ 发送 WebSocket
    sendMsg(ws, OutMessage{Type: "exec_result", ...})  // ✅ 发送 WebSocket
    
    // ❌ 缺少 blog() 调用，没有发送 bridge_log！
}
```

**对比 ssh_exec_command**（有 bridge_log）：

```go
case "ssh_exec_command":
    blog("INFO", "exec.start", ...)   // ✅ 发送 bridge_log
    blog("INFO", "exec.output", ...)  // ✅ 发送 bridge_log
    blog("INFO", "exec.done", ...)    // ✅ 发送 bridge_log
```

**结果**：
- exec_stdout/exec_result 通过 WebSocket 发送
- 但前端监听 `msg.type === 'bridge_log'`
- 这些消息不会被 forwardBridgeLog() 处理
- **数据库中没有 exec 相关日志**

---

### 3.3 错误信息不明确

**当前错误信息**：
```
qfk_system：✗ 命令执行失败：终端会话缺失或桥未运行，未获得真实主机输出，无法判定信号
```

**缺失信息**：
- ❌ SSH 连接状态
- ❌ WebSocket 连接状态
- ❌ terminal_bridge 进程状态
- ❌ 命令是否发送
- ❌ 是否超时
- ❌ 重试次数

---

### 3.4 三种执行方式对比

| 执行方式 | 发起方 | WebSocket 消息类型 | 有 bridge_log | 日志落库 |
|----------|--------|-------------------|---------------|----------|
| **Agent 执行** | agent-service | ssh_exec_process | ❌ 无 | ❌ 无 |
| **SOP 执行** | scheduler/agent | ❓ 待确认 | ❓ 待确认 | ❓ 待确认 |
| **用户执行** | 用户手动 | ssh_input | ❌ 无 | ❌ 无 |

---

## 四、完整解决方案

### 4.1 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                      terminal_bridge                         │
│                                                              │
│  WebSocket 消息处理                                          │
│  ├─ ssh_connect      → blog(ssh.connected)                  │
│  ├─ ssh_input        → blog(user.input)                     │
│  ├─ ssh_exec_process → blog(exec.start)                     │
│  │                    → execCommandIsolated()                │
│  │                       ├─ blog(exec.error, on error)      │
│  │                       └─ blog(exec.done, with result)    │
│  └─ ssh_disconnect   → blog(ssh.disconnected)              │
│                                                              │
│  bridge_log 发送                                             │
│  ├─ logHub.publish(e) → 注入 type="bridge_log"             │
│  └─ flushSubscriber()  → WebSocket.Send(JSON)              │
└─────────────────────────────────────────────────────────────┘
          │
          │ WebSocket (type="bridge_log")
          ▼
┌─────────────────────────────────────────────────────────────┐
│                Custom-UI (前端)                              │
│                                                              │
│  WebSocket 消息监听                                          │
│  ├─ msg.type === 'bridge_log'                               │
│  └─ forwardBridgeLog(entry)                                 │
│                                                              │
│  缓冲 + 批量上报                                              │
│  ├─ bridgeLogBuffer.push(entry)                             │
│  └─ POST /api/bridge-logs {logs: [...]}                    │
└─────────────────────────────────────────────────────────────┘
          │
          │ HTTP POST
          ▼
┌─────────────────────────────────────────────────────────────┐
│                api-gateway → conversation-service            │
│                                                              │
│  POST /api/bridge-logs                                       │
│  ├─ 鉴权（INTERNAL_API_TOKEN 或占位符 token）                │
│  ├─ 过滤无 case_id 的条目                                    │
│  └─ INSERT INTO bridge_execution_logs                       │
└─────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────┐
│                    PostgreSQL                                │
│                                                              │
│  bridge_execution_logs 表                                    │
│  ├─ case_id, event, level, message                          │
│  ├─ exec_id, command, exit_code, duration_ms                │
│  ├─ stdout_len, stderr_len, output_preview                  │
│  └─ trace_id, created_at, success, error_type               │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 关键代码修改

#### 修改 1：execCommandIsolated 添加完整日志

```go
func (s *SSHSession) execCommandIsolated(ws *websocket.Conn, command, execID string) {
    startTime := time.Now()
    
    // P0: 记录命令开始
    blog("INFO", "exec.start", "开始执行命令", "", s.caseID, "", "", map[string]any{
        "exec_id": execID,
        "command": command,
        "command_len": len(command),
    })
    
    session, err := s.client.NewSession()
    if err != nil {
        // P0: 记录错误（包含详细错误信息和分类）
        blog("ERROR", "exec.error", "创建隔离 SSH 会话失败", "", s.caseID, "", "", map[string]any{
            "exec_id": execID,
            "error": err.Error(),
            "error_type": "session_creation_failed",
        })
        sendMsg(ws, OutMessage{
            Type: "exec_result", CaseID: s.caseID, ExecID: execID,
            Stderr: fmt.Sprintf("创建隔离 SSH 会话失败: %v", err), ExitCode: -1,
        })
        return
    }
    defer session.Close()

    // ...（stdout/stderr pipe 设置）

    if err := session.Start(command); err != nil {
        blog("ERROR", "exec.error", "启动命令失败", "", s.caseID, "", "", map[string]any{
            "exec_id": execID,
            "command": command,
            "error": err.Error(),
            "error_type": "command_start_failed",
        })
        sendMsg(ws, OutMessage{...})
        return
    }

    // ...（读取输出）

    wg.Wait()
    exitCode := 0
    if werr := session.Wait(); werr != nil {
        if exitErr, ok := werr.(*ssh.ExitError); ok {
            exitCode = exitErr.ExitStatus()
        } else {
            exitCode = -1
        }
    }
    
    duration := time.Since(startTime)
    
    // P0: 记录命令完成（包含所有关键信息）
    outputPreview := stdoutBuf.String()
    if len(outputPreview) > 500 {
        outputPreview = outputPreview[:500] + "...(截断)"
    }
    
    blog("INFO", "exec.done", "命令执行完成", "", s.caseID, "", "", map[string]any{
        "exec_id": execID,
        "command": command,
        "exit_code": exitCode,
        "success": exitCode == 0,
        "duration_ms": duration.Milliseconds(),
        "stdout_len": stdoutBuf.Len(),
        "stderr_len": stderrBuf.Len(),
        "output_preview": outputPreview,
    })

    sendMsg(ws, OutMessage{
        Type: "exec_result", CaseID: s.caseID, ExecID: execID,
        Stdout: stdoutBuf.String(), Stderr: stderrBuf.String(), ExitCode: exitCode,
    })
}
```

#### 修改 2：ssh_exec_process 传递 trace_id

```go
case "ssh_exec_process":
    // ...
    wrappedCmd := wrapContainerCommand(msg.Command, msg.Container)
    
    // P0: 记录命令发起（包含 trace_id）
    blog("INFO", "exec.request", "收到命令执行请求", msg.TraceID, msg.CaseID, msg.NodeIP, cui, map[string]any{
        "exec_id": msg.ExecID,
        "command": wrappedCmd,
        "container": msg.Container,
        "trace_id": msg.TraceID,
    })
    
    go s.execCommandIsolated(ws, wrappedCmd, msg.ExecID)
```

#### 修改 3：错误信息细粒度分类

```typescript
// chat.ts 或 agent-service
function classifyExecError(msg: ExecResult): string {
    if (msg.exitCode === -1 && msg.stderr.includes("SSH 会话不存在")) {
        return "SSH 会话未建立或已断开，请先建立 SSH 连接";
    }
    if (msg.exitCode === -1 && msg.stderr.includes("创建隔离 SSH 会话失败")) {
        return "SSH 连接异常，无法创建会话";
    }
    if (msg.exitCode === -1 && msg.stderr.includes("启动命令失败")) {
        return "命令启动失败，请检查命令格式或权限";
    }
    if (msg.exitCode === 127) {
        return "命令未找到，请检查命令是否存在";
    }
    if (msg.exitCode === 126) {
        return "命令权限不足，请检查执行权限";
    }
    if (msg.exitCode === 1) {
        return "命令执行失败（退出码 1），请检查命令参数或环境";
    }
    if (msg.exitCode === 137) {
        return "命令被强制终止（OOM 或信号），请检查系统资源";
    }
    if (msg.exitCode === 124) {
        return "命令执行超时，请增加超时时间或优化命令";
    }
    
    return `命令执行失败（退出码 ${msg.exitCode}）：${msg.stderr || msg.stdout || "无输出"}`;
}
```

### 4.3 数据库表结构扩展

```sql
-- Migration 007: 扩展 bridge_execution_logs 表结构
ALTER TABLE bridge_execution_logs 
ADD COLUMN IF NOT EXISTS command TEXT,
ADD COLUMN IF NOT EXISTS exit_code INTEGER,
ADD COLUMN IF NOT EXISTS duration_ms BIGINT,
ADD COLUMN IF NOT EXISTS stdout_len INTEGER,
ADD COLUMN IF NOT EXISTS stderr_len INTEGER,
ADD COLUMN IF NOT EXISTS output_preview TEXT,
ADD COLUMN IF NOT EXISTS success BOOLEAN,
ADD COLUMN IF NOT EXISTS error_type VARCHAR(50);

-- 创建索引加速查询
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_exec_id ON bridge_execution_logs(exec_id);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_success ON bridge_execution_logs(success);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_error_type ON bridge_execution_logs(error_type);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_duration ON bridge_execution_logs(duration_ms);
```

### 4.4 可观测性指标增强

```go
// terminal_bridge Prometheus 指标
var (
    execCommandsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "terminal_bridge_exec_commands_total",
        Help: "Total number of exec commands",
    }, []string{"case_id", "success", "exit_code"})
    
    execCommandDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
        Name: "terminal_bridge_exec_command_duration_ms",
        Help: "Duration of exec commands in milliseconds",
        Buckets: []float64{100, 500, 1000, 2000, 5000, 10000},
    }, []string{"case_id"})
    
    execCommandOutputSize = promauto.NewHistogramVec(prometheus.HistogramOpts{
        Name: "terminal_bridge_exec_command_output_bytes",
        Help: "Size of exec command output in bytes",
        Buckets: []float64{100, 1024, 10240, 102400, 1048576},
    }, []string{"case_id", "stream"})
)
```

---

## 五、验证方案

### 5.1 功能验证

```bash
# 1. 执行命令后，检查日志完整性
kubectl exec postgres-0 -n hci-dev -- psql -U hci_admin -d hci_troubleshoot -c "
SELECT 
    event, 
    COUNT(*) as count,
    AVG(duration_ms) as avg_duration_ms,
    SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
    SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as fail_count
FROM bridge_execution_logs
WHERE case_id = '<测试工单>'
GROUP BY event;
"

# 期望结果：
#     event     | count | avg_duration_ms | success_count | fail_count
# --------------+-------+-----------------+---------------+------------
#  exec.start   |     2 |            NULL |          NULL |       NULL
#  exec.done    |     2 |            1234 |             2 |          0
#  exec.error   |     0 |            NULL |          NULL |       NULL

# 2. 检查输出预览
kubectl exec postgres-0 -n hci-dev -- psql -U hci_admin -d hci_troubleshoot -c "
SELECT 
    exec_id,
    command,
    exit_code,
    duration_ms,
    stdout_len,
    output_preview
FROM bridge_execution_logs
WHERE event = 'exec.done' AND case_id = '<测试工单>'
ORDER BY created_at DESC;
"

# 期望结果：有具体数据，output_preview 有预览内容
```

---

## 六、总结

### 6.1 当前问题

1. ❌ 执行返回数据缺失（无法排障）
2. ❌ 错误信息不明确（无法定位）
3. ❌ 日志记录不完整（无法分析）
4. ❌ 三种执行方式不统一（维护困难）

### 6.2 解决方案价值

- ✅ 可以通过数据库日志排查端到端链路问题
- ✅ 可以快速定位故障根因
- ✅ 可以分析命令执行性能
- ✅ 可以进行历史数据回溯

---

## 七、关联文档

- PR #587：修复 logEntry 缺少 type 字段
- PR #588：完整实现 exec 日志记录
- 任务文档：`docs/task/events/2026-07-21-terminal-bridge日志回采完整能力补齐任务.md`