# ticket-sync 解压韧性优化（2026-10-30）

## 背景

2026-10-23 至 10-30，宿主机 `hci-ticket-sync` 累计失败 646 次，集中在 8 个工单。
逐条取证后确认这不是"若干坏包"，而是四类结构性问题叠加：

| 现象 | 实测证据 | 根因 |
|---|---|---|
| `depth-exceeded` 585 次 | 真实嵌套到 **7 层** | 上限按早期样本拍成 4 层；且到达上限即整包失败 |
| `quota-exceeded` 40 次 | 单工单 **108,523** 个文件 | 上限 5 万，而文件数随节点数线性增长 |
| `archive-corrupt` 17 次 | `LOG_run_shm.txt.zip` 只有 **1 字节** | 采集端写出占位包，服务把一个叶子坏包放大成整包失败 |
| 单工单重试 **192 次** | 无效 rsync 约 **450 GiB** | 失败无退避，30 秒巡检无条件重拉 |
| kubelet/containerd 日志不可读 | 36 个目录权限 `drw-r--r--` | `tar --no-same-permissions` 只能去掉权限位，补不回缺失的 `x` |
| `.tmp` 累积 586 个目录 | 日志打印"已清理"但目录仍在 | `rmtree(ignore_errors=True)` 静默失败仍谎报成功 |

## 改动

### 解压层（`app/extractor.py`）

1. **权限归一化**：每层解压后对新增子树补属主读写位，目录额外补执行位。
   必须在扫描嵌套归档之前完成——目录缺 `x` 时 `os.walk` 进不去，深层归档会被整片漏扫，
   quota 统计也会偏低。只改属主为自己的条目，避免对保留他人属主的文件触发 EPERM。
2. **占位包跳过**：名称匹配归档后缀但小于 64 字节的文件直接剔除并告警。
   合法 zip 最小 22 字节、tar.gz 至少 1 KiB 量级，因此该判定不会误删可用数据。
3. **RAR 支持**：`ARCHIVE_SUFFIXES` 增加 `.rar`，解压优先 `unrar`、回退 7z（见第二轮收敛）。
4. **RAR 降级**：两条通道都解不开时保留原件、计入 `unreadable_archives`、任务继续成功。
   已跳过的路径记入 `skipped_archives`，否则下一轮会被重新扫到，形成死循环。

### 拉取层（`app/fetcher.py`）

5. **非日志载荷识别**：下载前用 `unzip -Z1` 读远端中央目录（不加密、不下载），
   命中"条目 ≤3 且含 vmdumpcore/.core 且无 .tgz/log_list.json"时直接跳过。
   实测 `Q2026102900692` 只含 34.6 GiB 的 vmcore，此前白占 33 GiB 磁盘。
   探测失败一律放行，不能把正常工单挡在门外。

### 任务层（`app/main.py`）

6. **失败退避**：10min → 30min → 2h → 6h，手动 `POST /jobs` 不受限。
7. `/artifacts` 增加 `retry.nextRetryAt`，让"失败但不再自动重试"可见。
8. 解压成功但存在无法解压的附件时，`message` 写入提示，前端以告警色展示。

### 清理与容量（`app/publisher.py`、`app/maintenance.py`、`app/config.py`）

9. `cleanup_workspace` 先补权限再删，失败必须留痕，不再谎报"已清理"。
10. 新增总量上限淘汰：`TICKET_MAX_TOTAL_BYTES`（默认 400 GiB）+
    `TICKET_MIN_KEEP_DAYS`（默认 3 天保护期），超限时按最旧优先删除。
    只靠保留期是时间维度，扛不住"单日 5 个 50~65 GiB 工单"的空间压力。

### 展示层（`frontend/admin`）

11. `quota-exceeded` / `depth-exceeded` 不再表述为"体积异常""层级异常"，
    改为"超出服务解压上限""超出服务层级上限"，并把服务端 `message`
    （含具体数值）拼接展示，新增 `non-log-payload` 文案。

## 第二轮收敛（同日，处理上文「遗留」）

上线后回看日志，三个遗留都还能咬人，逐项处理：

| 遗留 | 处理 | 依据 |
|---|---|---|
| 阈值余量只剩 12%（17.6 万 / 20 万） | `MAX_DEPTH` 12、`MAX_FILES` 500,000、`MAX_TOTAL_BYTES` 1 TiB | 实测单工单最大 176,562 文件、最深 7 层；取 2.8 倍与 70% 余量 |
| 层数超限会整单失败 | 到达上限改为**停止下钻**：保留已解产物、`depth_truncated` 计数、`job.message` 提示 | 主体日志都在浅层，为深层附件回滚整目录等于整单不可用 |
| RAR5 只能保留原件 | 双通道：优先 `unrar`，失败回退 `7z`；`install-user.sh` 装 rarlab 静态二进制，unit 补 `PATH` | 实测 `1.rar`（RAR5 v50 -m3）p7zip 23.01 报 `Unsupported Method`，unrar 7.23 正常读 |
| `ssh exit=255 stderr=`（2026-10-30 16:25） | 抽出 `_ssh_run`：连接层失败重试一次，detail 固定带 exit/attempt/elapsed_ms/remote，stderr 为空显式标 `(空)` | 旧实现只记 stderr，而 255 常常为空，事故无从归因 |

关键取舍：**阈值从"判据"降为"护栏"**。层数/文件数的目的不是判断包是否正常，
而是防止异常归档打爆磁盘与 inode；因此超限的正确反应是"停止并保留"，不是"判死并丢弃"。

## 验收

```
38 passed（新增 21 个用例：占位包、权限、清理、退避、容量、载荷识别、RAR 降级）
```

线上实测：

| 场景 | 结果 |
|---|---|
| `Q2026103000154`（1 字节坏包） | `ready`，跳过 1 个占位包，其余日志完整 |
| `Q2026102900692`（vmcore） | `failed / non-log-payload`，`receivedBytes=0`，未下载 |
| `Q2026102700323`（RAR5） | `ready` + "1 个归档工具无法解压，已保留原压缩文件" |
| 8 个历史失败工单 | 全部 `ready`，其中最深 7 层、最多 176,562 文件 |
| 权限 | 不可读目录 36 → 0，补正 906 处 |
| `.tmp` | 586 → 0 |

## 配置变更（宿主机 env，不入库）

```
TICKET_MAX_DEPTH=12           # 真实 7 层；超限只停止下钻，余量 70%
TICKET_MAX_FILES=500000       # 单工单实测最大 176,562，余量 183%
TICKET_MAX_TOTAL_BYTES=1099511627776   # 磁盘扩容到 2.1T 后重定标为 1 TiB
```

## 遗留

- 阈值已按单机 8 个工单的实际分布定标；集群节点数翻倍后需按同一口径（实测最大值 × 2.8）重算。
- `ssh exit=255` 只在 2026-10-30 16:25 出现一次且未复现，根因未定位；
  现已具备重试与可归因 detail（exit/attempt/elapsed_ms/remote/stderr），下次出现可直接定性。
- `Q2026010100001` 这类人工目录（仅 README.md）仍会出现在 `/artifacts` 列表中。
