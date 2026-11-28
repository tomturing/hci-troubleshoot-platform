# 5888 P1 直传鉴权：两阶段开关运维说明

> 当前状态：**阶段一（临时放宽）**，`ACLI_P1_REQUIRE_AUTH=0`，P1 直传可用。
> 最后更新：2026-11-06

## 为什么要有这个开关

acli 的 `remote_transfer.py` 传输层是「深信服域名分块上传」的最小实现：
`http_transfer` / `TransferToSangfor.upload_init` / `chunk_upload` 里 `headers={}`
硬编码、未接 `auth=`；`-u` 只认 3 个 sangfor 域名并硬编码 `/init_upload`。
`create` 会解析 `-H` / `-L` / `-U` / `-P` / 自定义 path，但真正发请求的组件
不用它们。2026-11-06 实测（acli 1.0.0 与 1.0.3 均如此）：

| 参数 | 5888 实际收到 |
|---|---|
| `-H Authorization: Bearer …` | 无任何 Authorization 头 |
| `-L 0:Authorization:Bearer …` | 同上 |
| `-U probe -P probe`（Basic） | 同上 |
| `-u …:5888/p1/probe/` | 请求仍是 `/init_upload`，路径被吞 |

因此只要 5888 强制要凭据，P1 直传 100% 在 `/init_upload` 被 401 打回并降级 P2。

## 两阶段

| 阶段 | `ACLI_P1_REQUIRE_AUTH` | 行为 | 适用 |
|---|---|---|---|
| 一（当前） | `0` | 允许「完全无认证头」的直传请求命中上传链路白名单 | acli 修复前，保证 P1 可用 |
| 二 | `1` | 恢复强制鉴权，匿名直传一律 401 | acli 修好 headers/auth 并分发后 |

## 一键切换（无需改代码）

```bash
sudo /acli/switch_p1_auth.sh on    # 阶段二：恢复强制鉴权
sudo /acli/switch_p1_auth.sh off   # 阶段一：临时放宽
```

脚本写 `/etc/systemd/system/acli-5888.service.d/p1-auth.conf` 并重启服务。
当前同一目录的 `p1-ticket.conf` 保存 `ACLI_P1_TICKET_SECRET`，两者互不覆盖。

## 放宽范围（刻意收紧）

只允许**同时满足**以下条件的请求匿名放行：

1. 开关为 `0`；
2. 请求**完全没有任何认证信息**（无 `Authorization` 头、无 `?token=`）——带错头、
   错票据一律 401，防止「随便发个错头就绕过鉴权」；
3. 路径在上传链路白名单：`/precheck`、`/init_upload`、`/upload_chunk`、
   `/upload_status/<id>`。

**仍强制鉴权**：`/archive_status`（平台归档核验）、其它任何路径。
`/`、`/static/*`、`/healthz` 本来就是白名单。

原有校验全部保留：产物名格式、大小、分片序号、会话清理。每次匿名放行都打
审计日志（`[P1] 匿名直传放行(临时)`，带工单号与来源 IP），`init_upload`
用 WARNING，分片请求用 INFO 以免刷屏。

## 保留的鉴权能力（阶段一也在生效）

- **P1 票据**：`p1.v1.<payload>.<sig>` HMAC 验签、绑定产物名、12h 过期。
  哪个 acli 版本能带出票据，会优先命中票据通道，与放宽互不影响。
- **全局 Bearer**：`ACLI_UPLOAD_TOKEN`，P2 平台代传与页面人工上传继续用它。
- **归档核验**：`/archive_status` + 平台 `verify-archive` 端点。

阶段二只是把「匿名」这条兜底关掉，上述代码一行都不用删。

## 验收记录（2026-11-06）

| 用例 | 结果 |
|---|---|
| 无头 `init_upload` | 200 + upload_id，日志有匿名放行审计 |
| 错头 `init_upload` | 401 |
| 无头 `/archive_status` | 401 |
| 无头 `/upload_chunk` | 400（业务层，已过鉴权） |
| 全局 Bearer `init_upload` | 200 |
| P1 票据 `init_upload` | 200 |
| 启动日志 | `[P1] 鉴权模式: relaxed(临时匿名直传放行)` |

## 回滚 / 备份

- 代码备份：`/acli/app.py.bak.p1relax`（本次改动前）、`.bak.p1basic`（上一轮）
- 关闭票据通道：删 `p1-ticket.conf` 并重启（平台侧会停止签发票据，自动走 P2）
- 完全回到加鉴权前：把 `p1-auth.conf` 设为 `0` 且移除 `ACLI_UPLOAD_TOKEN`

## 阶段二前置条件

1. acli `remote_transfer.py` 修好：`headers=remote_dict["headers"]` 与
   `auth=` 接进 `requests.post(...)`，`upload_init` / `chunk_upload` 都要透传；
2. 新版 acli 已分发到目标节点（平台 `acli_sync` 会自动安装，实测 45 秒铺 2 节点）；
3. 先跑一单确认 5888 出现 `[P1] 票据通过`，**再**执行 `switch_p1_auth.sh on`。

顺序不能倒：先开强制鉴权而节点还是旧 acli，P1 会立刻全量退回 401。
