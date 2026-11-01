# ticket-sync 手动任务优先通道与远端换包重同步（2026-11-01）

## 背景

为回答「自动同步与 admin-ui 输入工单手动同步有什么区别」而逐行对照 `app/main.py`，
发现两处**实现与设计意图不一致**，其中一处的注释本身就在陈述一个不成立的事实：

| 项 | 现状 | 影响 |
|---|---|---|
| `create_auto` docstring | 声称「为尚未同步**或远端已更新**的归档创建自动任务」 | 实现只判断 `final_dir.is_dir()` 就 `return None`，`RemoteArchive.mtime` 从未参与比较 |
| 并发控制 | 手动与自动共用 `Semaphore(TICKET_CONCURRENCY)` | 一轮巡检入队 5 个 60 GiB 级包后，人工点击只能排在队尾，最长等数小时 |

第一条的实际后果：客户重传修复版（同名 zip，mtime/size 变化）后，自动同步永远拉不到
新包，只能靠人工在页面重新输入工单号——而页面提示"已就绪"，用户没有任何理由去重试。

第二条的实际后果：`queued` 状态对用户是黑箱，点击"同步日志"后界面长时间停在排队，
与"点击即开始"的交互预期严重脱节。

## 决策

### 1. 手动任务走独立通道，不进并发闸门

`_run` 按 `job.origin` 选择闸门：`manual` 走 `_unlimited()`（立即通过），`auto` 走原
`Semaphore`。用 `async with` 上下文管理器而不是手写 `acquire/release`，异常路径不会漏放。

- 手动任务的"限制"只剩一条：同一工单正在任务中 → `409 job-running`（两个 rsync 抢
  同一个目标目录会互相破坏，这条不能放开）。
- 自动任务的限流全部保留：单轮 `MAX_AUTO_JOBS_PER_SCAN=5`、退避阶梯、墓碑。

### 2. 远端换包判据：与"上次拉的是哪一版"比对

发布成功时把刚拉下来的归档 `(mtime, size)` 写进 `.sync-meta.json` 的
`remoteMtime` / `remoteSize`；扫描时若远端 `size` 变化、或 `mtime` 比记录值更新
（>1s 容差）则判定"换了一版"，重同步并清掉失败退避。

为什么是这个判据，而不是"与 `completedAt` 比大小"：

- **幂等**：同一输入永远给出同一结论。用完成时间做推进式比较，一次元数据写失败就会
  让"远端比完成时间新"长期成立，退化成每轮重拉 65 GiB。
- **不会误伤存量**：改造前发布的目录没有这两项记录，一律按"没变"处理，升级首轮不会
  重拉历史产物（2026-11-01 已因放宽窗口一次性重拉 57 个工单、消耗 766 GiB）。
- **方向安全**：只认"远端变新"。即使哪天 rsync 不再保留 mtime（本地记录值被刷成拉取
  时刻），也只会漏判，不会变成无限重拉；体积变化仍能兜住重传。

`fetch` 用 `rsync -rlt`，`-t` 保留源端 mtime，因此本地归档文件的 `stat` 就是远端身份；
该 `stat` 必须在解压（可能删归档）与发布（`work_dir` 被 rename 走）之前取。

### 3. 来源可观测

- `jobs.origin` 落 SQLite（老库通过 `ALTER TABLE ADD COLUMN` 幂等补列，历史行按 `auto` 回填）。
- `/jobs`、`/artifacts` 的 job 载荷新增 `origin`。
- `/metrics` 新增 `ticket_sync_active_jobs_origin{origin="manual"|"auto"}`：总量指标
  无法区分"2 个自动任务"与"2 个自动 + 3 个人工点击"。

## 对抗性审查：这次改动的代价

1. **并发上限不再是全局硬上限。** N 个用户同时点 N 个不同工单，就是 N 个 rsync 同时
   跑，"2 并发 × 65 GiB"的磁盘余量假设只覆盖自动路径。保留的硬护栏是
   `TICKET_MIN_FREE_BYTES`（磁盘红线）与 `TICKET_MAX_EXTRACT_BYTES`（展开上限），
   两条路径一视同仁。运维侧可对 `ticket_sync_active_jobs_origin{origin="manual"}`
   设阈值告警。
2. **远端换包检测依赖 rsync 保留 mtime。** 已用"只认变新"方向兜住最坏情况；
   `size` 判据独立生效，两者任一变化即重同步。
3. **手动失败仍写入退避表**（本次未改）。它同时承担"阻止 30 秒巡检立刻重拉坏包"的
   职责，改动需要给 `retries` 加来源列并重新定义跨天补扫的准入判据，属于独立决策，
   不在本 PR 范围内。
   - 顺带纠正一个已有判断：#1413 合入后，失败已达 `MAX_RETRY_ATTEMPTS`（6）或命中
     确定性错误码即转**终态**，自动扫描一律让路。原先"手动点一次坏包 → 自动巡检在
     7 天窗口内反复重拉"的风险因此被收敛到"最多 6 次 / 确定性失败 0 次"，不需要
     再引入来源列。
   - 终态与"远端换包"的交互按本次语义处理：远端换包说明输入事实已改变，重同步会
     清掉终态（客户把转储包换成真日志包时，自动链路能自己恢复）。

## 验证

```
cd tools/ticket-sync
PYTHONPATH=. uv run --frozen pytest -o addopts="" -p no:cacheprovider -q
# 134 passed（已合并含 #1413 / #1415 的 main）
```

新增用例：

| 文件 | 覆盖 |
|---|---|
| `tools/ticket-sync/tests/test_manual_priority.py` | 手动任务在自动任务占满唯一槽位时立即进入 `fetching`；第二个自动任务仍被挡；同工单仍 `409`；`origin` 落库可被新进程读回 |
| `tools/ticket-sync/tests/test_auto_resync.py` | 同版本跳过；`size` 变化重同步；`mtime` 变新重同步；历史目录（无 `remoteMtime/remoteSize`）永不重拉；远端换包清退避；退避窗口内仍跳过；手动点击不受版本判据限制 |

## 未做与后续

- staging 宿主机上的 `hci-ticket-sync` 仍是旧代码，需 `tools/ticket-sync/deploy/install-user.sh`
  重跑一次才会生效（该服务是"宠物"部署，不走 ArgoCD）。
- 现场验收建议：① 对一个已同步工单重传远端 zip 后观察自动重同步日志
  `远端归档已更新，重新同步`；② 制造 2 个自动任务占满槽位，再手动点第三个工单，
  确认 `/metrics` 的 `origin="manual"` 立即变为 1。
