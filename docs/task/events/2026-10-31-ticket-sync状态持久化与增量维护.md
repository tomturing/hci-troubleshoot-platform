# ticket-sync 状态持久化与增量维护（2026-10-31，第二轮）

补齐 PR #1395 之后剩的两项结构性缺口。

## 背景

复盘时暴露了两个"每次都要重算/每次都会失忆"的设计问题：

1. **任务状态只存内存**。服务一重启，`/jobs`、`/artifacts` 就看不到任何历史；
   "这个工单上次同步成功没"只能翻 journal 反推。上一轮排查 38 个工单时，
   这个缺口让取证成本翻了几倍。
2. **容量判断每次遍历目录**。即使有了 `.sync-meta.json` 把体积查询降到读文件，
   仍然是"每天对每个工单重新问一遍"。工单体量只增不减，成本随规模线性上升。

## 改动

### SQLite 存储（`tools/ticket-sync/app/store.py`）

单文件库 `<产物根>/.ticket-sync.db`，WAL 模式，两张表：

| 表 | 主键 | 用途 |
|---|---|---|
| `jobs` | `ticket_id` | 任务快照：阶段、进度、错误码、时间戳 |
| `artifacts` | `ticket_id` | 产物用量：`bytes`/`files`/`has_logs`/`completed_at`/`dir_mtime` |

设计边界：

- 内存 `JobRegistry` 仍是运行态权威，DB 只是它的持久化镜像，不参与并发控制；
- 每次阶段变更写一条 upsert（WAL 下单条约 0.1ms，与"全盘 walk 3.5 分钟"不是一个量级）；
- 历史只保留最近 500 条，避免表无限增长；
- 连接以 `check_same_thread=False` + 内部锁串行化，因为维护任务跑在线程池里。

### `interrupted` 阶段（`tools/ticket-sync/app/models.py`）

重启时把遗留的 `queued/fetching/extracting` 改写为 `interrupted`。

**必须与 `failed` 区分**：中断不是同步失败，重新触发通常就能成功；
若混用 `failed`，用户会照着"排查日志源"的方向白费功夫。

前端同步补了类型与标签样式（`frontend/admin/src/api/ticketSync.ts`、
`frontend/admin/src/views/DshDiagnosisView.vue`），且 `isRunning` 走白名单，
`interrupted` 天然不会被当成"仍在推进"而触发轮询。

### 增量对账（`tools/ticket-sync/app/maintenance.py`）

`reconcile_artifacts` 用**目录 mtime 作为"自上次记账后有没有被写过"的信号**：

- 未变化 → 跳过，既不读 meta 也不扫体积
- 变化 → 读 meta 更新 `artifacts` 行
- 磁盘已消失 → 删除对应行

这里刻意只把 mtime 当"变化信号"、不当"同步时间"——后者会被 DSH 写报告、
人工 `touch` 改写，前者不受影响（重新发布一定会改 mtime）。

容量判断改为 SQL 聚合 + `ORDER BY has_logs ASC, completed_at ASC`，
不再依赖目录遍历。删不掉的行会保留（否则总量算少、这批数据永远轮不到再删）。

### 保留期与淘汰的记账同步

`cleanup_expired` / `enforce_capacity` 增加 `on_removed` 回调，
产物删除时同步清理记账行与历史，避免 DB 与磁盘长期漂移。

## 验收

- `71 passed`（新增 9 个：重启改判 interrupted、历史跨重启可查、
  内存优先于历史、增量对账三种分支、store 淘汰顺序）
- 兼容性：未传 store 时 `enforce_capacity` 仍走纯扫描，既有测试与
  "DB 不可用"降级路径不受影响

## 遗留

- `artifacts` 表没有独立的"体积趋势"历史，只有当前值；如需容量规划趋势，
  要另建时序表或依赖 `/metrics` 抓取。
- 任务历史保留 500 条且无归档，跨月追溯需要提高上限或导出。
