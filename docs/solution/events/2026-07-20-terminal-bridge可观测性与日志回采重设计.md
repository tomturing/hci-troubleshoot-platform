---
status: active
category: event
audience: developer
date: 2026-07-20
related_prs: []
owner: team
---

# terminal_bridge 可观测性与日志回采重设计（OBS-TERMINAL-BRIDGE-001）

> **关联任务** → [Agent可靠性改造任务清单 v3.19](../../task/agent/Agent可靠性改造任务清单.md)
>
> **关联方案** → [数据库设计 §2.9 bridge_execution_logs](../../数据库设计.md)

## 1. 背景与问题

- **工单 Q2026071923606**：QFK 诊断信号在 terminal_bridge 未真正执行命令时产生**假阳性**——命令因「SSH 会话缺失 / 桥未运行 / 超时」根本没在 HCI 主机上跑，返回空输出，而 `match_mode="not"`（或 `expected=False`）信号把「无关键字 = 无输出」误判为「关键字缺失 → 符合排查判定」，给出错误结论。
- **排障黑盒**：terminal_bridge 仅有非结构化 `log.Printf`（如 `EXEC_START` / `EXEC_DONE`），无法按工单 / trace / 节点聚合，问题溯源只能靠人工逐行翻控制台。
- **无统一链路**：Custom-UI → Bridge → Agent 三段之间缺少贯穿的 `trace_id`，一次诊断为何失败无法端到端串联。

## 2. 设计原则（第一性原理 + 业界范式）

1. **统一可观测性**：所有日志结构化为 JSON，携带 `trace_id` / `case_id` / `node_ip` / `custom_ui` 标签，纳入平台整体链路。
2. **回采（back-collection）**：日志经 WebSocket 以 `bridge_log` 消息实时推给已连接的 Custom-UI 浏览器，由前端统一 POST 到其后台「回采接口」落库（工单关联）。Bridge 为通用代理，**不感知后台地址**。
3. **多工单并行 / 串行**：会话本就以 `caseID@nodeIP` 为键，日志按 `case_id` 归属与回放。
4. **异常重传**：环形缓冲保留近期日志，浏览器（重）连接并 `ssh_connect` 时回放；发送失败暂存 `pending`，连接恢复后重传；可选落本地文件（`HCI_BRIDGE_LOG_DIR`）供进程重启回放。
5. **来源归属**：每个浏览器连接按 `Origin` 自动归属 `custom_ui`（如 `hci.local` / `acli.sangfor.com.cn:4443`），无需前端显式传参。

## 3. 方案

### 3.1 terminal_bridge 结构化日志重写（main.go）

- 新增 **LogHub** 中枢：全局有界环形缓冲（5000 条）+ 多订阅者 + 结构化落盘。
- `bridgeLogWriter` 把标准库 `log` 输出重定向为结构化日志并回采，`log.Printf("[Bridge] ...")` 零改造即被捕获（保留控制台原文便于本地调试）。
- `blog(level, event, msg, trace_id, case_id, node_ip, custom_ui, extra)` 显式结构化日志，事件名：`ssh.connected` / `exec.start` / `exec.done` / `exec.session_missing` / `error`。
- `InMessage` 增加 `trace_id`；`OutMessage` 增加 `trace_id` / `custom_ui` 回显字段。
- `corsWebSocketHandler` 升级为方法，按 `Origin` 派生 `custom_ui` 并注册回采订阅者；`ssh_connect` 成功后 `logHub.setCase(sub, caseID)` 归属工单并回放近期日志。

### 3.2 端到端 trace_id 透传

- `acli/executor.py`：`trace_id = get_current_trace_id() or exec_id`（无 OTel trace 时回退到 exec_id，保证端到端可关联），并透传进 conversation-service 推送载荷。
- `conversation-service/routes/agent_exec.py`：`AgentExecRequest` 增加 `trace_id`，SSE `event_data` 携带 `traceId`。
- `frontend/stores/chat.ts`：解构 `traceId` → `buildAgentExecProcessMessage(caseId, execId, command, nodeIp, container, traceId)`。
- `frontend/api/terminal.ts`：消息类型新增 `bridge_log`，`TerminalWsMessage` 增加 `trace_id` / `seq` / `ts` / `level` / `event` / `custom_ui` 等字段。
- 一条诊断链路从 Custom-UI → Bridge → Agent 共享同一 `trace_id`。

### 3.3 日志回采与落库

- 前端 `stores/chat.ts`：`bridge_log` 消息 → `forwardBridgeLog` → 缓冲批量 POST `/api/bridge-logs`（500ms 聚合，发送失败 2s 重传）；无 `case_id` 的日志不落库。
- 后端 `conversation-service/routes/bridge_logs.py`：实现 **真实 Session 鉴权**（Bearer token 校验：本地 HMAC 签名校验 / 或回拨可配置网关 verify URL），提取 `user_id` 用于审计；批量写入 `bridge_execution_logs`。
- 表 `bridge_execution_logs`（`desired_schema.sql` + 迁移 `005_bridge_execution_logs.sql`）：`case_id` / `trace_id` / `custom_ui` / `user_id` / `node_ip` / `level` / `event` / `message` / `extra`（jsonb），含 3 个索引（工单+时间、trace、custom_ui）。

### 3.4 QFK 终端失败哨兵（qfk/engine.py）

- 命令「在桥上跑过但未真正落到主机」的失败显式化：识别终端级失败哨兵（`SSH 会话不存在` / `需先 ssh_connect` / `execution timeout` / `终端桥未运行` 等）且 `exit_code not in (0, None)`，直接判 `QFKResult(matched=False, error=...)`，**不进入关键字 evaluate**，并给出可操作错误（请先通过 Custom-UI 建立 SSH 连接）。
- 从根上消除 `match_mode="not"` / `expected=False` 信号的假阳性（见工单 Q2026071923606）。

## 4. 涉及改动清单

| 模块 | 文件 | 改动 |
|------|------|------|
| terminal_bridge | `terminal_bridge/main.go` | LogHub 结构化日志中枢、回采订阅者、重传、trace_id/custom_ui 透传 |
| agent-service | `app/tools/acli/executor.py` | trace_id 回退与端到端透传 |
| agent-service | `app/tools/qfk/engine.py` | 终端失败哨兵，消除 negation 假阳性 |
| conversation-service | `app/routes/agent_exec.py` | SSE 透传 trace_id |
| conversation-service | `app/routes/bridge_logs.py`（新增） | `/api/bridge-logs` 回采接口 + 真实 Session 鉴权 |
| frontend | `src/api/terminal.ts` | `bridge_log` 消息类型 + trace_id 字段 |
| frontend | `src/stores/chat.ts` | 消费 bridge_log + 缓冲回采 + trace 透传 |
| database | `desired_schema.sql` + `data-migrations/005_bridge_execution_logs.sql` | 新增 bridge_execution_logs 表 |
| docs | `docs/solution/数据库设计.md` | §2.9 新增表说明（v7.17） |

## 5. 验证

- CI 门禁：ruff（后端）、frontend test+build、db-migration-test、docs-governance（docs 更新）。
- 端到端手验：Custom-UI 连 Bridge → `ssh_connect` → Agent 触发诊断 → 结构化日志回采落库 → 按 `case_id` / `trace_id` 查询。

## 6. 关联

- [Agent可靠性改造任务清单 v3.19](../../task/agent/Agent可靠性改造任务清单.md)
- [数据库设计 §2.9 bridge_execution_logs](../../数据库设计.md)
