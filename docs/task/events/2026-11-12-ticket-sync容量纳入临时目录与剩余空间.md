# ticket-sync 容量治理纳入临时目录与剩余空间（2026-11-12）

> 关联：[工单多次采集包合并](2026-11-12-工单多次采集包合并.md)、
> [ticket-sync 自动巡检与清理](2026-10-29-ticket-sync自动巡检与清理.md)、
> [工单日志宿主机同步服务](../../solution/events/2026-10-23-工单日志宿主机同步服务.md)

## 问题

容量治理原先只统计 `artifacts.bytes`，即 `/sf/tickets/Q...` 已发布产物的逻辑字节：

- `.tmp` 里的下载件、解压树、合并 `build/` 不计入；
- `.trash` 里的上一版产物不计入；
- 不看磁盘剩余可写空间，只看 `TICKET_MAX_TOTAL_BYTES`。

合并多次采集后，单个工单可能同时存在几十 GiB 的归档与解压中间态；失败任务还会把
下载件留在 `.tmp` 供续传。结果是 `df` 已经紧张、甚至新任务因 `TICKET_MIN_FREE_BYTES`
被拒，容量检查却仍显示“未超限、删除 0”，无法自愈。

## 决策

### 容量口径

```text
总量 = 已发布产物（artifacts.bytes）
     + .tmp 独占文件
     + .trash 独占文件
```

- 只统计 `st_nlink == 1` 的文件：`.trash` 与当前产物、`.tmp/build` 与当前产物之间是
  硬链接，同一 inode 只应算一次；否则容量虚高会提前误删可用工单。
- 状态库等固定小文件不计入总量上限；它们与工单数据量无关，真实磁盘压力由
  `free` 判据兜底。

### 触发条件

任一满足即释放：

1. `总量 > TICKET_MAX_TOTAL_BYTES`；
2. `剩余可写空间 < TICKET_MIN_FREE_BYTES`（与任务准入/解压红线复用同一个旋钮）。

### 释放顺序

1. **`.trash`**：回退副本，丢掉只影响人工回退；
2. **超龄 `.tmp`**：只删超过 2 小时的目录，且跳过正在同步的工单
   （`active_ticket_ids`），避免毁掉 rsync 续传；
3. **已发布产物**：仍按“非日志载荷优先、最旧优先”淘汰，并继续受
   `TICKET_MIN_KEEP_DAYS`（默认 3 天）保护。若可淘汰工单都不满保护期，
   只记录明确告警，不强删。

### 可观测性

- 每日维护日志打印 `总量/上限/可用/下限/删除/释放`，不再只打印“删除=0”。
- `/metrics` 新增：
  - `ticket_sync_capacity_used_bytes`
  - `ticket_sync_capacity_artifact_bytes`
  - `ticket_sync_capacity_extra_bytes`
  - `ticket_sync_capacity_limit_bytes`
  - `ticket_sync_capacity_free_bytes`
  - `ticket_sync_capacity_min_free_bytes`

## 实现

| 文件 | 改动 |
|---|---|
| `tools/ticket-sync/app/maintenance.py` | 容量口径加入 `.tmp`/`.trash`；`free` 触发；`.trash`/陈旧 `.tmp` 先释放；`report` 输出中间事实；硬链接按 `st_nlink == 1` 去重 |
| `tools/ticket-sync/app/publisher.py` | `cleanup_stale_workspaces` 支持 `protected`，活跃工单的 `.tmp` 不按年龄回收 |
| `tools/ticket-sync/app/main.py` | 容量检查传入 `min_free_bytes`/活跃集合/`report`；维护日志与 `/metrics` 暴露总量、可用空间、拆分口径 |
| `tools/ticket-sync/app/config.py` | dataclass 默认值与 `DEFAULT_MAX_TOTAL_BYTES`（1 TiB）对齐，消除 400 GiB/1 TiB 两套口径 |
| `tools/ticket-sync/deploy/ticket-sync.env.example` | 显式登记 `TICKET_MAX_TOTAL_BYTES` / `TICKET_MIN_FREE_BYTES` / `TICKET_MIN_KEEP_DAYS` 及释放顺序 |
| `tools/ticket-sync/README.md` | 容量公式、触发条件、释放顺序、保护期与指标说明 |

## 验证

新增用例覆盖：

- `.tmp`/`.trash` 计入总量，盘压时先丢它们、不误删工单；
- 剩余空间低于红线时即使产物总量未超限也淘汰；
- 剩余空间紧张但工单未满保护期时只告警不删；
- `.trash` 与产物硬链接时只算一次；
- 活跃 `.tmp` 即使超龄也不回收；
- `report` 能区分产物字节与额外字节。

本机 `tools/ticket-sync` 全量：`5 failed, 209 passed`，5 项 `test_truncation` 在
改造前即因 macOS 宿主 tar 报错文案失败，与本次无关；本次新增用例全部通过。

## 运维提醒

1. 设备上运行的是 `~/.local/share/hci-ticket-sync/app` 的副本，需重跑
   `tools/ticket-sync/deploy/install-user.sh` 并重启用户服务才会生效。
2. `TICKET_MIN_FREE_BYTES` 默认 120 GiB，既可拒绝新任务，也会触发容量释放；
   盘较大或需要给系统与镜像留更多余量时，可按运维窗口上调。
3. 总量上限仍是**每天一次**的全量检查；剩余空间低于红线时，维护循环每 30 秒
   只读一次 `statvfs`，触发后最多每 5 分钟执行一次释放。它不是任务准入的硬门禁，
   单次合并的瞬时峰值由解压前后的剩余空间检查兜底。

## 与 #1491 的合并说明（2026-11-13）

本方案与同期合入的 #1491「清理改为半分钟级 + 磁盘水位双阈值」改的是同一块代码，
落库时按**上游实现为准**收敛，避免两套删除逻辑并存互相打架：

- 容量治理统一走 `maintenance.run_cleanup_cycle`，每 30 秒一轮；总量上限退为
  **第三重保险（代理指标）**，主控改为剩余空间软阈值 `TICKET_EVICT_FREE_BYTES`，
  硬阈值 `TICKET_MIN_FREE_BYTES` 只负责写入前熔断；
- `.tmp` / `.trash` 不再计入总量上限，改为**每轮主动回收**（`purge_trash` +
  `cleanup_stale_workspaces`），硬链接去重与 `_extra_usage` 那套统计随之删除；
- 本文档「每日全量检查 + 低空间每 5 分钟释放」的频率口径已作废，以 #1491 的
  「每轮清理」为准。

本分支实际保留并已合入的部分：

| 保留项 | 位置 |
|---|---|
| 释放顺序：可再生的 `.trash`/超龄 `.tmp` 先于淘汰已发布产物 | `run_cleanup_cycle` 步骤 2/3 先于步骤 5；回归用例 `test_stale_tmp_released_before_evicting_artifacts` |
| `.tmp` 回收跳过活跃工单（`protected`） | `publisher.cleanup_stale_workspaces` + `run_cleanup_cycle` 传 `active_ticket_ids`；回归用例 `test_active_workspace_is_not_purged_even_if_stale` |
| `max_total_bytes` dataclass 默认值与 `DEFAULT_MAX_TOTAL_BYTES` 对齐 | `app/config.py` |
| `ticket_sync_capacity_removed_total` / `ticket_sync_capacity_freed_bytes_total` | `app/main.py`，数据源改为 `CleanupReport` |

`capacity_used_bytes` / `capacity_artifact_bytes` / `capacity_extra_bytes` 等拆分口径未保留：
对应统计实现已不存在，继续暴露只会恒为 0，反而误导排障。
