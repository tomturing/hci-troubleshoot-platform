# ticket-sync 截断归档降级与损坏终态（2026-11-02）

## 背景

`Q2026102802029` 反复失败于 `Q2026102802029/172.28.64.133-20261029-0032.tgz`
（`tar exit=2`）。逐层对照后确认是**源端数据本身残缺**，不是传输或判据问题：

| 检查 | 源端 `/sf/log` 原始文件 | 本地 rsync 副本 | 结论 |
|---|---|---|---|
| `unzip -t` 整包 | `No errors detected` | 同 | zip 结构与 CRC 合法 |
| 内层 tgz md5 | `689827c4…1be8` | 完全相同 | 传输零损坏 |
| zip 大小 | 8 219 826 253 | 完全相同 | 传输零损坏 |
| `gzip -t` 内层 tgz | `unexpected end of file` | 同 | 内容残缺 |
| 可读字节数 | 1 124 073 472 | 同 | 与 zip 声明长度一字不差 |

1 124 073 472 = `0x43000000`（1 GiB + 48 MiB 整数边界），是采集端写入被截断的典型形态：
gzip 流少了尾部 CRC32 + ISIZE 8 字节，但打包时按实际写入量记录了 zip 条目长度。

**这解释了两件事**：
1. 任何包级校验都发现不了它——`unzip -t` 校验的是"已写入的 1.12 GB"，CRC 是对的；
   md5/sha256 只能证明"我拿到的与源端一致"，而源端本身就是这个样子。
2. 旧实现把它当整单失败（`extract-failed`，非确定性），于是重试 6 次才转终态；
   而实际报错前已经解出 3 GB 可用日志（`sf/log/**/*.log` 全在）。

## 改动

| 问题 | 改动 | 位置 |
|---|---|---|
| 错误码不准确，且被当可重试 | `tar` 的 `Unexpected EOF` / `unexpected end of file` 归 `archive-corrupt`（带 `degradable=True`）；`archive-corrupt` 加入 `TERMINAL_ERROR_CODES`，首次即终态 | `tools/ticket-sync/app/extractor.py`、`tools/ticket-sync/app/main.py` |
| 残缺归档整单判死 | 截断时**保留已解出内容**继续：顶层与嵌套都支持；残缺压缩包删除，避免下一轮 `_find_archives` 反复扫到；新增 `ExtractStats.truncated_archives` 与日志 `截断=N` | `tools/ticket-sync/app/extractor.py` |
| 用户看不清"能读一部分" | `job.message` 追加"N 个归档被截断，已保留可读部分"，与"工具无法解压"分开表述 | `tools/ticket-sync/app/main.py` |

### 关键取舍

- **降级只在"解出了一部分"时成立**：若一条都没解出来（`_tree_stats` 文件数为 0），仍按失败处理，
  否则会发布一个空目录，把"损坏"伪装成"就绪"。
- **`archive-corrupt` 进终态**：能降级的截断在 extract 层已经消化掉，走到失败分支的损坏意味着
  一点内容都拿不到，重试不可能改善。这与会污染外界的"网络类错误"不同，后者仍保留退避重试。
- 截断与"p7zip 打不开 RAR5"分开计数（`truncated_archives` vs `unreadable_archives`）：
  前者是"少了一部分"，后者是"一点都读不了"，运维判断完全不同。

## 验证

- 单测 146 passed，新增 `test_truncation.py`（5 例：截断可降级、嵌套保留内容、顶层保留内容、
  零输出仍失败、正常包不误报）与 `test_retry_terminal.py` 的 `archive-corrupt` 终态用例。
- 生产数据对照：`Q2026102802029` 的内层 tgz 在本地复现 `tar -xzf` 报 `Unexpected EOF`，
  报错前解出 3.0 GB 内容——正是本改动要保留的部分。

## 追加修复（同日）：降级分支漏做权限归一化

### 现象

`Q2026102802029` 降级成功后，产物里 `sf/log/vs/iscsi/` 下的 18 个日志
（`tgtd.log`、`tgt-mgrd.log` 及 `.log.1~9.gz`）对 uid 1000 全部 `EACCES`：

```
ls -ld  .../sf/log/vs/iscsi   ->  drw-r--r--
cd      .../sf/log/vs/iscsi   ->  Permission denied
du      .../sf/log/vs/iscsi   ->  18 条 cannot access
```

`du` 跳过该子树，因此 `du -sh` 报 34G，比 `.sync-meta.json` 的 35.5 GB 少。

### 根因

目录缺执行位（`0664`）本身不是新问题——`_normalize_permissions` 就是为它写的。
漏的是**调用点**：正常归档在解压后执行 `_normalize_permissions(target)`，
而截断归档走 `degradable` 分支后直接 `continue`（保留内容、删残缺包），
跳过了这一步。于是"截断包里解出来的那部分"成为唯一没有补权限的产物。

证据链：

| 证据 | 值 |
|---|---|
| 截断告警 | `09:53:28 归档截断 path=...172.28.64.133-20261029-0032.tgz` |
| 该目录 `ctime` | `09:53:07`（解压时刻，此后**没有任何 chmod 触碰**） |
| 同 job 完成日志 | `depth=5 archives=209 补权限=114 截断=1`——114 全部来自正常归档 |

`ctime` 是关键判据：chmod 会更新 ctime，若截断分支补过权限，ctime 必然晚于 09:53:28。

### 改动

| 问题 | 改动 | 位置 |
|---|---|---|
| 降级保留下来的内容不补权限 | 在 `degradable` 分支 `continue` 前调用 `_normalize_permissions(target)`，与正常路径对齐 | `tools/ticket-sync/app/extractor.py` |

### 影响面

不只是"几个日志读不到"。目录缺 `x` 时 `os.walk` 进不去，会连带：

1. `_tree_stats` 少算字节与文件数（`bytes`/`files` 偏低）；
2. 下一轮 `_find_archives` 扫不到该子树里的嵌套归档，**整片漏扫**；
3. `_audit` 的容量统计偏低，淘汰判断失真。

### 验证

- 新增 `test_truncated_archive_subtree_permissions_normalized`：构造 `0664` 目录的截断包，
  断言 `permissions_fixed >= 1` 且目录具备 `S_IXUSR`。
- 反向验证：临时移除该行，用例即失败（`1 failed, 6 passed`），确认不是假测试。
- 全量 152 passed。
- 现场修复：对 `Q2026102802029` 的 `iscsi` 目录补 `u+rwx` 后，
  `tgtd.log`（41.7 MB）、`tgt-mgrd.log`（50.2 MB）等 18 个文件恢复可读，
  全库 `find -type d ! -perm -u+x` 归零。

