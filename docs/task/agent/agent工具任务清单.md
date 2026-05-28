---
status: active
category: task
audience: developer-agent
last_updated: 2026-05-28
owner: team
update_trigger: agent工具设计 v2.0 落地实现（T-TOOL-05/06/07 已完成）
---

# 任务清单：Agent 工具体系 v2.0

> 关联方案：[agent工具设计.md](../../solution/agent/agent工具设计.md)（**必读，理解决策背景后再开发**）  
> 关联方案：[agent设计.md](../../solution/agent/agent设计.md) §十（目录结构）  
> 项目根目录：`/mnt/d/aihci/hci-troubleshoot-platform/`

---

## 变更历史

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2026-05-28 | v1.1 | 完成 T-TOOL-05/06/07：conversation-service 内部 API + SSE 推送机制 |
| 2026-05-29 | v1.0 | 初版：基于 agent工具设计.md v2.0 完整梳理，20 个任务 |

---

## 任务优先级说明

| 优先级 | 含义 |
|--------|------|
| **P0** | 阻断性，必须最先完成（其他任务依赖它） |
| **P1** | 核心功能，P0 完成后立即开展 |
| **P2** | 清理/优化，不阻断功能验证 |

---

## 任务依赖关系图

```
P0 阶段（DB + Bridge 通道）
  T8 → T9 → T10          DB schema → 迁移 → 数据填充
  T1 → T2                terminal_bridge Go 端
  T3 → T4                前端 terminal.ts → chat.ts
  T5 → T6 → T7           conversation-service API + SSE

P1 阶段（Python 后端）
  T19 → T20              目录重命名（先做，避免后续 import 冲突）
  T11 → T12 → T13        classifier → executor → __init__
  T10 → T14              数据填充完成后，tool_registry.py 才能改为 DB 加载器
  T14 → T15 → T16        registry → react_engine → CompositeToolExecutor

P2 阶段（清理）
  T17                    acli_client.py（已完成，确认即可）
  T18                    前端确认卡片
```

---

## Phase 1：P0 任务（DB Schema + Bridge Relay 通道建设）

> **完成标志**：可以从 Python 发出命令，经由 Redis → SSE → terminal_bridge → SSH，结果回传到 Python。

---

### T-TOOL-08：移除 `tool_definition.tool_type` 列，更新 `risk_level` 注释

**优先级**：P0  
**文件**：`database/desired_schema.sql`  
**依赖**：无（可独立开始）

**背景**：`tool_type` 与 `category` 语义完全重叠（均表示工具执行后端归属），在 v2.0 合并为 `category`，避免两列维护。

**具体改动**（`tool_definition` 表）：

```sql
-- 删除以下列定义（desired_schema.sql 中找到并移除）：
tool_type  TEXT,  -- 或 tool_type TEXT NOT NULL DEFAULT 'scp',

-- 同时更新 risk_level 列的注释，区分静态值和动态覆盖：
risk_level INT NOT NULL DEFAULT 1,
-- 注释：1=auto(自动执行) 2=confirm(用户确认) 3=block(拒绝执行)
-- 语义说明：对 acli_exec/bash_exec 为"静态兜底值"，运行时被 RiskClassifier 覆盖；
--           对插件工具/SCP/SOP 为"固定值"，不经过 RiskClassifier。
```

**验收标准**：
- [ ] `desired_schema.sql` 中 `tool_definition` 表无 `tool_type` 列
- [ ] `risk_level` 列有清晰注释说明静态/动态语义

---

### T-TOOL-09：生成 Atlas 迁移文件（移除 tool_type 列）

**优先级**：P0  
**文件**：`database/migrations/` 目录下新建迁移文件  
**依赖**：T-TOOL-08 完成

**背景**：项目使用 Atlas 管理数据库 schema，需要通过 `atlas schema diff` 生成迁移文件。

**执行步骤**：
```bash
# 在项目根目录执行
cd /mnt/d/aihci/hci-troubleshoot-platform

# 查看当前 atlas 配置
cat atlas.hcl

# 生成迁移文件（根据实际 atlas 配置调整命令）
atlas schema diff \
  --from "file://database/desired_schema.sql" \
  --to env://dev \
  --format "{{ sql . }}"
```

> **注意**：先确认 `atlas.hcl` 中的连接配置，以及是否需要 `atlas migrate diff` 命令。  
> 若环境不可用，手写迁移 SQL 文件放入 `database/migrations/`：

```sql
-- database/migrations/YYYYMMDD_drop_tool_definition_tool_type.sql
-- migrate:up
ALTER TABLE tool_definition DROP COLUMN IF EXISTS tool_type;

-- migrate:down
ALTER TABLE tool_definition ADD COLUMN IF NOT EXISTS tool_type TEXT NOT NULL DEFAULT 'scp';
```

**验收标准**：
- [ ] 生成/创建对应迁移文件
- [ ] 迁移文件包含 up/down
- [ ] 在测试 DB 执行无报错

---

### T-TOOL-10：填充 `tool_definition` 表数据（从旧 tool_registry.py 迁移）

**优先级**：P0  
**文件**：新建 `database/seeds/tool_definition_v2.sql`（或 `scripts/db/seed_tools.py`）  
**依赖**：T-TOOL-09 完成

**背景**：v2.0 将 `tool_registry.py` 改为从 DB 加载，因此必须先在 DB 中有数据。需要将旧代码中的工具定义迁移到 `tool_definition` 表。

**需要插入的工具清单**（共 13 条）：

| tool_name | category | risk_level | is_active |
|-----------|----------|-----------|----------|
| `acli_exec` | acli | 1 | true |
| `bash_exec` | acli | 1 | true |
| `acli_plugin_vm_start` | acli | 1 | true |
| `acli_plugin_vm_suspend` | acli | 1 | true |
| `acli_plugin_netdoctor` | acli | 2 | true |
| `acli_plugin_asys` | acli | 1 | true |
| `get_active_alerts` | scp | 1 | true |
| `get_failed_tasks` | scp | 1 | true |
| `get_vm_list` | scp | 1 | true |
| `get_cluster_detail` | scp | 1 | true |
| `get_sop_node` | sop | 1 | true |
| `sop_advance` | sop | 1 | true |
| `sop_request_variable` | sop | 1 | true |

**`description` 和 `parameters_schema` 内容**：
- `acli_exec` / `bash_exec` / 4 个插件工具：从 `agent工具设计.md` §四的 `tool_definition 记录` 部分直接复制
- SCP/SOP 工具：从 `backend/agent-service/app/adapters/agents/htp/tool_registry.py` 中对应 `ToolDefinition` 对象的 `description` 和 `parameters` 字段迁移

**注意**：旧代码中的 SCP/SOP 工具定义在 `tool_registry.py` 前 ~60 行，逐条读取并转为 INSERT 语句。

**SQL 模板**：
```sql
INSERT INTO tool_definition (tool_name, display_name, category, description, parameters_schema, risk_level, is_active, version)
VALUES
  ('acli_exec', 'HCI CLI 执行', 'acli', '...（完整 description）...', '{"type":"object",...}', 1, true, 1),
  ...
ON CONFLICT (tool_name) DO UPDATE SET
  description = EXCLUDED.description,
  parameters_schema = EXCLUDED.parameters_schema,
  updated_at = NOW();
```

**验收标准**：
- [ ] SQL 文件执行无报错
- [ ] `SELECT tool_name, category, is_active FROM tool_definition ORDER BY category, tool_name;` 返回 13 条记录
- [ ] 每条记录的 `parameters_schema` 是合法 JSON，可通过 `pg_typeof(parameters_schema)` 验证为 jsonb

---

### T-TOOL-01：`terminal_bridge/main.go` 新增 `ssh_exec_command` 消息处理

**优先级**：P0  
**文件**：`terminal_bridge/main.go`（Go）  
**依赖**：无（可与 T-TOOL-08/09/10 并行）

**背景**：terminal_bridge 是运行在用户浏览器端的本地程序，负责 WebSocket ↔ SSH 的双向转发。需要新增一个消息类型，让 Agent 可以通过前端向 SSH 注入命令并捕获结果。

**新增入站消息处理**（Frontend → Bridge via WebSocket）：

消息格式：
```json
{
  "type": "ssh_exec_command",
  "case_id": "case-123",
  "exec_id": "550e8400-e29b-41d4-a716-446655440000",
  "command": "acli vm list --formatter json; status=$?; printf '\\n__EXEC_DONE_550e8400:%s\\n' \"$status\"\n"
}
```

处理逻辑（伪代码）：
1. 解析消息，提取 `exec_id`、`command`
2. 从 `command` 中提取 marker（命令末尾的 `__EXEC_DONE_{execId16}__` 部分）
3. 注册 marker 监听器到 output goroutine（按 case_id 找到对应 SSH session）
4. 将 `command`（含 `\n`，会自动执行）写入 SSH stdin：`session.Send(command)`
5. output goroutine 扫描到 marker 时，构建 `exec_result` 消息发送回 WebSocket

**注意**：
- `command` 字段已包含 marker 和换行符，直接写入 stdin 即可
- marker 格式为 `__EXEC_DONE_{exec_id前16位}:{exit_code}` — 从 marker 解析 exit_code
- output 中 marker 之前的内容即为 stdout

**验收标准**：
- [ ] Go 代码编译通过（`cd terminal_bridge && go build .`）
- [ ] 接收 `ssh_exec_command` 消息不 panic
- [ ] 命令执行后正确发回 `exec_result` 消息

---

### T-TOOL-02：`terminal_bridge/main.go` 新增 `exec_result` 出站消息

**优先级**：P0  
**文件**：`terminal_bridge/main.go`（Go）  
**依赖**：T-TOOL-01

**出站消息格式**（Bridge → Frontend via WebSocket）：
```json
{
  "type": "exec_result",
  "case_id": "case-123",
  "exec_id": "550e8400-e29b-41d4-a716-446655440000",
  "output": "vm_name  vm_id\n...",
  "exit_code": 0
}
```

**实现要点**：
- `output`：marker 出现位置之前的所有输出（去掉 marker 行本身）
- `exit_code`：从 marker 字符串中解析（`__EXEC_DONE_{id}:{exit_code}`）
- 超时处理：若 60s 内未收到 marker，发送 `exit_code: -1`，`output: "execution timeout"`

**验收标准**：
- [ ] 命令正常执行后，前端 WebSocket 收到 `type: "exec_result"` 消息
- [ ] `output` 包含命令输出，不含 marker 行
- [ ] `exit_code` 准确反映命令返回值

---

### T-TOOL-03：`frontend` 新增 `buildExecCommandMessage()`

**优先级**：P0  
**文件**：`frontend/src/api/terminal.ts`（TypeScript）  
**依赖**：无

**实现内容**：在 `terminal.ts` 中新增函数（参考现有 `buildBridgeCommandPayload`）：

```typescript
/**
 * 构造 Agent 执行命令的 WebSocket 消息（ssh_exec_command 类型）。
 * 命令末尾自动追加 marker，用于捕获执行结果。
 */
export function buildAgentExecMessage(
  caseId: string,
  execId: string,
  rawCommand: string
): string {
  const markerId = execId.replace(/-/g, '').substring(0, 16)
  const marker = `__EXEC_DONE_${markerId}`
  // 追加 marker 和换行，使命令自动执行
  const command = `${rawCommand}; status=$?; printf '\\n${marker}:%s\\n' "$status"\n`
  return JSON.stringify({
    type: 'ssh_exec_command',
    case_id: caseId,
    exec_id: execId,
    command,
  })
}

/**
 * 解析 exec_result 消息中的 output 和 exit_code。
 */
export function parseAgentExecResult(message: unknown): {
  execId: string
  output: string
  exitCode: number
} | null {
  if (
    typeof message !== 'object' ||
    message === null ||
    (message as Record<string, unknown>).type !== 'exec_result'
  ) return null
  const m = message as Record<string, unknown>
  return {
    execId: String(m.exec_id ?? ''),
    output: String(m.output ?? ''),
    exitCode: Number(m.exit_code ?? -1),
  }
}
```

**验收标准**：
- [ ] TypeScript 编译通过（`pnpm typecheck`）
- [ ] 函数导出正常，`chat.ts` 可 import

---

### T-TOOL-04：`frontend/src/stores/chat.ts` 监听 `agent_exec_command` SSE 事件

**优先级**：P0  
**文件**：`frontend/src/stores/chat.ts`（TypeScript）  
**依赖**：T-TOOL-03，T-TOOL-05，T-TOOL-06

**背景**：Agent 通过 conversation-service 下发 `agent_exec_command` SSE 事件到前端，前端需要：
1. 拦截该事件
2. 通过 terminal_bridge 的 WebSocket 执行命令
3. 等待 `exec_result` 消息
4. POST 结果到 `/api/conversations/{id}/exec-result`

**新增 SSE 事件处理逻辑**（在现有 SSE 监听处新增 case）：

```typescript
// SSE 事件：agent_exec_command
// data: { execId, command, reason, riskLevel, nodeIp, caseId }

case 'agent_exec_command': {
  const { execId, command, riskLevel, caseId } = data

  // risk=3：直接拒绝，不到达 bridge
  if (riskLevel >= 3) {
    await postExecResult(convId, execId, '', -1, 'blocked')
    return
  }

  // risk=2：展示确认卡片（见 T-TOOL-18）
  // risk=1：直接执行
  const shouldExec = riskLevel === 1
    ? true
    : await showShellConfirmCard(data)  // 返回 Promise<boolean>

  if (!shouldExec) {
    await postExecResult(convId, execId, '', -1, 'user_rejected')
    return
  }

  // 通过 terminal_bridge WebSocket 执行命令
  const msg = buildAgentExecMessage(caseId, execId, command)
  bridgeWs.send(msg)

  // 监听 exec_result（带超时）
  const result = await waitForExecResult(execId, 35_000)
  await postExecResult(convId, execId, result.output, result.exitCode)
  break
}
```

**辅助函数**：
- `waitForExecResult(execId, timeout)` → `Promise<{output, exitCode}>`：在 bridgeWs.onmessage 中注册一次性监听器，resolved 后自动清除
- `postExecResult(convId, execId, output, exitCode, status?)` → `POST /api/conversations/{id}/exec-result`

**验收标准**：
- [ ] 收到 `agent_exec_command` SSE 事件后，命令被发送到 terminal_bridge
- [ ] 收到 `exec_result` 后，结果被正确 POST 回后端
- [ ] risk=2 时展示确认界面（可先用 `window.confirm()` 占位）
- [ ] 超时（35s）后 POST `exit_code: -1, output: "timeout"`

---

### T-TOOL-05：`conversation-service` 新增 `POST /internal/conversations/{id}/agent-exec`

**优先级**：P0  
**文件**：`backend/conversation-service/`（Python）  
**依赖**：无（可并行）

**背景**：Agent Service 通过内部接口通知 Conversation Service 有命令需要执行，Conversation Service 再通过 SSE 推给前端。

**接口规格**：

```
POST /internal/conversations/{conversation_id}/agent-exec
Authorization: Bearer {INTERNAL_SERVICE_TOKEN}
Content-Type: application/json

{
  "exec_id": "550e8400-e29b-41d4-a716-446655440000",
  "command": "acli vm list --formatter json",
  "reason": "检查 VM 状态",
  "risk_level": 1,
  "node_ip": "192.168.1.10",
  "case_id": "case-123"
}
```

**处理逻辑**：
1. 鉴权：校验 `Authorization: Bearer {INTERNAL_SERVICE_TOKEN}`（从环境变量读取）
2. 写 Redis：`SETEX exec:{exec_id} 120 "pending"`（120s 过期）
3. 推送 SSE 事件（通过现有 SSE 推送机制）：
   ```json
   {
     "event": "agent_exec_command",
     "data": {
       "execId": "...",
       "command": "...",
       "reason": "...",
       "riskLevel": 1,
       "nodeIp": "192.168.1.10",
       "caseId": "case-123"
     }
   }
   ```
4. 返回 `{"status": "accepted", "exec_id": "..."}`（202 Accepted）

**注意**：不等待执行结果（异步），结果通过 T-TOOL-06 接口异步回传。

**验收标准**：
- [x] 接口存在，鉴权校验有效
- [x] Redis key 正确写入（TTL 120s）
- [x] SSE 推送到对应 conversation 的订阅者
- [x] 返回 202

---

### T-TOOL-06：`conversation-service` 新增 `POST /conversations/{id}/exec-result`

**优先级**：P0  
**文件**：`backend/conversation-service/`（Python）  
**依赖**：T-TOOL-05

**背景**：前端执行命令后，将结果 POST 回 Conversation Service，CS 再写入 Redis 队列让 Agent Service 通过 blpop 获取。

**接口规格**：

```
POST /api/conversations/{conversation_id}/exec-result
Authorization: Bearer {USER_SESSION_TOKEN}（前端用户鉴权）
Content-Type: application/json

{
  "exec_id": "550e8400-e29b-41d4-a716-446655440000",
  "output": "vm_name  vm_id\n...",
  "exit_code": 0
}
```

**处理逻辑**：
1. 鉴权：校验用户 Session（防止伪造结果）
2. 验证 `exec_id` 对应的 Redis key 存在（`GET exec:{exec_id}` 不为空）
3. 写 Redis 队列：`LPUSH exec_result:{exec_id} {json}`
4. 删除 pending key：`DEL exec:{exec_id}`
5. 返回 `{"status": "ok"}`

**验收标准**：
- [x] 接口存在，鉴权有效
- [x] `exec_result:{exec_id}` Redis key 被正确写入
- [x] `exec:{exec_id}` pending key 被清除
- [x] 不存在 `exec_id` 时返回 404

---

### T-TOOL-07：`conversation-service` SSE 推送 `agent_exec_command` 事件

**优先级**：P0  
**文件**：`backend/conversation-service/`（Python）  
**依赖**：T-TOOL-05（此任务是 T-TOOL-05 中 SSE 推送部分的具体实现）

**背景**：Conversation Service 已有 SSE 推送基础设施（现有告警、任务通知走的同一套）。需要确认现有推送机制可以携带 `agent_exec_command` 事件类型。

**检查点**：
1. 找到现有 SSE 推送代码（搜索 `event:` 或 `EventSource` 相关路径）
2. 确认推送格式符合 `event: agent_exec_command\ndata: {...}\n\n`
3. 确认前端 SSE 监听代码能正确路由该事件类型

**若现有机制支持，只需**：
- 在 T-TOOL-05 的处理中调用现有推送函数，传入正确的事件类型和 data 即可

**验收标准**：
- [x] 前端 EventSource 能接收到 `agent_exec_command` 类型的 SSE 事件
- [x] 事件 data 中包含 `execId`、`command`、`riskLevel`、`nodeIp`

---

## Phase 2：P1 任务（Python 后端核心实现）

> **完成标志**：Agent 可以通过 `acli_exec`/`bash_exec` 工具调用在真实 HCI 节点执行命令，结果正确返回给 LLM，风险控制有效。

---

### T-TOOL-19：目录重命名 `app/tools/shell/` → `app/tools/acli/`

**优先级**：P1（先于 T-TOOL-11/12/13 执行，避免后续 import 路径混乱）  
**文件**：`backend/agent-service/app/tools/shell/`  
**依赖**：无

**操作步骤**：
```bash
cd backend/agent-service

# 如果 shell/ 目录已存在（可能在先前迭代中创建）
mv app/tools/shell app/tools/acli

# 如果 shell/ 不存在，直接创建 acli/ 目录
mkdir -p app/tools/acli

# 更新所有 import（如有引用 tools.shell 的文件）
grep -r "from app.tools.shell" . --include="*.py" -l
grep -r "import app.tools.shell" . --include="*.py" -l
# 对找到的文件执行替换
sed -i 's/from app\.tools\.shell/from app.tools.acli/g' <文件>
```

**验收标准**：
- [ ] `app/tools/acli/` 目录存在
- [ ] `app/tools/shell/` 目录不存在（或为空）
- [ ] 全局搜索 `tools.shell` 无结果：`grep -r "tools\.shell" backend/agent-service --include="*.py"`

---

### T-TOOL-20：文件重命名 `app/tools/base.py` → `app/tools/base_tool.py`

**优先级**：P1（与 T-TOOL-19 同步执行）  
**文件**：`backend/agent-service/app/tools/base.py`  
**依赖**：无

**操作步骤**：
```bash
cd backend/agent-service

# 重命名
mv app/tools/base.py app/tools/base_tool.py

# 更新所有 import
grep -r "from app.tools.base import\|from app\.tools\.base " . --include="*.py" -l
# 替换
sed -i 's/from app\.tools\.base import/from app.tools.base_tool import/g' <文件>
```

**验收标准**：
- [ ] `app/tools/base_tool.py` 存在
- [ ] `app/tools/base.py` 不存在
- [ ] `grep -r "from app.tools.base " backend/agent-service --include="*.py"` 无结果

---

### T-TOOL-11：实现 `app/tools/acli/classifier.py`

**优先级**：P1  
**文件**：`backend/agent-service/app/tools/acli/classifier.py`（新建）  
**依赖**：T-TOOL-19

**实现内容**：直接参考 `agent工具设计.md` §六.2 的完整代码实现。

**关键要求**：
- `classify_acli(command: str) -> int`：对 acli 命令进行风险分级，返回 1/2/3
- `classify_bash(command: str) -> int`：对 bash 命令进行风险分级，返回 1/2/3
- `risk_to_policy(risk: int) -> str`：映射 1→"auto"，2→"confirm"，3→"block"
- 规则按 risk 降序排列，第一个匹配获胜
- 默认返回 1（未命中规则时，视为只读）

**单元测试**（必须提供，放在 `tests/tools/test_classifier.py`）：
```python
# 测试用例示例
assert classify_acli("acli vm list --formatter json") == 1
assert classify_acli("acli service asv redis restart") == 2
assert classify_acli("acli vm delete abc-123") == 3

assert classify_bash("df -h /") == 1
assert classify_bash("systemctl restart nginx") == 2
assert classify_bash("rm -rf /tmp/test") == 3
assert classify_bash("dd if=/dev/zero of=/dev/sda") == 3
```

**验收标准**：
- [ ] 文件存在，无语法错误
- [ ] `pytest tests/tools/test_classifier.py` 全部通过
- [ ] 边界测试：空字符串、None、超长命令均返回 1 而不报错

---

### T-TOOL-12：实现 `app/tools/acli/executor.py`

**优先级**：P1  
**文件**：`backend/agent-service/app/tools/acli/executor.py`（新建）  
**依赖**：T-TOOL-11，T-TOOL-05，T-TOOL-06（接口已存在才能真正打通）

**需要实现的类和函数**：

**`ExecResult` dataclass**：
```python
@dataclass
class ExecResult:
    stdout: str          # 标准输出（截断 ≤ 4000 chars）
    stderr: str          # 错误输出（截断 ≤ 1000 chars）
    exit_code: int
    command: str         # 实际执行的命令（净化后）
    node: str            # 执行节点 IP
    duration_ms: int
    truncated: bool
    risk_level: int      # 本次执行的实际风险等级
```

**`CommandSanitizer` 类**（或函数）：
```python
# 净化规则（拒绝时抛 ValueError）：
# 1. 命令替换：$(...)、`...`
# 2. 命令链：&&、||、;（pipe | 允许）
# 3. 路径穿越：../、/etc/shadow、/root/.ssh/
# 4. bash_exec 禁止以 acli 开头
# 5. acli_exec 必须以 acli 开头
def sanitize(command: str, tool_name: str) -> str: ...
```

**`BridgeRelayExecutor` 类**（核心）：

```python
class BridgeRelayExecutor:
    def __init__(
        self,
        redis: Redis,
        conversation_service_url: str,  # 例如 "http://conversation-service:8000"
        internal_token: str,
    ) -> None: ...

    async def execute(
        self,
        tool_name: str,
        args: dict,
        *,
        conversation_id: str,
        node_ip: str | None = None,
        risk_level: int = 1,   # 已由 react_engine 的 RiskClassifier 覆盖
        policy: str = "auto",
    ) -> ExecResult:
        """
        执行流程：
        1. 净化命令（CommandSanitizer）
        2. policy="block" → 直接返回 ExecResult(exit_code=-1, stdout="[blocked]...")
        3. 生成 exec_id（uuid4）
        4. POST /internal/conversations/{conv_id}/agent-exec（含命令、risk_level、node_ip、case_id）
        5. await redis.blpop(f"exec_result:{exec_id}", timeout=32)
        6. 解析结果，截断输出，构建 ExecResult
        7. 写 tool_result 表（审计）
        """
```

**tool_result 写入**（步骤 7）：
```python
# 写入 tool_result 表（使用现有 DB session）
await db.execute(insert(ToolResult).values(
    exec_id=exec_id,
    tool_name=tool_name,
    command=clean_command,
    node_ip=node_ip or "",
    risk_level=risk_level,
    authorized_by=authorized_by,  # risk=2 时为确认用户名，risk=1 时为 "auto"
    duration_ms=duration_ms,
    exit_code=result.exit_code,
    output=result.stdout,
    trace_id=ctx.trace_id,
))
```

**验收标准**：
- [ ] `CommandSanitizer` 正确拒绝注入命令（`$(ls)`、`; rm -rf`）
- [ ] `blpop` 超时时返回包含 "timeout" 的 ExecResult（exit_code=-1）
- [ ] `tool_result` 表有对应记录
- [ ] stdout 超过 4000 chars 时被截断，`truncated=True`

---

### T-TOOL-13：创建 `app/tools/acli/__init__.py`

**优先级**：P1  
**文件**：`backend/agent-service/app/tools/acli/__init__.py`（新建）  
**依赖**：T-TOOL-11，T-TOOL-12

**实现内容**：

```python
"""
app/tools/acli：HCI 节点执行工具包（acli_exec + bash_exec + 插件工具）。

所有工具共用 BridgeRelayExecutor 执行后端，走 terminal_bridge 中转路径。
"""

from app.tools.acli.classifier import classify_acli, classify_bash, risk_to_policy
from app.tools.acli.executor import BridgeRelayExecutor, ExecResult

__all__ = [
    "BridgeRelayExecutor",
    "ExecResult",
    "classify_acli",
    "classify_bash",
    "risk_to_policy",
]
```

**验收标准**：
- [ ] `from app.tools.acli import BridgeRelayExecutor` 可正常 import
- [ ] `python -c "from app.tools.acli import *"` 无报错

---

### T-TOOL-14：重写 `htp/tool_registry.py` 为 DB 加载器

**优先级**：P1  
**文件**：`backend/agent-service/app/adapters/agents/htp/tool_registry.py`  
**依赖**：T-TOOL-10（DB 中有数据），T-TOOL-19/20（import 路径更新）

**背景**：当前文件约 490 行硬编码 `ToolDefinition` 对象。v2.0 改为启动时从 `tool_definition` 表加载。

**旧代码保留策略**：
- 旧的 `TOOL_REGISTRY` 字典（硬编码部分）**整体删除**
- `get_tools_for_llm()` 函数逻辑不变（遍历 TOOL_REGISTRY，过滤 policy!=block）
- 新增 `load_tool_registry(db)` 异步函数

**新文件核心内容**：

```python
"""
tool_registry.py v2.0：工具注册表，从 tool_definition 数据库表加载。

SSOT：tool_definition 表持有"工具 IS 什么"（LLM 接口描述、参数 schema）。
代码持有"工具 DOES 什么"（执行路由、RiskClassifier）。
"""
from __future__ import annotations

import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.tools.base_tool import ToolDefinition
from app.tools.acli.classifier import risk_to_policy
from app.database.models import ToolDefinitionORM  # 确认实际 ORM 模型路径

logger = logging.getLogger(__name__)

# 运行时注册表，由 lifespan 初始化
TOOL_REGISTRY: dict[str, ToolDefinition] = {}


async def load_tool_registry(db: AsyncSession) -> dict[str, ToolDefinition]:
    """启动时从 tool_definition 表加载所有激活工具。"""
    result = await db.execute(
        select(ToolDefinitionORM).where(ToolDefinitionORM.is_active == True)
    )
    registry = {}
    for row in result.scalars():
        registry[row.tool_name] = ToolDefinition(
            name=row.tool_name,
            description=row.description,
            parameters=row.parameters_schema,
            risk_level=row.risk_level,
            policy=risk_to_policy(row.risk_level),
            category=row.category,
        )
    logger.info(f"已加载工具注册表：{len(registry)} 个工具（{list(registry.keys())}）")
    return registry


def get_tools_for_llm() -> list[dict]:
    """
    返回可供 LLM 调用的工具列表（过滤掉 policy=block 的工具）。
    OpenAI function call 格式。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in TOOL_REGISTRY.values()
        if tool.policy != "block"
    ]
```

**同步修改 `main.py` lifespan**：
```python
from app.adapters.agents.htp.tool_registry import load_tool_registry, TOOL_REGISTRY

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... 其他初始化 ...
    global TOOL_REGISTRY
    async with get_db_session() as db:
        loaded = await load_tool_registry(db)
        TOOL_REGISTRY.update(loaded)
    yield
```

**验收标准**：
- [ ] 启动时日志显示 "已加载工具注册表：13 个工具"
- [ ] `get_tools_for_llm()` 返回非空列表
- [ ] `tool_registry.py` 中无硬编码 `ToolDefinition(name="acli_vm_list", ...)` 等旧代码
- [ ] 启动后 `TOOL_REGISTRY` 包含 `acli_exec`、`bash_exec` 等 13 个工具

---

### T-TOOL-15：`htp/react_engine.py` 适配 RiskClassifier 动态风险覆盖

**优先级**：P1  
**文件**：`backend/agent-service/app/adapters/agents/htp/react_engine.py`  
**依赖**：T-TOOL-11，T-TOOL-14

**背景**：`acli_exec`/`bash_exec` 的 `risk_level` 在 DB 中是静态默认值 1，实际执行前需要由 RiskClassifier 根据命令内容动态判定，判定结果覆盖 DB 默认值后传给 `BridgeRelayExecutor`。

**在 `_execute_tool_call()` 方法中新增**（找到现有方法，在执行前插入）：

```python
from app.tools.acli.classifier import classify_acli, classify_bash, risk_to_policy

async def _execute_tool_call(self, tool_name: str, tool_args: dict) -> str:
    tool_def = TOOL_REGISTRY.get(tool_name)
    if not tool_def:
        return f"[error] 工具 {tool_name!r} 不存在或已禁用"

    # ─── 动态风险覆盖（仅对通用命令执行工具）───
    if tool_name in ("acli_exec", "bash_exec"):
        command = tool_args.get("command", "")
        runtime_risk = (
            classify_acli(command) if tool_name == "acli_exec"
            else classify_bash(command)
        )
        tool_def = tool_def.model_copy(update={
            "risk_level": runtime_risk,
            "policy": risk_to_policy(runtime_risk),
        })
        if tool_def.policy == "block":
            return (
                f"[blocked] 命令 {command!r} 属于高危操作（risk=3），已拒绝执行。"
                f"请改用更安全的命令，或向用户说明无法执行此操作。"
            )
    # ─────────────────────────────────────────

    # ... 后续正常执行逻辑（传入 tool_def 给 executor）...
```

**注意**：确认 `model_copy` 是 Pydantic v2 语法（`ToolDefinition` 若是 BaseModel 则可用），Pydantic v1 对应 `copy(update=...)`。

**验收标准**：
- [ ] `acli vm delete xxx` 触发 block，LLM 收到 `[blocked]` 提示
- [ ] `acli vm list` 不触发额外确认（risk=1，auto）
- [ ] `acli service xxx restart` 触发 confirm（risk=2，进入确认流程）
- [ ] `bash_exec("rm -rf /tmp")` 触发 block

---

### T-TOOL-16：`main.py` `CompositeToolExecutor` 路由 acli category

**优先级**：P1  
**文件**：`backend/agent-service/app/main.py`（或 `CompositeToolExecutor` 所在文件）  
**依赖**：T-TOOL-12，T-TOOL-14

**背景**：`CompositeToolExecutor` 根据 `tool_def.category` 路由到不同执行后端。v2.0 需要新增 `category="acli"` → `BridgeRelayExecutor` 的路由。

**修改内容**（在现有路由逻辑中新增）：

```python
from app.tools.acli.executor import BridgeRelayExecutor

class CompositeToolExecutor:
    def __init__(self, ...):
        # 新增 BridgeRelayExecutor 实例化
        self.bridge_executor = BridgeRelayExecutor(
            redis=redis,
            conversation_service_url=settings.CONVERSATION_SERVICE_URL,
            internal_token=settings.INTERNAL_SERVICE_TOKEN,
        )

    async def execute(self, tool_name: str, args: dict, *, tool_def: ToolDefinition, ...) -> str:
        match tool_def.category:
            case "scp":
                return await self.scp_executor.execute(tool_name, args)
            case "sop":
                return await self.sop_executor.execute(tool_name, args)
            case "acli":
                # bash_exec 和 acli_exec 及插件工具均走此路径
                result = await self.bridge_executor.execute(
                    tool_name, args,
                    conversation_id=self.conversation_id,
                    node_ip=args.get("node_ip"),
                    risk_level=tool_def.risk_level,
                    policy=tool_def.policy,
                )
                return result.stdout or f"[exit_code={result.exit_code}]"
            case _:
                return f"[error] 未知工具类型 category={tool_def.category!r}"
```

**验收标准**：
- [x] `category="acli"` 的工具调用能到达 `BridgeRelayExecutor.execute()`
- [x] `BridgeRelayExecutor` 实例化参数（redis、URL、token）来自 settings/env

---

## Phase 3：P2 任务（清理与优化）

---

### T-TOOL-17：确认 `acli_client.py` 废弃注释（已完成）

**优先级**：P2  
**文件**：`backend/agent-service/app/adapters/clients/acli_client.py`  
**依赖**：无

**状态确认**：本 session 中已在 `_run_ssh()` 方法添加废弃 docstring + `warnings.warn(DeprecationWarning)`。  
**执行操作**：仅需 review 确认内容正确，无需额外修改。

**验收标准**：
- [ ] `acli_client.py` 模块 docstring 明确说明废弃原因（私网不可达）和新路径（BridgeRelayExecutor）
- [ ] `_run_ssh()` 方法有 `warnings.warn("...", DeprecationWarning, stacklevel=2)`

---

### T-TOOL-18：前端 `InteractiveRequestCard` 支持 `exec_confirm` 类型

**优先级**：P2（T-TOOL-04 中 risk=2 可先用 `window.confirm()` 临时替代）  
**文件**：`frontend/src/components/InteractiveRequestCard.vue`（或对应组件）  
**依赖**：T-TOOL-04

**功能要求**：
- 展示命令内容（等宽字体代码块）
- 展示执行原因（`reason` 字段）
- 展示目标节点 IP
- 确认按钮（绿色）/ 拒绝按钮（红色）
- 10 分钟无响应自动拒绝（显示倒计时）

**UI 参考**（参考现有 `VariableRequestCard.vue` 或 `ConfirmCard.vue` 的样式规范）：

```
┌─────────────────────────────────────────┐
│  ⚠️  Agent 请求执行以下命令               │
│                                         │
│  目标节点：192.168.1.10                  │
│  执行原因：检查服务状态                    │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ acli service asv redis restart  │   │
│  └─────────────────────────────────┘   │
│                                         │
│  [拒绝]                    [确认执行]    │
└─────────────────────────────────────────┘
```

**验收标准**：
- [ ] 组件能渲染（无 TypeScript 错误）
- [ ] 点击"确认执行"后 Promise resolved(true)
- [ ] 点击"拒绝"后 Promise resolved(false)
- [ ] 10 分钟超时后 Promise resolved(false)

---

## 测试验收：端到端冒烟测试

> 所有 Phase 1 + Phase 2 任务完成后，执行以下手动验证流程。

### 场景 A：只读命令（risk=1，全自动）

1. 在前端打开一个 case，建立 SSH 连接
2. 向 Agent 发送：`查看当前所有 VM 的列表`
3. **预期**：Agent 自动调用 `acli_exec("acli vm list --formatter json")`，无需用户确认，结果自动返回给 LLM，LLM 输出 VM 列表摘要

### 场景 B：写操作命令（risk=2，需要确认）

1. 向 Agent 发送：`重启 redis 服务`
2. **预期**：Agent 调用 `acli_exec("acli service asv redis restart")`，前端弹出确认卡片，展示命令内容
3. 用户点击"确认执行"
4. **预期**：命令执行，结果返回 LLM，LLM 输出重启结果

### 场景 C：高危命令（risk=3，自动拒绝）

1. 向 Agent 发送（或模拟 LLM 产生）：`删除 vm-123`
2. **预期**：`acli vm delete vm-123` 被 RiskClassifier 识别为 risk=3，`react_engine` 返回 `[blocked]` 给 LLM，LLM 告知用户无法执行此操作

### 场景 D：DB SSOT 验证

1. 在 DB 中临时修改某工具的 `description`：
   ```sql
   UPDATE tool_definition SET description = '测试热更新' WHERE tool_name = 'acli_exec';
   ```
2. 重启 agent-service（触发重新加载）
3. **预期**：`get_tools_for_llm()` 返回的 `description` 已更新，无需改代码

---

## 文件索引

| 任务 | 主要涉及文件 |
|------|------------|
| T-TOOL-08/09/10 | `database/desired_schema.sql`、`database/migrations/`、`database/seeds/` |
| T-TOOL-01/02 | `terminal_bridge/main.go` |
| T-TOOL-03/04/18 | `frontend/src/api/terminal.ts`、`frontend/src/stores/chat.ts`、`frontend/src/components/InteractiveRequestCard.vue` |
| T-TOOL-05/06/07 | `backend/conversation-service/`（路由 + SSE 推送） |
| T-TOOL-11 | `backend/agent-service/app/tools/acli/classifier.py`（新建） |
| T-TOOL-12 | `backend/agent-service/app/tools/acli/executor.py`（新建） |
| T-TOOL-13 | `backend/agent-service/app/tools/acli/__init__.py`（新建） |
| T-TOOL-14 | `backend/agent-service/app/adapters/agents/htp/tool_registry.py`（重写） |
| T-TOOL-15 | `backend/agent-service/app/adapters/agents/htp/react_engine.py`（修改） |
| T-TOOL-16 | `backend/agent-service/app/main.py`（修改） |
| T-TOOL-17 | `backend/agent-service/app/adapters/clients/acli_client.py`（确认） |
| T-TOOL-19/20 | 目录/文件重命名操作 |
