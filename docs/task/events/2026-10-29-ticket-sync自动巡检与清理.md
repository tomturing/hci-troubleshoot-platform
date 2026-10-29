---
status: active
category: task
audience: developer
last_updated: 2026-10-29
owner: 平台组
related:
  - ../../solution/events/2026-10-23-工单日志宿主机同步服务.md
---

# ticket-sync 自动巡检与本地清理

## 变更历史

| 日期 | 版本 | 变更内容 |
|---|---|---|
| 2026-10-29 | v1.0 | 增加当天远端归档自动同步、按工单互斥和本地七天保留期清理 |
| 2026-10-29 | v1.1 | 增加 `/artifacts` 总览接口和 DSH 控制台目录/任务状态展示 |

## 需求

ticket-sync 常驻服务每 30 秒检查日志源服务器 `/sf/log`，发现当天生成且本地尚未同步的
`/sf/log/<工单号>/<工单号>.zip` 后，复用既有同步、分层解压和原子发布链路。每天检查一次
`TICKET_ROOT` 下的工单目录，删除超过 7×24 小时的目录。管理台原有手动 `POST /jobs`
同步入口、进度查询和取消能力保持不变。

## 实现约束

- 远端扫描通过既有 SSH 账号执行 GNU `find`，只接受顶层工单归档的固定路径格式；扫描失败
  只记录错误并等待下一轮，不影响手动同步。
- 自动任务与手动任务共用 `JobRegistry._lock` 和 `RUNNING_PHASES`。自动扫描在检查本地
  mtime 与登记任务之间不释放锁，因此同一个压缩包不会因为 30 秒轮询重复拉取或解压。
- 以本地工单目录是否存在作为已同步标记；目录不存在才自动入队。避免源服务器与本地
  时钟偏差导致同一个归档被每轮重复拉取；需要重拉时仍保留手动同步入口。
- 清理只处理根目录下符合 `Q\d{13}` 的直接子目录，以及 `.trash` 下服务生成且超过期限
  的旧版本目录；跳过 `.tmp`、非工单目录和正在执行的工单。清理异常单独记录，不中断
  后续巡检。
- 首次启动等待一个巡检周期，避免网络、挂载点尚未就绪时立即触发远端 SSH；服务退出时
  取消维护协程。

## 配置

`tools/ticket-sync/deploy/ticket-sync.env.example` 新增：

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `TICKET_AUTO_SYNC_ENABLED` | `true` | 是否启用远端自动扫描 |
| `TICKET_AUTO_SYNC_INTERVAL_SECONDS` | `30` | 自动扫描间隔 |
| `TICKET_RETENTION_DAYS` | `7` | 本地工单目录保留天数 |

## 验证

- `23 passed`：远端路径解析、过期目录清理、活动工单保护、重复自动入队和已有目录跳过。
- `ruff check app tests` 与 `ruff format --check app tests` 通过。
- 手动同步 API 未改路径和请求契约；前端在保留原手动操作的基础上增加目录总览卡片。
- DSH 控制台新增「本地工单目录」卡片：`ready` 显示 `/data/tickets/<工单号>`，`fetching`
  显示同步字节进度，`extracting` 显示解压体积、文件数和当前归档；自动任务也会显示在此处。
