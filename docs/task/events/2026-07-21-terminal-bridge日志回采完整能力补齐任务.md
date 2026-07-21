---
task_id: T-OBS-07
priority: P0
status: pending
assignee: zcode
date: 2026-07-21
related_prs:
  - "#587"
  - "#588"
---

# terminal_bridge 日志回采完整能力补齐任务

## 背景

工单 Q2026072160299 验证发现，PR #586 合入后 `bridge_execution_logs` 表有数据，但 98% 的日志丢失，无法满足排障需求。

**核心问题**：
- 执行返回数据缺失
- 错误信息不明确
- 无法通过数据库日志排查端到端链路问题

详细分析见：`docs/solution/events/2026-07-21-terminal-bridge日志缺失根因分析与完整解决方案.md`

---

## 目标

1. **记录完整的命令执行日志**：开始时间、结束时间、命令、退出码、输出预览
2. **提供细粒度错误分类**：区分 SSH 会话问题、命令启动问题、执行超时等
3. **支持历史数据回溯**：可通过数据库查询命令执行历史
4. **补齐可观测性能力**：满足端到端排障需求

---

## 文件清单

### 新建文件

1. `database/data-migrations/007_extend_bridge_execution_logs_table.sql` - 数据库表结构扩展

### 修改文件

1. `terminal_bridge/main.go` - 添加完整的 bridge_log 支持
   - execCommandIsolated() 添加 exec.start/exec.done 日志
   - 添加错误分类（error_type）
   - 传递 trace_id

---

## 实现步骤

### 步骤 1：修改 terminal_bridge execCommandIsolated()

**文件**：`terminal_bridge/main.go`

**修改点**：

1. 添加 exec.start 日志（记录命令开始）
2. 添加 exec.error 日志（记录错误详情和分类）
3. 添加 exec.done 日志（记录命令完成、退出码、耗时、输出预览）

**代码示例**：

```go
func (s *SSHSession) execCommandIsolated(ws *websocket.Conn, command, execID string) {
    startTime := time.Now()
    
    // 记录开始
    blog("INFO", "exec.start", "开始执行命令", "", s.caseID, "", "", map[string]any{
        "exec_id": execID,
        "command": command,
    })
    
    // ... 执行命令
    
    // 记录完成
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
}
```

---

### 步骤 2：扩展数据库表结构

**文件**：`database/data-migrations/007_extend_bridge_execution_logs_table.sql`

**内容**：

```sql
-- Migration 007: 扩展 bridge_execution_logs 表结构
-- 工单：Q2026072160299
-- 日期：2026-07-21

ALTER TABLE bridge_execution_logs 
ADD COLUMN IF NOT EXISTS command TEXT,
ADD COLUMN IF NOT EXISTS exit_code INTEGER,
ADD COLUMN IF NOT EXISTS duration_ms BIGINT,
ADD COLUMN IF NOT EXISTS stdout_len INTEGER,
ADD COLUMN IF NOT EXISTS stderr_len INTEGER,
ADD COLUMN IF NOT EXISTS output_preview TEXT,
ADD COLUMN IF NOT EXISTS success BOOLEAN,
ADD COLUMN IF NOT EXISTS error_type VARCHAR(50);

CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_exec_id ON bridge_execution_logs(exec_id);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_success ON bridge_execution_logs(success);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_error_type ON bridge_execution_logs(error_type);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_duration ON bridge_execution_logs(duration_ms);
```

---

### 步骤 3：更新 desired_schema.sql

**文件**：`database/desired_schema.sql`

**修改**：同步 migration 007 的表结构变更

---

### 步骤 4：更新前端错误分类（可选）

**文件**：`frontend/customer/src/stores/chat.ts`

**说明**：如果需要更友好的错误提示，可以添加错误分类函数。但当前 terminal_bridge 已经提供了详细的 stderr 信息，这一步可以延后。

---

## 验收标准

### 功能验收

1. ✅ 执行命令后，数据库有 exec.start 和 exec.done 日志
2. ✅ exec.done 包含：command、exit_code、duration_ms、stdout_len、stderr_len、output_preview
3. ✅ 错误场景有 exec.error 日志，包含 error_type 分类
4. ✅ 成功场景 success=true，失败场景 success=false

### 查询验证

```bash
# 1. 检查日志完整性
kubectl exec postgres-0 -n hci-dev -- psql -U hci_admin -d hci_troubleshoot -c "
SELECT event, COUNT(*) FROM bridge_execution_logs 
WHERE case_id='<测试工单>' 
GROUP BY event;
"

# 期望：有 exec.start 和 exec.done

# 2. 检查输出预览
kubectl exec postgres-0 -n hci-dev -- psql -U hci_admin -d hci_troubleshoot -c "
SELECT command, exit_code, duration_ms, output_preview 
FROM bridge_execution_logs 
WHERE case_id='<测试工单>' AND event='exec.done'
ORDER BY created_at DESC LIMIT 1;
"

# 期望：有具体数据和输出预览
```

---

## 风险与依赖

### 风险

- **日志量增加**：每个命令执行会记录 2-3 条日志，需关注存储容量
- **输出截断**：output_preview 截断到 500 字符，可能丢失关键信息（但完整输出在 stdout_len 记录）

### 依赖

- PR #587：logEntry 缺少 type 字段修复（已合入）
- 前端已支持 bridge_log 消息接收

---

## 时间估算

- **步骤 1**：修改 terminal_bridge，添加 bridge_log - 2h
- **步骤 2**：创建数据库 migration - 0.5h
- **步骤 3**：更新 desired_schema.sql - 0.5h
- **步骤 4**：测试验证 - 1h
- **总计**：4h

---

## 关联文档

- 方案文档：`docs/solution/events/2026-07-21-terminal-bridge日志缺失根因分析与完整解决方案.md`
- 前序任务：`docs/task/events/2026-07-20-terminal-bridge回采链路修复任务.md`