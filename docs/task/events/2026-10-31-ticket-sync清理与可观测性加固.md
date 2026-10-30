# ticket-sync 清理与可观测性加固（2026-10-31）

## 背景

对 `hci-ticket-sync` 做一次「定期清理是否真的工作 + 全部工单有没有遗漏问题」的复盘，
结论是**清理逻辑本身正确，但判据、执行方式和产物语义都有结构性问题**。

### 清理逻辑验证（结论：功能正常）

| 验证项 | 结果 |
|---|---|
| 是否每天执行 | 每天首次巡检触发，`2026-10-31 00:00:07` 有记录 |
| 过期目录是否真删 | 造一个 10 天前的 `Q0000000000001`，重启后 `removed=1` 且目录消失 |
| `removed=0` 是否等于失效 | 否：存量最早目录是 10-29，7 天保留期下确实无过期对象 |

## 发现的问题

### P0-1 维护任务把事件循环钉死 3.5 分钟

`2026-10-31 00:00:07` 打完「保留期检查完成」后，直到 `00:03:35` 才打「容量检查完成」，
**中间 3.5 分钟服务完全静默**，同期多个 `/artifacts` 请求全部堆积在 00:03:35 才返回。

根因：`prune_capacity` 在协程里直接调用同步的 `os.walk + lstat`，统计 550 GiB / 44 万文件。
这段代码同时持有 `JobRegistry._lock`，连前端查询都被一起挡住；`/health` 也不响应。

影响：外部监控若对 `/health` 设 5s 超时，会误判服务宕机；自动巡检整轮停摆；
冷缓存（重启后首日）才是常态，因此这是每天都会发生的事。

### P0-2 「就绪」与「产物可用」脱节

`Q2026102101911` 下载 16.4 GiB、解压后 65 GiB，产物只有一个 `kasi/kasi.dump`，
状态却是 `ready`。用户点进去无日志可看，65 GiB 白占配额。

### P0-3 非日志载荷识别覆盖不足

`looks_like_non_log_bundle` 只认 `vmdumpcore` 与 `.core`，`kasi.dump` 全部放行。
判定还只读顶层 zip 中央目录，不覆盖嵌套结构。

### P1-4 保留期判据用目录 mtime

mtime 只反映"直接子项最后一次增删"，DSH 在产物里生成报告、人工 `touch` 都会改写它。
它既不等于"何时同步完"，也可能让刚同步的工单被误删。

### P1-5 容量淘汰不区分价值

按"最旧优先"淘汰，一个 65 GiB 的 dump 工单会挤掉多个可用日志工单。

### P2-6 无指标暴露

只有 `/health`，没有可告警的时间序列，维护耗时这类关键信号完全不可见。

## 改动

### 显式同步元数据（`tools/ticket-sync/app/maintenance.py`、`tools/ticket-sync/app/main.py`）

发布成功后写 `<工单目录>/.sync-meta.json`：`completedAt` / `bytes` / `files` / `hasLogs`。

- 保留期优先读 `completedAt`，无 meta 的历史目录回退 mtime
- 容量统计优先读 `bytes`，缺失才扫一次并回填
- **回填后必须恢复目录 mtime**：新增 meta 文件会把 mtime 刷成当前时间，
  历史目录因此被无限续命（这是实现过程中被测试抓出来的缺陷）

### 阻塞 IO 移出事件循环（`tools/ticket-sync/app/main.py`）

`prune_expired` / `prune_capacity` 改为 `asyncio.to_thread`，且不再持有注册表锁。
删除目标已排除活跃工单，发布是原子 rename，并发上最坏只多删一个旧目录。

### 淘汰顺序按价值（`tools/ticket-sync/app/maintenance.py`）

`(hasLogs=false 优先, 同步时间最旧优先)` 二级排序。

### 载荷识别扩展（`tools/ticket-sync/app/fetcher.py`）

新增 `vmcore` / `kdump` / `.dump` / `.dmp` / `crash-` 特征；`log_list.json` 改为后缀匹配
（实际条目带路径前缀）。三条判定同时满足才跳过，避免误伤正常日志包。

### 产物可用性提示（`tools/ticket-sync/app/main.py`）

发布后检查产物是否含 `log_list.json` 或 `sp_*`（只下探工单号形态的内层目录），
没有就在 `job.message` 明确提示"可能仅含转储文件"。

### 指标（`tools/ticket-sync/app/main.py`）

新增 `GET /metrics`（Prometheus 文本格式）：磁盘余量、活跃任务数、按阶段的任务分布、
维护耗时、累计淘汰量与释放字节。

## 验收

- `62 passed`（新增 14 个用例：meta 判据、回填不改 mtime、淘汰顺序、可用性判定、载荷特征、指标端点）
- 端到端：造 10 天前目录 → 重启 → `removed=1` 且目录消失

## 遗留

- 任务状态仍只存内存，重启后无法回溯历史处理结果；排查只能依赖 journal。
  下一步建议落 SQLite（工单号、归档大小、耗时、结果、错误码）。
- 维护仍是"每天一次全量校验"，工单数增长后建议改为增量（只在发布/删除时更新用量）。
