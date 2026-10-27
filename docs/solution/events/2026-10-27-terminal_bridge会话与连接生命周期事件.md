---
status: implemented
category: solution
audience: developer
last_updated: 2026-10-27
owner: 平台组
related:
  - 2026-09-20-terminal_bridge日志治理与上传补采方案.md
  - 2026-10-26-日志收集会话存活与保活修复.md
---

# terminal_bridge 会话与连接生命周期事件（可定因补全）

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-10-27 | v1.0 | 初版：补齐 conn/session 生命周期事件、进程退出与 panic 留痕、stderr 落盘、面板通道回采转发、output_preview 落库 | 无独立任务文档 |

---

## 一、现场事实（2026-10-27，Q2026102701197 / 归档 Q2026102700862）

客户机 `DESKTOP-1AKSJIS`（Atom E3845 / 3.9GB / 连续运行 581 天）上，日志收集反复报
`SSH 会话不存在（需先 ssh_connect）`。本地 `bridge-20261027-9d97fb0c.log` 共 1040 行，
但**排查所需的四类证据全部缺失**：

| 想知道 | 日志里有什么 | 缺什么 |
|---|---|---|
| 06:37:49 谁销毁了会话 | 只有 `active_sessions=0` / `session_keys=[]` | 没有销毁者、没有原因、没有存活时长 |
| 远端 shell 是否退出过 | 只有 WS 消息 `ssh_disconnected`（不是事件） | 没有结构化事件与退出原因 |
| 14:58 进程怎么没的 | 日志戛然而止（最后 3 条只有 pong） | 无 `bridge.shutdown`、无 panic 记录、stderr 未落盘 |
| 19 次 connected / 17 次 disconnected | remote 字段全都一样 | 无 `conn_id`，无法配对；探测连接与业务连接不可区分 |

字段完整度实测：**1040 行中 456 行（44%）无 `case_id`**；`bridge.收到消息` 219 条与
`bridge.resolveSession` 158 条全部无 `case_id`、`extra` 为空（纯文本，云端不可检索）。

云端另有结构性缺失：回采通道 `bridge → WS → 浏览器 → 后端` 依赖浏览器在线，
断线窗口内的事件无人转发；日志收集面板自建 WS 收到 `bridge_log` 直接 return，
面板通道上的桥日志从未进入回采链路。

## 二、根因

1. 会话销毁散落在 5 处（连坐清理、同键覆盖、远端 EOF、显式断开、自动连接回滚），
   各自 `log.Printf`，字段不一 → 「谁销毁了会话」不可查；
2. 连接没有标识与角色 → connected/disconnected 无法配对，端口探测与真实断连同形；
3. 进程退出与 panic 无留痕（`log.SetOutput` 只接管 log 包，不接管 stderr）；
4. 面板通道不转发桥日志；落库把 `output_preview` 恒写 NULL。

## 三、改动

| 位置 | 改动 |
|---|---|
| `terminal_bridge/lifecycle.go`（新增） | `connID` 生成、会话销毁唯一入口 `destroySession`、`session.created`、`resourceSnapshot`/采样器、`redirectStderrToFile` |
| `terminal_bridge/main.go` | `handle()` 生成 connID/role 并写入 connected/disconnected；5 处销毁统一走 `destroySession`；`on_output_start` 增加结束原因并记 `ssh.disconnected`；`收到消息`/`resolveSession` 降为 DEBUG；main 增加 panic recover、`bridge.shutdown`、stderr 重定向、资源采样 |
| `frontend/customer/src/components/LogCollectPanel.vue` | 面板 WS 收到 `bridge_log` 转发给 `chatStore.forwardBridgeLog`（此前直接 return） |
| `frontend/customer/src/stores/chat.ts` | 按 `event_id` 去重（同事件会被终端与面板两条 WS 各推一份） |
| `backend/conversation-service/app/routes/bridge_logs.py` | `output_preview` 从 `extra` 提取并截断 4096 字符落库（此前恒为 NULL） |

### 事件契约（新增/改造）

| 事件 | 级别 | 关键字段 | 回答什么 |
|---|---|---|---|
| `session.created` | INFO | `key, conn_id, role, source, address, simulation` | 谁建的会话、走的哪条路径 |
| `session.destroyed` | WARN | `key, reason, by_conn, owner_conn, age_seconds, active_sessions, session_keys` | **谁销毁、为什么、活了多久** |
| `session.destroy_miss` | WARN | `key, reason, by_conn, active_sessions` | 清理链路是否被走了两次 |
| `ssh.disconnected` | WARN | `reason(remote_eof/local_close), exit_error, duration_seconds` | 是节点断的还是我们关的 |
| `conn.role` | INFO | `conn_id, role(terminal/panel)` | 探测连接与业务连接归类 |
| `bridge.shutdown` | WARN | `signal, uptime_seconds, active_sessions` | 正常退出 vs 被终结 |
| `bridge.panic` | ERROR | `panic, stack(4KB)` | 崩溃原因（配合 stderr 文件完整栈） |
| `resource.sample` | INFO | `goroutines, heap_alloc_mb, sys_mb, num_gc, log_ring, log_seq` | 是否资源枯竭 |

`reason` 取值：`ws_closed` / `reconnect` / `remote_eof` / `client_disconnect` / `local_close`。

### 降噪口径

- `bridge.收到消息` → `ws.message`（DEBUG，默认 INFO 级别不落盘）：现场 219 条噪音全部消失；
- ping/pong 完全不记：由 WS 心跳承担，不是业务事实；
- `bridge.SSH_认证成功` 的 `log.Printf` 删除（与 `ssh.connected` 重复）。

### stderr 落盘

panic / runtime fatal 只写 stderr，而 `log.SetOutput` 只接管标准库 log 包。
新增 `<日志目录>/bridge-stderr.log`（权限 0600），控制台窗口被关闭后仍可取证。

## 四、边界（本 PR 明确不做）

1. **会话租约**（WS 关闭不销毁会话）未做——本次只补证据链，不改变生命周期语义；
2. **bridge 直传日志**（不依赖浏览器）未做——需要前端下发短期 token，属于下一批；
3. `output_preview` 只补落库，不改变桥侧截断口径（仍为 512 字符）。

## 五、验收

1. 断连复现：`session.destroyed{reason=ws_closed, by_conn=cX-Y, age_seconds=N}` 与
   `bridge.disconnected{conn_id, role, owned_keys}` 成对出现；
2. 远端 `exit`：`ssh.disconnected{reason=remote_eof}` + `session.destroyed{reason=remote_eof}`；
3. Ctrl+C：`bridge.shutdown{signal=interrupt, uptime_seconds}`；
4. 强制结束进程：出现 `bridge.startup` 而无 `bridge.shutdown`（可被云端规则识别）；
5. 面板与终端同时在线：桥日志只落一份（`event_id` 去重）；
6. 云端 `bridge_execution_logs.output_preview` 非空（`log_collect.create` 失败时有 acli 原文）。

## 六、回滚

纯代码回滚，无 schema 变更；新增事件对旧前端无影响（未知 `event` 被忽略）。
