# ticket-sync 格式判定与重试终态加固（2026-11-01）

## 背景

#1408 / #1411 / #1412 修完「重复同步」四条路径并部署后，对宿主机（`192.168.0.4`，
`hci-ticket-sync.service`）做了一次以**运行态为唯一证据**的复盘：不看代码注释与 PR 描述，
只信 `journalctl`、`.ticket-sync.db`、归档魔数与 `rsync -n`。结论是**四条路径确实修好了，
但仍有 4 个活跃缺陷，其中 3 个是"重试永远不会成功"的死循环**。

### 已验证生效的部分

| 机制 | 证据 |
|---|---|
| #1408 续传 | `work_dir` 已按工单固定；对完整归档跑 `rsync -n` 得到 `transferred 0 bytes` |
| #1411 退避持久化 | 服务 22:28 重启后 `retries` 表仍保留 `failures=3` |
| #1411 墓碑 | `tombstones` 表存在且在 `create_auto` 中生效 |
| #1412 限流 | 日志 `窗口=7天 归档数=98` → `待重试=7 候选合计=9`，9 个候选未被放大成 98 个任务 |

## 发现的问题

### P0-1 扩展名与真实格式不一致，三个工单永久失败（12.2 GiB）

`Q2026102700681`、`Q2026102800262`、`Q2026102801855` 全部报
`[archive-corrupt] 压缩包已损坏`，但归档本身是好的：

```
head -c4   → "172." / "10.1"
offset 257 → ustar
tar -tf    → 10.137.66.5-20261028-1129.tgz ...
```

它们是**未压缩 tar 被命名为 `.zip`**。`_extract_any()` 按扩展名选工具，`.zip` → `unzip`
→ `exit=9`，被归类为"压缩包损坏"后无限重试。这类失败无论重试多少次都不会成功。

### P0-2 #1408 承诺的「失败保留归档」在 extract 阶段失效

```python
archive.unlink(missing_ok=True)  # 归档不再需要，先删掉腾空间
```

顶层归档在**解压开始时**就被删掉，之后任何深层失败，`finally` 里的
`prepare_workspace(work_dir, archive_name)` 已无归档可留。实测 `Q2026101600842`、
`Q2026102700874`、`Q2026102802029` 三个 work_dir **全是空目录**，每轮退避重试都从 0
重新拉 16–65 GiB。

### P1-1 退避阶梯封顶等于无限重试

`RETRY_BACKOFF_SECONDS` 用 `min(failures, 3)` 取档，到 21600s 后不再增长。当前 6 个工单
`failures=3` 会一直循环；`Q2026102900692` 已被判定 `non-log-payload`（"不是日志包，已跳过"）
却仍留在 `retries` 里反复重试——终态判定没有回写退避表。

### P1-2 限流只管入队，不管扫描

只要 `retries` 非空，巡检就每 31 秒扫一次 7 天窗口的全部 98 个归档：

```
22:34:15 窗口=7天 归档数=98    22:34:46 窗口=7天 归档数=98    22:35:17 ...
```

远端 `/sf/log` 有 1060 个目录，限流挡住的是入队，挡不住这趟 ssh 遍历。

## 改动

| 问题 | 改动 | 位置 |
|---|---|---|
| P0-1 | 新增 `detect_archive_format()`：按魔数（zip/rar/7z/gzip/xz/bz2/tar@257）判定真实格式，扩展名只作兜底；`_untar` 按格式选 `-xf/-xzf/-xJf/-xjf`；新增 `7z` 解压通道 | `app/extractor.py` |
| P0-2 | 归档保留到**解压全部成功后、publish 之前**才释放；顶层归档从 `_find_archives`/`_tree_stats` 显式排除；磁盘余量不足时才提前释放 | `app/extractor.py`、`app/main.py` |
| P1-1 | `MAX_RETRY_ATTEMPTS=6` 转终态；`TERMINAL_ERROR_CODES` 首次即终态；终态不进 `retry_ticket_ids()`、`create_auto` 直接让路；`job.message` 追加"已停止自动重试，可手动重试"；`/artifacts` 暴露 `retry.exhausted` | `app/main.py` |
| P1-2 | 7 天补扫按 `DEEP_SCAN_MIN_INTERVAL_SECONDS=300` 节流（`JobRegistry.last_deep_scan_at`） | `app/main.py` |

### 关键设计取舍

- **终态用「次数用尽」表达**，不新增数据库字段：确定性失败登记时直接把 `failures` 推到
  上限，两种终态共用同一套判据与跨重启恢复路径（`retries` 表 schema 不变）。
- **归档保留在 work_dir 内**而不是挪到同级目录：改动面最小，且能复用现有的按工单号固定
  目录、`ExecStartPre -mmin +120` 回收和 `cleanup_stale_workspaces()`。代价是必须显式排除，
  否则归档会被当成嵌套归档再解一次（实测产物里多出一层同名目录）、还会把 6 GiB 算进
  `.sync-meta.json` 的 `bytes`。
- **节流只加在宽窗口**：当天扫描（3 个归档）保留 30 秒粒度，不影响新工单的及时性。

## 验证

- 单测：`PYTHONPATH=. uv run python -m unittest discover -s tests` → **120 passed**
  （新增 `test_format_detect.py`、`test_retry_terminal.py`、`test_archive_release.py`，
  并在 `test_scan_guard.py` 补节流用例）。
- 真实数据：`Q2026102800262.zip`（6 261 637 120 字节，与远端一致）`tar -tf` 可正常列出
  5 个 `.tgz`；修复后 `detect_archive_format` 返回 `("tar", "magic")`，不再走 `unzip`。
- 待部署验证：宿主机升级走 `deploy/install-user.sh`（宠物部署，不走 ArgoCD），部署后
  三个 `archive-corrupt` 工单应转为 `ready`；6 个终态工单应从 `retries` 循环中退出。

## 遗留

- 采集端把 tar 写成 `.zip` 是上游问题，本服务只做兼容；若采集端修复，改动的兜底逻辑仍然有效。
- 归档暂存会让"峰值磁盘"比旧实现多一份归档体积（65 GiB 级）；容量红线
  `TICKET_MAX_TOTAL_BYTES=1 TiB` 与 2.1T 磁盘仍有余量，但并发数上调时需重算。
