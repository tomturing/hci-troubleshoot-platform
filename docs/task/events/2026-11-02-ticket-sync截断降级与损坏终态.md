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
