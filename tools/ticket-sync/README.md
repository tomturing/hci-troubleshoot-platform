# HCI 工单日志同步服务（ticket-sync）

宿主机常驻服务：把日志源服务器上的工单归档（`<工单号>.zip`）通过 SSH 拉回、
逐层解压，并原子发布到本地目录 `/sf/tickets/<工单号>/`，供 DSH 诊断工作台
通过 hostPath 只读挂载使用。

> 设计文档：`docs/solution/events/2026-10-23-工单日志宿主机同步服务.md`
> 自动巡检与清理：`docs/task/events/2026-10-29-ticket-sync自动巡检与清理.md`

## 1. 服务介绍

### 1.1 为什么是宿主机服务，不是 k3s 容器

该能力被刻意放在集群之外，作为 systemd 常驻服务运行：

| 判据 | 宿主机服务（本服务） | 容器内服务 |
|---|---|---|
| 写 `/sf/tickets` | 本地路径直接写 | 需 hostPath 挂到该容器 |
| 修改生效 | 重启进程，秒级 | 重建镜像 + 滚动更新，分钟级 |
| 工具链 | 宿主 GNU `unzip`/`tar`（支持 `-P`） | 基础镜像决定，可能缺 `-P`/`-Z1` |
| 对现有服务影响 | 零 | 需改 Deployment/镜像/RBAC |

代价是脱离 GitOps（属"宠物"部署），因此**升级通过本目录的部署脚本完成，不走 ArgoCD**。

### 1.2 数据流

```
浏览器 admin-ui /dsh-diagnosis
      │  POST /ticket-sync/jobs  （nginx 反代到宿主机 18080）
      ▼
宿主机 192.168.0.4:18080  hci-ticket-sync（Python + uv + systemd，User=node）
      │
      ├─ 每 30s SSH 扫描 /sf/log 当天归档，本地缺失则自动入队
      ├─ 每天清理 /sf/tickets 下超过 7 天的工单目录
      ├─ ① rsync over ssh 拉取 → /sf/tickets/.tmp/<id>-<uuid>/
      ├─ ② 逐层解压、逃逸清理、配额校验
      └─ ③ rename 原子发布 → /sf/tickets/<工单号>/
      ▼
/sf/tickets/<工单号>/  ──hostPath(只读)──►  DSH Pod  /data/tickets/<工单号>/
```

### 1.3 目录契约

```
/sf/tickets/
├── <工单号>/                 最终产物（不套额外层级，DSH 直接选这一层）
│   └── .sync-meta.json       同步事实：completedAt / bytes / files / hasLogs
├── .tmp/<id>-<uuid>/         解压中间态，服务启动时清空
├── .trash/<id>-<时间戳>/      被替换的旧版本（保留最近一次）
└── .ticket-sync.db           SQLite：任务历史 + 产物用量
```

- 产物不套额外层级，避免用户多点一层。
- `.tmp`/`.trash` 以点开头，文件选择器默认不展示。
- `<工单目录>/.sync-meta.json` 是**清理与容量判断的唯一事实来源**：`completedAt` 决定保留期、
  `bytes` 免去每日全盘统计、`hasLogs` 决定容量淘汰顺序。目录 mtime 只在没有
  meta 的历史目录上兜底——它会被外部写操作改写，不能作为判据。

### 1.4 任务状态机

```
queued ──► fetching ──► extracting ──► ready
   │           │            │
   └───────────┴────────────┴──► failed
   │
   └──► cancelled
```

- 同一工单串行：重复提交返回 `409 job-running`；全局并发信号量默认 `2`。
- 任务状态落 SQLite（`<产物根>/.ticket-sync.db`），重启后 `/jobs`、`/artifacts` 仍能查到历史。
  重启时遗留的 `queued/fetching/extracting` 会被改写为 `interrupted`——它与 `failed`
  必须区分：前者重新触发通常就能成功，后者才需要排查日志源。
- `.tmp` 半成品在启动时清空，但产物与历史不受影响。

### 1.5 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/jobs` | 创建同步任务，body `{"ticketId":"Q2026102100147"}`，返回 202 / 400 / 409 |
| `GET` | `/jobs/{ticketId}` | 查询阶段与进度（字节、文件数、当前归档、错误码） |
| `GET` | `/artifacts` | 本地工单目录 + 自动/手动任务总览（DSH 控制台使用） |
| `DELETE` | `/jobs/{ticketId}` | 取消任务并清理 `.tmp` |
| `GET` | `/health` | 健康、磁盘余量、活跃任务数 |
| `GET` | `/metrics` | Prometheus 文本指标：磁盘余量、活跃任务、任务分布、维护耗时与累计淘汰量 |

工单号唯一合法形态：`^Q\d{13}$`（`Q` + `YYYYMMDD` + 5 位当天序号）。服务端强制校验，
不接受任何路径分隔符，因此可直接拼进 `/sf/log` 与 `/sf/tickets` 路径。

### 1.6 自动巡检与保留期清理

- 维护协程**首次等待一个周期**（默认 30s），避免启动时与网络/挂载初始化争抢。
- 之后按 `TICKET_AUTO_SYNC_INTERVAL_SECONDS` 扫描远端当天归档，仅当**本地工单目录
  不存在**时才入队（避免时钟偏差导致重复拉取）；需要重拉仍用手动 `POST /jobs`。
- 每天首次巡检时删除超过 `TICKET_RETENTION_DAYS`（默认 7）天的 `Q\d{13}` 目录及
  `.trash` 旧版本，跳过 `.tmp`、非工单目录和正在同步的工单。判据是
  `<工单目录>/.sync-meta.json` 的 `completedAt`。
- **对账必须先于过期判断**：历史目录的 meta 只有 bytes/hasLogs、没有 `completedAt`，
  此时只能回退目录 mtime；而 mtime 会被 DSH 往产物里写报告、人工 touch 刷新，
  直接拿它当完成时间，一份老产物每天都会被算成"刚同步完"，永远不满足保留期
  （`Q2026102101911` 是 10-21 的包，10-30 被刷新后 64.3 GiB 一直没回收，
  `ticket_sync_expired_removed_total` 恒为 0）。所以每日维护先对账把当时的
  mtime 固化成显式 `completedAt`，此后再被写入也不再漂移。
- `completedAt` 只接受 `(0, now+5min]` 区间内的数值：`true` 是 int 子类、
  会被当成 1970 年而误删，`0` 同理，未来时间则永远不会过期，一律按缺失处理。
- 产物用量记在 SQLite 的 `artifacts` 表。每日对账时只处理"目录 mtime 变过**或判据
  尚未固化**"的工单，其余跳过。跳过前仍读一次 meta（几十字节的小文件），因为
  "`completedAt` 有没有固化"本身就是判据——第一版补丁把它漏在跳过条件之外，
  结果存量的 mtime 从上次对账起就没再变过，被直接跳过，37 个目录 0 个被固化。
  真正昂贵的体积扫描仍只在 mtime 变化时进行，容量判断由 SQL 聚合完成。
  正在同步的工单本轮跳过，但仍算"见过"，否则末尾的 `known - seen` 会把它
  刚写好的记账行删掉。
- 容量超出 `TICKET_MAX_TOTAL_BYTES` 时淘汰，顺序为**先非日志载荷、再最旧优先**：
  一个 65 GiB 的 dump 工单不该挤掉多个可用日志工单。
- 上述两项都是**阻塞 IO，必须在线程池里执行**。2026-10-31 00:00:07–00:03:35
  的实测事故就是同步遍历 550 GiB 把事件循环钉死，期间所有 HTTP 请求排队。
  改动后维护耗时会写进 `/metrics` 的 `ticket_sync_maintenance_duration_seconds`。
- 扫描失败只记日志，不中断服务，下一轮自动重试。

### 1.7 解压能力与降级

解压交给宿主 GNU `unzip` / `tar` 与 p7zip（`7z`）：

| 格式 | 工具 | 说明 |
|---|---|---|
| `.zip` | `unzip -P` | 支持 ZipCrypto 密码 |
| `.tar.gz` / `.tgz` / `.tar` | GNU `tar` | — |
| `.rar` | `7z x` | 依赖 p7zip 内置 Rar/Rar5 解码器 |

三类"单点问题"不会升级为整包失败，但都会留下可追踪的告警：

| 情形 | 处理 | 可见字段 |
|---|---|---|
| 归档 < 64 字节（采集端写坏的占位包） | 删除并跳过 | 日志 `跳过占位=N` |
| `.rar` 解不开（unrar 与 p7zip 都不支持该压缩方法） | 保留原件，继续其它归档 | 日志 `无法解压=N`、`job.message` |
| 归档只含转储（vmcore / kdump / `.core` / `.dump` / `.dmp`），无日志清单 | 下载前识别，直接跳过 | 错误码 `non-log-payload` |
| 解压后产物里既无 `log_list.json` 也无 `sp_*` | 仍发布，但 `job.message` 明确提示"可能仅含转储文件" | `job.message` |

嵌套层数上限（`TICKET_MAX_DEPTH`）是**停止下钻的护栏，不是整单失败的判据**：
达到上限时保留已解出的产物、把剩余归档计入 `depth_truncated` 并在 `job.message`
里提示"N 个归档超出嵌套层数上限，未继续解压"。理由是主体日志都在浅层，
为几个深层附件回滚整个工作目录会让用户拿不到任何日志。

RAR 解压走双通道：优先 `unrar`（rarlab 静态二进制，RAR5 全方法），失败再回退
`7z`。宿主机没有 root，`tools/ticket-sync/deploy/install-user.sh` 会把 unrar 装到 `~/.local/bin`，
unit 里显式补了 `PATH`——否则 `shutil.which("unrar")` 会静默落空。

此外每层解压后都会补权限：文件补属主读写位、目录补执行位。源端
`sf/log/kubelet`、`sf/log/containerd` 等目录权限常是 `drw-r--r--`，
`tar --no-same-permissions` 补不回缺失的 `x`，不处理则 DSH 与本地遍历都进不去，
深层归档会被整片漏扫。日志字段 `补权限=N`。

### 1.8 错误码

| code | 触发条件 | 前端展示 |
|---|---|---|
| `invalid-ticket-id` | 工单号不匹配 `^Q\d{13}$` | 工单号格式错误 |
| `not-found` | 源侧文件不存在 | 该工单在 0.2 上不存在 |
| `too-large` | 归档超过 20 GiB | 工单包过大 |
| `bad-password` | unzip 密码错误 / 密码为空 | 归档密码错误，请联系运维 |
| `archive-corrupt` | 头部校验失败 | 压缩包已损坏 |
| `no-space` | 剩余空间低于红线 | 服务器磁盘空间不足 |
| `quota-exceeded` | 展开后超限（400 GiB / 50 万文件） | 解压体积异常，已中止 |
| `depth-exceeded` | 已废弃：嵌套超限改为停止下钻并保留产物，见 1.7 | 不再出现 |
| `ssh-failed` | ssh/rsync 非零退出 | 无法连接 0.2 |
| `extract-failed` | 其他解压错误 | 解压失败，请查看服务日志 |
| `non-log-payload` | 归档只含 vmdumpcore/.core 等转储（无 `log_list.json`、无 `sp_*.tgz`） | 该工单包不是日志包（仅含转储文件），已跳过 |

### 1.9 配置项

全部来自环境变量（`app/config.py`），未设置时使用代码默认值：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TICKET_SOURCE_HOST` | `192.168.0.2` | 日志源服务器 |
| `TICKET_SOURCE_PORT` | `22468` | SSH 端口 |
| `TICKET_SOURCE_USER` | `acli` | SSH 用户 |
| `TICKET_SSH_KEY` | `~/.ssh/id_ed25519` | 私钥路径（系统级部署常用 `/etc/hci-ticket-sync/id_ed25519`） |
| `TICKET_ARCHIVE_PASSWORD` | 空 | 归档密码；解压前必需，空则任务报 `bad-password` |
| `TICKET_ROOT` | `/sf/tickets` | 产物根目录（须 node:node 可写） |
| `TICKET_LISTEN_HOST` | `192.168.0.4` | 监听地址（不要用 `0.0.0.0`） |
| `TICKET_LISTEN_PORT` | `18080` | 监听端口 |
| `TICKET_MAX_ARCHIVE_BYTES` | 20 GiB | 单工单归档上限 |
| `TICKET_MAX_EXTRACT_BYTES` | 400 GiB | 展开总量上限 |
| `TICKET_MIN_FREE_BYTES` | 60 GiB | 剩余空间红线 |
| `TICKET_MAX_FILES` | `500000` | 文件数上限（实测单工单最多 176,562） |
| `TICKET_MAX_DEPTH` | `12` | 解压嵌套层数上限（实测最深 7 层；超限只停止下钻） |
| `TICKET_CONCURRENCY` | `2` | 全局并发任务数 |
| `TICKET_AUTO_SYNC_ENABLED` | `true` | 是否启用远端自动扫描 |
| `TICKET_AUTO_SYNC_INTERVAL_SECONDS` | `30` | 自动扫描间隔 |
| `TICKET_RETENTION_DAYS` | `7` | 本地工单目录保留天数 |
| `TICKET_MAX_TOTAL_BYTES` | 1 TiB | 产物总量上限（盘已扩到 2.1T），超限时按最旧优先淘汰 |
| `TICKET_MIN_KEEP_DAYS` | `3` | 容量淘汰的最短保留天数（不足则只告警不删） |

配置模板见 `tools/ticket-sync/deploy/ticket-sync.env.example`。**密码只存在宿主机 env 文件，不入库、
不进集群 Secret、不进镜像。**

## 2. 部署方式

服务提供两套等价部署，运行身份都是 `node`（`/sf/tickets` 属主 `node:node`，
换其他身份要么写不进，要么 DSH 读不到）：

| 项 | 用户级 unit（无 root，推荐） | 系统级 unit（生产规范） |
|---|---|---|
| 代码与 venv | `~/.local/share/hci-ticket-sync` | `/opt/hci-ticket-sync` |
| 配置 | `~/.config/hci-ticket-sync/ticket-sync.env` | `/etc/hci-ticket-sync/ticket-sync.env` |
| unit | `~/.config/systemd/user/hci-ticket-sync.service` | `/etc/systemd/system/hci-ticket-sync.service` |
| 管理 | `systemctl --user` | `systemctl` |
| 日志 | `journalctl --user -u hci-ticket-sync` | `journalctl -u hci-ticket-sync` |
| 一键脚本 | ✅ `tools/ticket-sync/deploy/install-user.sh` | ❌ 手动安装 |

### 2.1 前置条件

1. 宿主已装 `uv`；`/sf/tickets` 已创建且属主 `node:node`、权限 `750`
   （`install -d -o node -g node -m 750 /sf/tickets`）。
2. `node` 可通过 SSH 免密登录日志源（`~/.ssh/config` 或密钥已就绪）。
3. 用户级部署需 `loginctl enable-linger node` 才能开机自启（`node` 可自助开启）。

### 2.2 方式 A：用户级部署（无 root，脚本一键完成）

见第 3 节。产物：代码与 `.venv` 在 `~/.local/share/hci-ticket-sync`，配置在
`~/.config/hci-ticket-sync/ticket-sync.env`，unit 安装到
`~/.config/systemd/user/`，由 `install-user.sh` 全自动完成。

### 2.3 方式 B：系统级部署（需要 root）

```bash
# 1) 代码与 venv
sudo install -d -o node -g node /opt/hci-ticket-sync
sudo cp -a tools/ticket-sync/app tools/ticket-sync/pyproject.toml tools/ticket-sync/uv.lock /opt/hci-ticket-sync/
sudo -u node bash -lc 'cd /opt/hci-ticket-sync && uv sync --frozen --no-dev'

# 2) 配置（600，填入归档密码）
sudo install -d -m 700 -o node -g node /etc/hci-ticket-sync
sudo install -m 600 -o node -g node \
  tools/ticket-sync/deploy/ticket-sync.env.example /etc/hci-ticket-sync/ticket-sync.env
# 编辑 /etc/hci-ticket-sync/ticket-sync.env，填 TICKET_ARCHIVE_PASSWORD 等

# 3) unit
sudo install -m 644 tools/ticket-sync/deploy/hci-ticket-sync.service \
  /etc/systemd/system/hci-ticket-sync.service
sudo systemctl daemon-reload
sudo systemctl enable --now hci-ticket-sync
```

### 2.4 部署校验

```bash
# 服务状态
systemctl --user status hci-ticket-sync        # 或 systemctl status（系统级）

# 健康检查（含版本、磁盘余量、活跃任务）
curl -s http://192.168.0.4:18080/health

# 本地工单目录 + 任务总览（PR1364 新增）
curl -s http://192.168.0.4:18080/artifacts

# 手动触发一个工单
curl -s -X POST http://192.168.0.4:18080/jobs \
  -H 'Content-Type: application/json' -d '{"ticketId":"Q2026102100147"}'
```

### 2.5 回滚

```bash
# 用户级
systemctl --user disable --now hci-ticket-sync
rm -f ~/.config/systemd/user/hci-ticket-sync.service
systemctl --user daemon-reload
# 数据目录 /sf/tickets 不删除，避免误伤已同步产物
```

## 3. 部署脚本使用方法

### 3.1 `tools/ticket-sync/deploy/install-user.sh` —— 一键安装 / 一键升级

以当前登录用户（`node`）安装并常驻，**不需要 root**，**幂等可反复执行**：

```bash
bash tools/ticket-sync/deploy/install-user.sh
```

脚本做的事（顺序）：

1. 把 `app/` 与 `pyproject.toml`、`uv.lock` 拷到 `~/.local/share/hci-ticket-sync/`
   （`.venv` 由 `uv` 自己维护，不删除重装）。
2. `uv sync --frozen --no-dev`：严格按 `uv.lock` 安装，不带测试依赖。
3. 若 `~/.config/hci-ticket-sync/ticket-sync.env` **不存在**，从模板生成（600）后
   **中止并提示先填密码**；已存在的 env 文件**永不被覆盖**。
4. 校验 `TICKET_ARCHIVE_PASSWORD` 非空，否则中止。
5. 安装 unit 到 `~/.config/systemd/user/`，`daemon-reload` → `enable` → `restart`。
6. 尝试 `loginctl enable-linger node` 保证开机自启，失败仅告警。

> 注意：`ExecStart` 里的 `${TICKET_LISTEN_HOST/PORT}` 展开**看不到代码默认值**，
> 缺了就是空字符串。因此两个 unit 都用 `Environment=` 预置了 `192.168.0.4:18080`，
> env 文件仍可覆盖。

### 3.2 首次安装

```bash
# 第一次运行会因缺密码中止，并生成模板
bash tools/ticket-sync/deploy/install-user.sh

# 编辑配置，至少填入 TICKET_ARCHIVE_PASSWORD
vi ~/.config/hci-ticket-sync/ticket-sync.env

# 再次运行即完成安装并启动
bash tools/ticket-sync/deploy/install-user.sh
```

### 3.3 升级（同步新代码）

仓库更新后（例如合并 PR1364），**重跑同一个脚本即可完成升级**：

```bash
git -C <repo> pull --ff-only
bash tools/ticket-sync/deploy/install-user.sh   # 拷贝新 app + 重启服务
```

脚本用 `restart` 而非 `--now`，确保已在运行的服务也加载新代码。

### 3.4 常用运维命令

```bash
systemctl --user status hci-ticket-sync      # 状态
systemctl --user restart hci-ticket-sync     # 重启（不重新拷代码）
systemctl --user stop hci-ticket-sync        # 停止
journalctl --user -u hci-ticket-sync -f      # 实时日志（任务日志带 ticket= job=）
journalctl --user -u hci-ticket-sync --since "10 min ago"
```

### 3.5 命令行单工单调试（不经 HTTP）

```bash
cd tools/ticket-sync
uv run python -m app.cli Q2026102100147            # 同步并解压
uv run python -m app.cli Q2026102100147 --dry-run  # 只校验配置/工单号/权限
uv run python -m app.cli Q2026102100147 --force    # 目标已存在时强制重建
uv run python -m app.cli Q2026102100147 --keep-archive  # 保留下载的 zip 备查
```

退出码：`0` 成功；`2` 参数或配置问题；`3` 业务失败（`TicketError`）。

### 3.6 测试

```bash
cd tools/ticket-sync
# 注意：需 PYTHONPATH=.；并覆盖仓库根 pyproject 的 `-n`(xdist)，否则无 xdist 的 venv 会报错
PYTHONPATH=. uv run --frozen pytest -o addopts="" -p no:cacheprovider -q
```

## 4. 安全边界

- 只监听内网地址（默认 `192.168.0.4`），不监听 `0.0.0.0`；入口经 admin-ui 同源反代。
- 服务本身不做认证：能直连 18080 的客户端可绕过页面直接调接口，依赖管理面内网访问控制。
- SSH 私钥 `0600` 属主 `node`；归档密码仅存本地 env 文件，不写日志、不出现在响应中。
- 进程以非特权用户运行，`NoNewPrivileges`、`PrivateTmp`。
- 已知残余风险：`unzip -P` 会让密码出现在该进程 `argv` 中（单用户运维环境，已接受）。
