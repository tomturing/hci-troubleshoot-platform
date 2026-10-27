---
status: accepted
date: 2026-10-27
deciders: team
---

# Terminal Bridge 会话租约改造方案

## 状态

**已采纳**（2026-10-27）

## 背景与故障

2026-10-27 14:37:49 现场故障链：浏览器刷新 / 网络抖动导致某条 WebSocket 连接关闭，
旧实现在 WS 关闭的 defer 里直接 `destroySession` 关闭并摘掉全部该连接持有的 SSH 会话。
但其中部分会话正被"日志收集面板"复用，于是面板正在执行的 acli 采集被连坐清理，
采集任务失败。故障表现为"会话莫名其妙没了"，且无 `reason`/持有者留痕，事后无法回答
"谁、何时、为何"三问。

## 第一性原理解构（阶段一）

会话的生命周期不应由"某一条浏览器连接是否还在"单一事件决定。会话的真实持有者可能是
多条连接（终端连接 + 日志面板连接），只有"全部持有者都走了"会话才应空闲。因此：

- 引入**引用计数** `sessionRefs`：会话入表即登记一个持有者（refs=1），连接关闭时减 1，
  减到 0 才进入空闲；
- 引入**空闲租约** `sessionIdleAt`：refs 归零时记录时刻，进入 TTL 宽限期，不再立即销毁；
- **销毁收敛到单一入口** `destroySession`：close + 摘表 + 留痕三件事原子完成，所有
  reason（ws_closed / reconnect / remote_eof / client_disconnect / keepalive_failed / idle_ttl）
  统一产出 `session.destroyed` 事件；
- **回收器** `startSessionReaper`：周期性扫描空闲超 TTL 的会话并回收，兜底清理"连接都走了
  但会话还活着"的那一类。

硬性约束：远端已 EOF 的会话由 `onExit` 先清掉，reaper 只处理"连接都走了但会话还活着"；
显式 `ssh_disconnect` 与远端 EOF 仍走 `destroySession` 立即销毁（用户意图 / 远端判定，
不可被租约宽限）。

## 对抗性审查（阶段二）

- **刷新瞬断（主场景）**：终端 WS 关 → releaseSession → refs=0 → 空闲；用户在 TTL(默认 300s)
  内刷新重连 → 复用同一会话 → 面板不被连坐。✅ 已覆盖。
- **盲区（已知、留作后续）**：日志面板复用会话时只经 `resolveSession` 取用，**不调用
  `holdSession` 登记持有权**。若终端标签页彻底关闭、且面板的长耗时 acli 采集超过 TTL，
  reaper 仍会回收该会话（面板 `refs=0`）。本改造的宽限期覆盖"瞬断重连"，但不覆盖
  "终端长期关闭 + 面板长跑 > TTL"。修复需让面板借用会话时也 `holdSession` 并在结束时
  `releaseSession`——属后续独立 PR。
- **TTL=0 退化**：`HCI_BRIDGE_SESSION_IDLE_TTL_SECONDS<=0` 时 reaper 不启动，退化为"永不主动
  回收"，仅依赖远端 EOF / 显式断开。仅调试用。
- **reaper 抢锁**：扫描间隔取 TTL/4（最少 15s），Atom 级机器上不会频繁抢锁；victim 列表在锁
  外逐个 `destroySession`，避免持锁做 IO。

## 改造方案（阶段三）

- `lifecycle.go`：新增 `holdSessionLocked` / `releaseSession` / `forgetLease` / `reapIdleSessions` /
  `sessionIdleTTL` / `startSessionReaper`，常量 `sessionReasonIdleTTL`、默认值
  `defaultSessionIdleTTL=5min`；
- `main.go`：`Bridge` 结构体加 `sessionRefs` / `sessionIdleAt`；`newBridge` 初始化；`set()`
  入表即 `holdSessionLocked`；WS 关闭 defer 由 `destroySession` 改为 `releaseSession`；启动时
  `startSessionReaper(bridge, sessionIdleTTL())`。

## 验收标准（阶段四）

- 单元/集成：复用 `terminal_bridge` 现有测试套件，`go build` / `go vet` / `gofmt` 干净；
- 可观测：刷新页面后 `session.idle` 出现、原会话在 TTL 内可复用、超时才出现
  `session.destroyed{reason=idle_ttl}`；
- 不复现：2026-10-27 14:37:49 类"面板采集被连坐清理"故障不再出现。

## 订正记录

### 2026-10-27：初版实现存在锁重入自死锁（CI 拖到 10m17s 超时才失败）

**原始断言（保留不删）**：上文《改造方案》写的是「`set()` 入表即 `holdSession`」，
当时 `holdSession` 自己也会 `b.mu.Lock()`。

**已被证伪的部分**：Go 的 `sync.Mutex` **不可重入**。`set()` 已在 `b.mu` 临界区内再调
`holdSession()` → 自己等自己放锁，任何一次建会话永久阻塞。表现不是崩溃而是“慢”：
`go test` 默认 10 分钟才 panic，而 `terminal bridge simulation-auth gate` 的
`timeout-minutes` 也是 10，于是它在 `format, test and vet` 步骤上卡满 10m17s 被杀，
把死锁伪装成了“CI 很慢”。

**修正依据与结果**：按 Go 惯例把它改名为 `holdSessionLocked`（后缀即“调用方必须已持锁”），
删除函数内部的加锁，`set()` 改为调用 `holdSessionLocked`。原卡死的
`TestDestroySessionEmitsLifecycleEvent` 从永久阻塞变为 0.00s 通过，
`go test ./...`（9.9s）与 `go test -race ./...`（11.2s）全部通过。
同时逐个复核了 `lifecycle.go` 其余加锁函数：`releaseSession` 在锁外 `blog`；
`reapIdleSessions` 先 `Unlock` 再调 `destroySession`；`destroySession` 自身不持锁——
均无同类隐患。验收标准补一条：**锁内绝不调用会再加锁的方法，需要持锁的辅助函数一律以
`Locked` 后缀命名**。
