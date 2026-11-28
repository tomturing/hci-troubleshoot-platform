# 日志上传服务运维手册

> 最后更新：2026-09-11
> 适用环境：`/acli`（Flask + gunicorn，端口 5888）

---

## 目录

1. [服务概述](#一服务概述)
2. [本次优化背景与目标](#二本次优化背景与目标)
3. [发现的问题](#三发现的问题)
4. [已实施的变更](#四已实施的变更)
5. [清理工具使用手册](#五清理工具使用手册lifecycle_cleanpy)
6. [apppy-改造说明](#六apppy-改造说明)
7. [日常运维](#七日常运维)
8. [告警与故障处理](#八告警与故障处理)
9. [回滚指引](#九回滚指引)
10. [遗留待办](#十遗留待办)
11. [测试](#十一测试)
12. [版本管理（git）](#十二版本管理git)

---

## 一、服务概述

远程技术支持日志的接收服务：现网设备把排查日志打包成 zip 上传，服务端按**工单号**归档保存，供技术支持分析问题。

| 项目 | 说明 |
|---|---|
| 技术栈 | Flask 3.1 + gunicorn 23（gevent，4 workers） |
| 监听端口 | `0.0.0.0:5888`（`gunicorn_config.py`） |
| 单文件上限 | 5 GB，分片 10 MB，仅接受 `.zip` |
| 文件名规则 | `Q + 13 位数字 + .zip`，如 `Q2026090901775.zip`<br>其中前 8 位为**工单创建日期** |
| 归档目录 | `/sf/log/<工单号>/<工单号>.zip`（**规范名始终指向最新一份**）<br>重传时把旧包改名为 `<工单号>-<YYYYmmddHHMMSS>.zip` 保留，不覆盖 |
| 判重 | 归档时按 sha256 比对同目录已有包：内容相同的旧包**直接删除**（只留最新一份）；上传前可调 `/precheck` 先判重，命中则整包跳过上传 |
| 上传分片目录 | `/tmp/acli_upload_chunks` |
| 上传会话目录 | `/tmp/acli_upload_sessions`（JSON，24 小时过期） |
| 应用日志 | `/acli/logs/upload_server.log`（10MB × 5 轮转） |
| 清理日志 | `/acli/logs/lifecycle.log`（按天轮转，保留 30 天） |

### 数据规模基线（2026-09-10）

- 工单目录 **1401 个**，共约 **3.06 TB**
- 月均增量 **约 240 GB**
- 最大单工单 85 GB

---

## 二、本次优化背景与目标

### 背景

磁盘 `/dev/vda2`（4.0 TB）使用率一度达到 **92%，仅剩 330 GB**。按 240 GB/月 的增量，**约 1.5 个月就会写满**。

### 目标

1. **不影响上传功能**——任何清理动作都不得干扰在线上传
2. **尽可能保留现有日志**——这些是客户问题的排查依据，删了无法找回

### 策略取向

由此确定核心策略为**容量驱动（水位驱动）的弹性保留**，而非固定保留期：

> 不告急不删；告急时只删最老的、删到安全水位立刻停。
> 磁盘有多少余量，就保留多久。

对比（同样的数据）：

| 方案 | 删除量 | 保留窗口 | 执行后水位 |
|---|---|---|---|
| 固定 180 天 TTL | 1.52 TB | 约 6 个月 | 约 51% |
| **水位驱动弹性保留（采用）** | 按需（约 235 GB/次） | **约 12 个月** | 约 90% |

---

## 三、发现的问题

### 1. 上传分片永久泄漏（已修复）——最严重

`/tmp/acli_upload_chunks` 堆积 **7616 个分片文件，占用 75 GB**，最旧的来自 2026-08-11。

**根因**：原清理线程只认得合并后的单一文件 `{upload_id}.part`，从不清理 `{upload_id}_chunk_{i}.part`；而且它遍历的是会话目录中的 JSON——会话在上传成功或过期时会被删除，导致清理线程**永远扫不到对象**。分片只在「合片成功」路径里被逐个删除，一旦上传中断就永久残留。

### 2. 常驻清理线程重复劳动（已修复）

清理线程在每个 gunicorn worker 进程内各起一个（4 个 worker = 4 个线程），每小时各扫一遍同一目录。日志中那些间隔 8~11 分钟、毫无规律的清理记录即由此产生。既浪费资源，又污染日志。

### 3. 磁盘保留块浪费（已修复）

ext4 默认预留 5% 空间给 root（约 200 GB）。该盘实际用作数据盘，这部分空间纯属浪费。

### 4. 性能与安全问题（未处理，见[遗留待办](#十遗留待办)）

合片前 `time.sleep(10)`、MD5 重复计算两次、合片用 4 KB 小缓冲、前端串行上传分片、`app.run(debug=True)` 等。

---

## 四、已实施的变更

| # | 变更 | 说明 | 结果 |
|---|---|---|---|
| 1 | **清理孤儿分片** | 删除 3 天前的分片，先落清单再删 | 删除 7616 个，回收 **74.38 GB** |
| 2 | **分片清理逻辑修复** | `app.py` 新增 `delete_upload_chunks()`，在会话过期、上传成功、合片失败、未知异常 4 处即时清理 | 杜绝新增泄漏 |
| 3 | **移除常驻清理线程** | 兜底扫描移交 cron 脚本 | 消除 4 worker 重复扫描与日志噪音 |
| 4 | **新增清理工具** | `/acli/lifecycle_clean.py` | 分片 + 工单生命周期统一管理 |
| 5 | **tune2fs 释放保留块** | 5% → 1%（在线生效、可逆） | 释放约 **164 GB** |
| 6 | **配置计划任务** | root crontab 2 条 | 自动化运行 |
| 7 | **新增 `.keep` 白名单** | `/sf/log/.keep` | 支持永久保留指定工单 |
| 8 | **并发上传 + 竞态/安全修复（2026-09-11）** | 前端 3 并发上传、fcntl 锁+原子写根治会话并发竞态、`ACLI_UPLOAD_TOKEN` 鉴权、日志降级 INFO、关闭 `debug`、清理散落备份 | 上传更快且更可靠、接口可鉴权、日志噪音下降 |

### 变更效果

```
磁盘水位   92%  →  86%
可用空间   330G →  568G（+238G）
/tmp 分片  75G  →  1.5M
工单数据   一个都没删
```

### 其他环境变更（需知晓）

- `/acli` 目录属主由 `root` 改为 `acli`（便于代码维护，gunicorn 以 root 运行不受影响）
- `/sf/log/.keep` 属 `root`，编辑时需 `sudo`
- 改造前的 `app.py` 基线归档在 `/acli/backups/app.py.bak_20260910_baseline`（散落备份已清理，回滚优先用 git）

---

## 五、清理工具使用手册（`lifecycle_clean.py`）

### 5.1 设计原则

- **默认 dry-run**：不加 `--apply` 只预览，绝不删除
- **安全阀**：`--expect-count N`，实际待删条数与预期不符立即中止
- **删除优先级**：白名单 → 合规保护 → 水位触发
- **审计**：删除前先写 manifest，再执行删除
- **单实例**：flock 防 cron 重叠（锁不可用时降级为无锁运行并告警）

### 5.2 清理策略（cases 子命令）

```
1. 白名单（.keep 锁定 / 活跃上传中的工单 / 24 小时内 / 非 Q+13位 目录）  → 永不删
2. 年龄 ≤ 180 天（合规下限）                                            → 永不删
3. 年龄 > 180 天 且 水位 > 96%                                          → 按最老优先删，直到水位 ≤ 90%
4. 候选池删完水位仍 > 96%                                               → 停止删除，只告警转人工
```

**关键点**：

- **年龄取 `max(工单号日期, 目录 mtime)`**——工单号里的日期是工单创建日，实际上传可能滞后。实测案例：`Q2025031700614` 工单日期 2025-03-17，但日志 6 月才上传。若只按工单号日期算，它会被当成最老的一批优先删掉。
- **水位口径**与 `df` 一致：`used / (used + avail)`
- **删除单位是整个工单目录**（含目录内 `tgz`、`_members` 等附件）
- 无回收站，删除不可恢复——因此 dry-run 与 manifest 是主要的安全保障

### 5.3 命令与参数

```bash
python3 /acli/lifecycle_clean.py <子命令> [参数]
```

**`chunks`**——清理孤儿上传分片

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--max-age-hours` | 24 | 分片过期时长 |
| `--apply` | 关 | 真正执行删除（默认仅预览） |
| `--expect-count N` | 无 | 安全阀，待删条数不符则中止 |

**`cases`**——水位驱动的工单清理

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--high` | 96.0 | 触发水位（%） |
| `--low` | 90.0 | 停止水位（%） |
| `--compliance-days` | 180 | 合规下限（天），低于此年龄不删 |
| `--max-delete-gb` | 500 | 单次运行删除上限 |
| `--apply` | 关 | 真正执行删除 |
| `--expect-count N` | 无 | 安全阀 |

**全局**：`--no-lock` 跳过单实例锁（一般不用）

### 5.4 使用示例

```bash
# 1) 预览：查看当前水位与待删清单（安全，推荐先执行）
python3 /acli/lifecycle_clean.py cases

# 2) 确认条数后执行（把预览得到的数字填进 --expect-count）
sudo /usr/bin/python3 /acli/lifecycle_clean.py cases --apply --expect-count 137

# 3) 手动清理孤儿分片
sudo /usr/bin/python3 /acli/lifecycle_clean.py chunks --apply

# 4) 临时调整阈值（例如紧急降水位时）
sudo /usr/bin/python3 /acli/lifecycle_clean.py cases --high 90 --low 85 --apply
```

### 5.5 计划任务（已配置）

```cron
# 每小时检查磁盘水位，超过 96% 才按最老优先删除工单（删到 90% 停）
0 * * * * /usr/bin/ionice -c2 -n7 /usr/bin/python3 /acli/lifecycle_clean.py cases --apply >/dev/null 2>&1

# 每天凌晨 3 点清理超过 24 小时的孤儿上传分片
0 3 * * * /usr/bin/ionice -c2 -n7 /usr/bin/python3 /acli/lifecycle_clean.py chunks --apply >/dev/null 2>&1
```

未超阈值时脚本只做一次 `df` 读取（毫秒级）即退出，因此每小时执行的开销可忽略。

### 5.6 输出与产物

- **运行日志**：`/acli/logs/lifecycle.log`（按天轮转，保留 30 天）
- **删除清单**：
  - 工单：`/acli/logs/case_manifest_<时间戳>.csv`（工单号、路径、大小、年龄、工单日期、删除时间）
  - 分片：`/acli/logs/chunk_manifest_<时间戳>.csv`

### 5.7 白名单（`.keep`）

需要永久保留某工单时，把工单号写进 `/sf/log/.keep`（一行一个，`#` 开头为注释）：

```bash
echo "Q2025010100001" | sudo tee -a /sf/log/.keep
```

脚本会无条件跳过，并在报告中显示「白名单跳过 N 个」。

---

## 六、`app.py` 改造说明

### 6.1 新增 `delete_upload_chunks(upload_id)`

以 `upload_id` 为前缀匹配，删除该次上传产生的**全部**临时文件（`{id}.part` 与 `{id}_chunk_{i}.part`）。

调用位置（仅失败/异常路径；**成功路径不调用**——合片与归档已移交独立进程）：

| 位置 | 场景 |
|---|---|
| `load_upload_session()` 会话过期时 | 清理该次上传残留分片 |
| `upload_chunk` 派发独立进程失败 `except` | 派发异常即清 |
| `upload_chunk` 外层 `except` | 未知异常时按 `upload_id` 清理 |

### 6.2 移除常驻清理线程

原 `clean_expired_sessions()` 线程及其启动代码已删除，`import threading` 一并移除。

兜底清理改由 cron 调度 `lifecycle_clean.py chunks` 完成。请求路径内的**即时清理**（上传成功/失败）仍保留在 `app.py`，不依赖 cron。

### 6.3 生效方式

```bash
# 获取 master PID（PPID 为 1 的那个）
ps -eo pid,ppid,cmd | grep "gunicorn -c" | grep -v grep

# 优雅重载
sudo kill -HUP <master_pid>
```

重载后确认：新 worker 启动、`curl http://127.0.0.1:5888/` 返回 200、日志中不再出现「开始清理过期上传会话」。

### 6.4 异步处理架构（B 档：根治大文件超时）

**背景**：历史上 80GB 上传出现「分片已合并但未移动/重命名」——根因是合片与 MD5 在 gevent worker 内同步执行，阻塞事件循环导致心跳超时被 master 杀掉 worker。

**改造**：`upload_chunk` 收齐最后一片后，仅把会话标记为 `pending` 并立即 `return complete`，通过 `subprocess.Popen([python3, /acli/process_upload.py, upload_id], start_new_session=True)` 派发**独立进程**完成合片/MD5/归档。

- 独立进程脱离 gunicorn 进程组（`start_new_session=True`），worker 重启不被连带终止
- HTTP 请求毫秒级返回，hub 零阻塞，心跳正常，worker 不再被超时杀掉
- MD5 仅计算一次（原重复计算已消除），合片缓冲 4KB → 1MB
- 失败仅记入 `/acli/logs/upload_failed.log`，不影响 HTTP 响应（保持原交互方式）

**独立脚本** `/acli/process_upload.py`：

| 阶段 | 动作 |
|---|---|
| 合片 | 按分片序号顺序合并为 `.part`，合片过程中即删除各分片（1MB 缓冲） |
| MD5 | 合并文件算一次 |
| 归档 | `shutil.move` 到 `/sf/log/<工单号>/<工单号>.zip`（同分区即 rename，跨设备自动拷贝）；同名已存在时先把**旧包**改名加时间戳（时间戳取旧包 mtime），新包占用规范名 |
| 清理 | 删除会话 JSON；失败则清理临时文件并写失败日志 |

### 判重：内容相同就不必再传一遍

同一个包被反复上传是现场常态（客户重传、技术支持补传），而客户侧到本站的上行
实测只有 ~170KB/s，545MB 要传近一小时。判重分两层，都基于**整文件 sha256**：

| 环节 | 端点 | 行为 |
|---|---|---|
| 上传前 | `POST /precheck` | 带 `sha256` 时精确判定（`match=sha256, certain=true`）；只带 `file_size` 时按体积给"疑似"（`certain=false`，**不可据此拒收**） |
| 初始化 | `POST /init_upload` | 带 `sha256` 且命中已有包 → `409 duplicate_archive`；`force=1` 表示"知道一样，仍要留一份新的" |
| 归档 | `process_upload.py` | 与目录内任何包 sha256 相同 → 删除旧包（无论叫规范名还是带戳名），新包占规范名 |

设计取舍：

- 时间戳只证明"传过一次"，不证明内容变过；按 mtime 判重会把重复上传当成新版本。
- 体积相同不等于内容相同，所以体积命中只用于提示，绝不自动拒收或删除。
- 摘要缓存在工单目录的 `.digests.json`，键绑 `size + mtime_ns`；文件一被替换就失效重算。
- 归档顺序是"新包先落到 `.incoming` 暂存名 → 再删旧 → 最后 `os.replace`"，
  保证任何时刻磁盘上至少有一份完整归档；删旧失败只降级为"改名保留"，不影响上传成功。

---

## 七、日常运维

### 7.1 例行检查

```bash
# 磁盘水位
df -h / | tail -1

# 服务存活
curl -s -o /dev/null -w "http_code=%{http_code}\n" http://127.0.0.1:5888/

# 清理任务运行记录
tail -n 30 /acli/logs/lifecycle.log

# 分片目录是否异常增长
du -sh /tmp/acli_upload_chunks

# 工单目录数量与容量
ls /sf/log | wc -l
du -sh /sf/log
```

### 7.2 服务管理

| 操作 | 命令 |
|---|---|
| 查看进程 | `ps -eo pid,ppid,etime,cmd \| grep "gunicorn -c" \| grep -v grep` |
| 优雅重载（改代码后） | `sudo kill -HUP <master_pid>` |
| 停止 | `sudo kill <master_pid>` |
| 启动 | `cd /acli && .venv/bin/gunicorn -c gunicorn_config.py app:app`（建议配 systemd） |
| 应用日志 | `tail -f /acli/logs/upload_server.log` |

### 7.3 容量预期

- 当前水位 86%，可用 568 GB
- 按 240 GB/月 增量，**约 1.6 个月后触及 96% 触发线**
- 触发后每次删除约 235 GB 回到 90%，**保留窗口稳定在约 12 个月**

---

## 八、告警与故障处理

### 8.1 告警分级（写入 `lifecycle.log`）

| 水位 | 级别 | 含义 |
|---|---|---|
| ≥ 90% | WARNING | 提醒，接近触发线 |
| ≥ 94% | CRITICAL | 严重，即将触发删除 |
| ≥ 96% | — | 触发删除（删到 90%） |
| ≥ 98% | CRITICAL | 紧急模式，取消 IO 节流快速释放 |

> **注意**：目前告警只写入日志，**没有外部通知**（邮件/企微）。考虑到选择了 96% 的高水位策略，建议补充外部告警渠道。

### 8.2 常见故障

**Q：磁盘突然接近写满，等不到下次 cron？**

```bash
# 立即手动触发（会按 96% 规则执行，若当前未达 96% 需显式降低触发线）
sudo /usr/bin/python3 /acli/lifecycle_clean.py cases --high 85 --low 80 --apply
```

**Q：候选池为空但仍需降水位？**

脚本会输出「候选池为空：所有数据均在合规保护期内」并返回错误码。此时说明 **180 天内的数据已不足以腾出空间**，属容量问题，需人工决策：放宽 `--compliance-days`（需业务/合规确认）、外迁或扩容。

**Q：分片目录又涨上来了？**

```bash
# 先看预览，确认是否有活跃上传被误判
python3 /acli/lifecycle_clean.py chunks
```

脚本会自动保护活跃会话中的 `upload_id`。若确认为孤儿分片，执行 `chunks --apply`。

**Q：上传失败报 500？**

检查 `/acli/logs/upload_server.log`。改造后，上传失败会**立即清理本次分片**，属预期行为；用户需重新上传（前端不支持断点续传，重试会生成新的 `upload_id`）。

**Q：需要恢复已删除的工单？**

无法恢复——无回收站。只能通过 manifest 确认删了哪些，再联系客户重新上传。这是选择「无回收站 + 尽可能保留」策略的既定代价，重要工单请提前加入 `.keep`。

---

## 九、回滚指引

| 变更 | 回滚方式 |
|---|---|
| `app.py` 改造 | `cp /acli/backups/app.py.bak_20260911_pre_concurrent /acli/app.py` 后 `kill -HUP <master_pid>`（改造前基线：`/acli/backups/app.py.bak_20260910_baseline`） |
| tune2fs 保留块 | `sudo tune2fs -m 5 /dev/vda2`（在线、可逆；会减少约 164 GB 可用空间） |
| 计划任务 | `sudo crontab -e` 删除对应行，或 `sudo crontab -r` 清空 |
| 清理脚本 | 删除 `/acli/lifecycle_clean.py` 并移除 crontab 条目 |
| `/acli` 属主 | `sudo chown -R root:root /acli` |

---

## 十、遗留待办

按优先级排列，**均未实施**，实施前建议先评估影响。

### P1 性能（除前端并发外，均已通过 A+B 档修复）

| 问题 | 状态 |
|---|---|
| 合片前硬编码等待 10 秒 | ✅ 已修复（B 档：合片移出 worker） |
| MD5 计算两次 | ✅ 已修复（B 档：独立进程只算一次） |
| 合片用 4 KB 循环拷贝 | ✅ 已修复（B 档：改为 1MB 缓冲） |
| 阻塞 IO 在 gevent worker 内 | ✅ 已修复（B 档：合片/MD5 移交独立进程） |
| 单文件上限仅 5GB | ✅ 已修复（A 档：`MAX_FILE_SIZE` 提至 100GB，`init_upload` 校验） |
| 前端串行上传分片 | ✅ 已修复（前端改 3 并发上传；前置的会话并发写竞态已用 fcntl 锁 + 原子写根治） |

### P2 可靠性

- **会话并发竞态**：✅ 已修复。`upload_chunk` 改为在 `fcntl.LOCK_EX` 文件锁内完成「读-改-写」（`update_session_chunk`），并先写临时文件再 `os.replace` 原子替换；只有使分片计数达到 `chunk_total` 的那一次更新返回 `all_received=True`，天然去重，并发上传不再丢分片计数。
- **`executor.submit` 结果被丢弃**：✅ 已随 B 档改为 `subprocess.Popen` 派发独立进程（`process_upload.py`），失败仅写入 `/acli/logs/upload_failed.log`，不再静默丢失。
- **`os.rename` 跨设备风险**：✅ `process_upload.py` 归档已用 `shutil.move`（同分区 rename，跨设备自动拷贝）。
- **外部告警缺失**：⬜ 仍待办。水位告警仅写日志，建议接入邮件/企微。

### P3 安全与工程卫生

- **`debug=True` RCE 风险**：✅ 已修复。`app.run(..., debug=False)`；并新增 `ACLI_UPLOAD_TOKEN` 鉴权（见下文）。
- **上传接口无鉴权**：✅ 已加 token 鉴权。默认不启用（向后兼容）；设置 `ACLI_UPLOAD_TOKEN` 后，`/init_upload`、`/upload_chunk`、`/upload_status` 需携带 `Authorization: Bearer <token>`（或 `?token=`），前端页面自动注入并携带。
- **日志噪音**：✅ 已修复。文件/控制台/根 logger 均由 `DEBUG` 降为 `INFO`，每分片参数日志由 INFO 降为 DEBUG，仅保留关键节点 INFO。
- **历史备份文件散落**：✅ 已清理。散落的 `app.py_bk`、`app.py_bk_before_B`、`app.py_bk1~4`、`upload.html_bak1` 已删除；保留基线归档于 `/acli/backups/`（`app.py.bak_20260910_baseline` 为改造前基线，`app.py.bak_20260911_pre_concurrent` 为本轮改动前快照）。

### 本轮新增配置项（环境变量，均向后兼容）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ACLI_UPLOAD_TOKEN` | 空（不鉴权） | 设置后启用上传接口 Bearer token 鉴权 |
| `ACLI_UPLOAD_FOLDER` | `/sf/log` | 归档目录（测试/多实例可用） |
| `ACLI_TEMP_FOLDER` | `/tmp/acli_upload_chunks` | 分片临时目录 |
| `ACLI_SESSION_STORE` | `/tmp/acli_upload_sessions` | 会话 JSON 目录 |

**前端并发上传**：分片上传由串行改为 **3 并发**（`CONCURRENCY=3`），进度按「已完成分片字节 + 进行中各片实时进度」聚合。用户交互（选文件、开始/暂停/继续、进度条、成功提示）完全不变，仅总耗时更短、进度条涨得更快。

---

## 十一、测试

自动化单元测试放在 `backend/log-server/tests/unit/`（由 `uv run pytest backend/log-server/tests/unit/ -n0` 与 CI 统一运行）；历史现网独立脚本归档在 `backend/log-server/tests/legacy/`，可在目标宿主机使用 `.venv` 的 Python 单独执行。

### 11.1 单元测试 `backend/log-server/tests/unit/test_case_status.py`

覆盖 `/case_status`、`/cases/status/batch`、多归档改名元数据保留、`archive_bind_key` 稳定性、P1 静默停滞检测、严格只读不变式、以及鉴权与目录穿越安全边界。

```bash
uv run pytest backend/log-server/tests/unit/ -n0 -v
```

### 11.2 历史常规集成脚本 `backend/log-server/tests/legacy/test_upload.py`

覆盖五项（T1~T5）：默认无鉴权上传+合片一致、3 并发合片、高并发竞态压力（30 片 / 并发 12 / 3 轮，验证不丢片、恰好一个 `complete`）、重复分片不重复累加 `uploaded_size`、鉴权开关（无 token 401 / 有 token 200 / 首页免鉴权且注入 token）。

```bash
/acli/.venv/bin/python backend/log-server/tests/legacy/test_upload.py
# 输出末尾：=== RESULT: passed=25 failed=0 ===
```

### 11.3 历史大文件脚本 `backend/log-server/tests/legacy/test_large_file.py`

端到端验证 10 GiB 大文件：分片用固定种子**流式、确定性生成**（不把 10GB 载入内存），整体 MD5 同样流式计算并比对。实测 1024 片并发 8 上传约 80s、合片+MD5 约 140s、整体 MD5 字节级一致。

```bash
# 约需 20GB 临时空间（分片+归档同分区，合片 mv 后释放）与数分钟
/acli/.venv/bin/python backend/log-server/tests/legacy/test_large_file.py
# 输出末尾：=== RESULT: PASS ===
```

> 测试通过环境变量把分片/会话/归档指向临时目录（如 `/tmp/acli_test_large_*`），**不触碰生产 `/sf/log`**；运行结束按需 `rm -rf` 对应临时目录。

---

## 十二、版本管理（git）

`/acli` 已纳入 git（分支 `master`）。日常开发流程：

```bash
git -C /acli status                 # 查看改动
git -C /acli diff                   # 查看差异
git -C /acli add <具体文件>          # 仅 add 源码，勿 add logs/.venv/backups
git -C /acli commit -m "描述本次改动"

# 回滚用 git 自身（不再依赖 backups/ 快照）
git -C /acli log                    # 查提交历史
git -C /acli revert <commit>        # 撤销某次提交（生成反向提交）
```

忽略规则见 `.gitignore`：`logs/`、`backups/`、`.venv/`、`__pycache__/`、`*.pyc` 等运行产物不入库。改动前基线归档保留在 `/acli/backups/`（已被忽略），但首选回滚方式仍是 git。

---

## 附录：速查表

| 项目 | 值 |
|---|---|
| 触发水位 / 停止水位 | 96% / 90% |
| 合规下限 | 180 天 |
| 单次删除上限 | 500 GB |
| 单批删除量 / 批间隔 | 200 GB / 3 秒 |
| 紧急模式阈值 | 98%（取消节流） |
| 分片过期时间 | 24 小时 |
| 会话过期时间 | 24 小时 |
| 清理脚本 | `/acli/lifecycle_clean.py` |
| 清理日志 | `/acli/logs/lifecycle.log` |
| 独立处理脚本 | `/acli/process_upload.py` |
| 上传处理日志 | `/acli/logs/upload_process.log` |
| 上传失败日志 | `/acli/logs/upload_failed.log`（仅失败记录） |
| 白名单 | `/sf/log/.keep` |
| 服务端口 | 5888 |
| 应用日志 | `/acli/logs/upload_server.log` |
| 单文件大小上限 | 100 GB（`init_upload` 校验） |
