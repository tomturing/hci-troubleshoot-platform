# 5888 `/upload_progress` 端点部署与回滚 SOP

## 一、为什么需要它

P1 直传由节点上的 acli 自己调用 `/init_upload`，平台从来没有 `upload_id`，
只能用产物名 `Q{13位}.zip` 作两侧公共键。平台 PR #1478 上线的
`GET /destination-progress` 对 P1 调 5888 的 `/upload_progress?filename=`，
旧版 5888 没有该端点，返回 404 → 平台降级为「不支持进度查询」。本补丁补上
这个只读端点，管理端「日志上传」页才能显示 P1 的真实进度。

端点契约（字段必须与平台 `_map_progress` / `_session_matches_generation` 对齐）：

| 字段 | 约束 |
|---|---|
| `found` | `false` 也必须 HTTP 200，平台显示「暂无记录」而非失败 |
| `status` | 只有 `done` + `target_path` 会被平台判为已交付；`uploading` 判为上传中 |
| `start_time` | **必须是带 `+00:00` 的 UTC ISO**；平台拿它和 aware `created_at` 比较，naive 会抛 `TypeError` 变 500 |
| `digest.sha256` | 有则平台展示「收妥（sha256 一致）」 |
| `upload_id` | 平台用它排除「这一轮其实是 P2 代传会话」 |

## 二、三条不可让步的设计约束

1. **只读**：绝不能调用 `load_upload_session()`，它发现会话过期会顺手删会话
   与分片。端点直接 `os.listdir` + `json.load`，半截 JSON 只跳过。
2. **终态不丢**：会话被 `session_reaper.py` 24h 清掉后，归档件仍是权威事实。
   找不到会话时兜底读归档目录，回报 `status=done / progress=100`，并把归档
   mtime 作为 `start_time` 供平台做代际校验。否则已交付的 P1 会被显示成
   「暂无记录」，与 `Q2026111000243` 的误判是同一类错误。
3. **查不到 ≠ 失败**：会话与归档都无才 `found=false`，且 HTTP 200；平台不写库。

## 三、交付物

| 文件 | 用途 |
|---|---|
| `upload_progress.snippet.py` | 权威端点源码（人类可读），与 patch 内容逐字一致 |
| `app.py.patch` | **唯一权威交付物**，针对基线 `0f4764e` 的 unified diff |
| `build_patch.py` | 从真实基线 + snippet 生成 diff，避免手写行号出错 |
| `apply.sh` | 幂等落地：基线 sha256 校验 → dry-run → 备份 → apply → `py_compile`，失败自动回滚 |
| `rollback.sh` | 从最近备份恢复并校验回到基线 |
| `preflight_restart.py` | 重启硬门禁：会话/分片活跃度、合片进程、ESTABLISHED 连接、healthz |
| `verify_endpoint.py` | 重启前在隔离目录用 Flask `test_client` 跑契约断言 |
| `SHA256SUMS` | 全部交付物摘要，远程 apply 前核对 |

基线：`192.168.0.2:/acli/app.py`，commit `0f4764e`，1484 行，
sha256 `2e3c19718849aa21a4eb0c623c52335ed538bf2023c514ef5ac00d2cc37ce231`。
`/acli` 是**无 remote 的本地 git 仓库**，走不到 GitHub PR；平台仓库这边的
PR 负责托管补丁与 SOP，现网 `/acli` 另做一次本地 commit，消息引用平台 PR 号
与 `app.py.patch` 的 sha256，两边可互相追溯。

## 四、部署步骤

以下命令在平台开发机执行；`ssh 192.168.0.2` 走 22468 端口 + `acli` 用户。

```bash
# 0. 生成本地 PR 的权威 diff（改了 snippet 必须重跑）
uv run --no-project python tools/log-server-5888/build_patch.py \
  --baseline /tmp/acli_app_baseline.py --output tools/log-server-5888/app.py.patch
patch --dry-run -p1 -d /tmp < tools/log-server-5888/app.py.patch   # 需先把基线放到 /tmp/app.py

# 1. 传补丁与工具（只传必要文件，不传源码副本）
scp tools/log-server-5888/{app.py.patch,apply.sh,rollback.sh,verify_endpoint.py,preflight_restart.py} 192.168.0.2:/tmp/

# 2. 重启前硬门禁：allowed=true 才继续
ssh 192.168.0.2 '/acli/.venv/bin/python /tmp/preflight_restart.py'

# 3. 交叉验证平台侧近 24h 无活跃 P1/P2（期望输出 0|0）
kubectl exec -n hci-staging postgres-0 -- psql -U hci_admin -d hci_troubleshoot -tAc \
  "SELECT count(*) FILTER (WHERE upload_mode='acli_direct' AND (status IN ('collecting','packaging','uploading') OR delivery_status IN ('queued','pulling','uploading','relaying'))) AS p1_active, count(*) FILTER (WHERE upload_mode='bridge_relay' AND (status IN ('collecting','packaging','uploading') OR delivery_status IN ('queued','pulling','uploading','relaying'))) AS p2_active FROM log_collect_task WHERE created_at > now() - interval '24 hours'"

# 4. 落地补丁（幂等、带备份、失败自动回滚）
ssh 192.168.0.2 'APP=/acli/app.py PATCH=/tmp/app.py.patch bash /tmp/apply.sh'

# 5. 重启前隔离验证：必须全 PASS，此时现网 worker 仍是旧代码
ssh 192.168.0.2 '/acli/.venv/bin/python /tmp/verify_endpoint.py'

# 6. 让新代码生效（二选一，见第六节）
# 6a 优雅重载（推荐，需提权）：sudo -A kill -HUP <MainPID>
# 6b 无提权时靠 gunicorn max_requests=1000 自然轮换 worker

# 7. 现网验证：连续 20 次请求全部 200（确认 4 个 worker 都已换代码）
# 8. /acli 本地 git commit（中文，引用平台 PR 号 + patch sha256）
# 9. 平台侧回查 destination-progress，并用真浏览器验收「日志上传」页
```

## 五、提权与凭据处理

`acli` 用户无免密 sudo，`sudo -S` 从 stdin 喂密码在部分环境读不到，
`sudo -n` / `kill -HUP` / `systemctl restart` 均需密码。可用方式是
`SUDO_ASKPASS` + `sudo -A`：

- 凭据写入 **chmod 700 的临时 askpass 文件**，用毕双向删除（本地 + 远端
  `/tmp/.ap_tmp`），不写进任何仓库、文档、日志或提交信息；
- 命令行里不带明文密码（会被环境审批策略拦截）；
- 只用一次，只执行 `kill -HUP` 或 `systemctl restart`，不做其他 root 操作。

## 六、生效方式（按风险从低到高）

| 方式 | 是否需 root | 中断 | 判据 |
|---|---|---|---|
| `kill -HUP <gunicorn master>` | 是 | 无监听中断，worker 优雅替换 | 4 个 worker 启动时间全部更新；20/20 请求命中新端点 |
| `systemctl restart acli-5888` | 是 | 秒级 | 服务 active、healthz 200、新端点 200 |
| `max_requests=1000` 自然轮换 | 否 | 零中断 | 连续轻量 `/healthz` 触发每 worker 轮换，再 20/20 命中新端点 |

现网 `gunicorn_config.py`：`workers=4 / gevent / max_requests=1000 /
max_requests_jitter=50 / timeout=1200`，**没有 `preload_app`**，所以 worker
重启时会重新 import `app.py`；这既是自然轮换可行的原因，也意味着 6a/6b
都不需要重启 master。

无论哪种方式，生效前后都必须跑第 2 步硬门禁：会破坏在途任务的不是「重启
命令」本身，而是 worker 被替换时恰好有人传到一半。

## 七、验收判据

1. `/upload_progress?filename=Q2026111000243.zip` 带合法凭据 → 200，
   `found=true / status=done / progress=100`，`start_time` 带 `+00:00`；
2. 非法产物名 → 400；不存在的工单 → 200 `found=false`；
3. 调用前后会话目录文件集合与内容 sha256 不变（只读性）；
4. 平台 `GET /api/v1/log-collect/admin/tasks/{id}/destination-progress`
   对 P1 从 `unsupported` 变为 `done` 或 `uploading`；
5. 真浏览器打开 admin「日志收集 → 日志上传」，P1 行「日志服务器真实进度」
   显示真实观测而非「不支持进度查询」；
6. 端点日志带 `trace=` 与 `X-Request-ID` 响应头（唯一调用链）；
7. 整个窗口内 P1/P2 代传无 401/500，`systemctl is-active acli-5888=active`。

## 八、回滚

```bash
ssh 192.168.0.2 'APP=/acli/app.py bash /tmp/rollback.sh'
# 再让 worker 生效（同第六节），并复核 sha256 回到基线
```

平台侧无需回滚：`SangforRelayClient.upload_progress()` 对 404 就地降级为
`unsupported`，回滚 5888 后管理页只会显示「不支持进度查询」，不会报错、
更不会把任务写成失败。

## 九、已知限制

- 会话 `status=failed` 时端点会如实回传 `error`，但平台 `_map_progress`
  目前把非 `uploading`/`done` 的状态统一显示「暂无记录」。这是 PR #1478
  的既有语义，本次不改平台代码；如需精确呈现上传失败，另开迭代。
- 归档兜底只能给出落盘事实（`size` / `mtime` / `target_path`），`digest`
  为空；会话被回收后 sha256 只能查 `/upload_status/<upload_id>` 或归档索引。
- 端点每次扫描整个会话目录（当前约 22 个文件）。平台侧 30s 轮询 + 10s
  TTL 缓存已限流；若会话数增长到千级，应改为按产物名建只读索引。

## 十、两个实测踩过的坑（改脚本前先读）

1. **`/proc/net/tcp` 状态码**：`01` 才是 ESTABLISHED，`0A` 是 **LISTEN**。
   写成 `0A` 会把 5888 自己的监听 socket 当成在途上传，硬门禁永远拒绝。
   `preflight_restart.py` 现在同时回报 `states` 分布，便于人工复核。
2. **`difflib` 按行展开**：`unified_diff` 的输入必须是逐行列表；把整块
   代码当一个元素会生成错误行数的 hunk 头，`patch --dry-run` 直接 FAILED。
   `apply.sh` 的三道门禁（基线 sha256 / dry-run / `py_compile`）就是为在
   打到生产文件前拦住这类错误。

另外，远程核对 `SHA256SUMS` 要加 `--ignore-missing`：`snippet`、
`build_patch.py`、`README.md` 是仓库侧文件，本就不该传到现网。
