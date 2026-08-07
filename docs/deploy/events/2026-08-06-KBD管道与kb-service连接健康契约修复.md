---
status: completed
category: deploy
audience: developer, operator
last_updated: 2026-08-06
owner: team
---

# KBD 管道与 kb-service 连接健康契约修复

## 事件结论

PR #683 修复的是“未识别图片的占位符被正文重建逻辑擦除”，该修复已经进入 staging 镜像；本次异常发生在更早的连接与编排层，因此合入 #683 后仍会出现：本地 pipeline 把 port-forward 固定连到 `hci-dev`，而 staging 集群只有 `hci-staging`；live kb-service 丢失 Kubernetes DNS search domain，无法把数据库短名 `postgres` 补全；三个探针共用不检查数据库的 `/health`，使实例在数据库不可达时仍被标记 Ready。

## 现场证据

- `data-pipeline/kbd/logs/kbd_20260806_005512.log`：Fetch 11 成功、Import 11 错误，port-forward 命令明确使用 `-n hci-dev` 并以 retcode 1 退出；Vision/Classify/Extract 随后产生级联失败。
- staging 的 `svc/kb-service` 与 EndpointSlice 位于 `hci-staging`，正确 namespace 的 port-forward 可建立。
- `kbd_entry`、`kb_category`、`sop_document` 直接数据库查询均成功，排除数据表缺失或查询语句本身错误。
- kb-service 日志为 `socket.gaierror: [Errno -2] Name or service not known`；Pod 内 `postgres` 解析失败，而 `postgres.hci-staging.svc.cluster.local` 正常。
- live Deployment 为 `dnsPolicy: None`，只有 nameservers 与 ndots、没有 searches；`/health` 在数据库 DNS 故障时仍返回 200。

## 修复决策

### Pipeline 环境与隧道

- 删除任何静默 `hci-dev` 回退。优先使用显式 `KBD_K8S_NAMESPACE/K8S_NAMESPACE`，其次使用当前 context namespace、ArgoCD 环境角色、唯一 kb-service namespace；无法唯一确定则拒绝运行。
- 隧道创建前验证 Service 与 EndpointSlice；健康检查验证响应身份确实为 kb-service，防止复用被其他程序占用的 8004 端口。
- port-forward 输出写入诊断日志，不再丢到 `/dev/null`；连接级失败只做一次前置失败，并生成逐 support_id 结果。
- PID 文件升级为包含 namespace、port、command、started_at 的 JSON；终止前核对 `/proc/<pid>/cmdline`，并以文件锁串行化并发创建，避免 PID 复用误杀与端口争用。

### Pipeline DAG 与退出语义

- Import 只以本次 ingest API 的逐 ID 结果判定成功。数据库存在历史 support_id 不能掩盖本次 override 失败。
- 只有本次前置阶段成功的 ID 才能进入 Vision、Classify、Extract、Audit；被依赖阻断的状态明确记录为 `skipped`，不再保留 `pending` 或制造级联请求。
- progress 覆盖完整六阶段；关键阶段存在 `failed/needs_review` 时 pipeline summary 失败，CLI 返回非零，便于 shell/CI 可靠识别。

### DNS 与探针

- kb-service Helm desired state 显式声明 `dnsPolicy: ClusterFirst`，由 Kubernetes 为当前 namespace 注入搜索域，且能覆盖历史 `dnsPolicy: None` 漂移；没有任何 staging 硬编码。
- liveness、readiness、startup 分别指向 `/health/live`、`/health/ready`、`/health/startup`。
- readiness 的 `SELECT 1` 增加 2 秒上限；数据库或 DNS 不可用时返回 503，只从 Service 流量池摘除，不触发 liveness 重启风暴。

## 环境隔离证明

同一 Chart 分别以 `global.namespace=hci-dev/hci-staging/hci-prod` 渲染，kb-service 的 metadata namespace 随值变化，DNS policy 均为 `ClusterFirst`，三级探针路径一致；代码和模板没有 `hci-staging` 运行逻辑硬编码。Pipeline 只有在自动发现唯一目标或操作者显式指定时才连接，候选不唯一会 fail closed，因此不会误写 dev/prod。

## 验收清单

- Python 单测覆盖 namespace 角色解析、无效显式 namespace 不回退、PID 复用、连接级逐 ID 失败、历史 DB 行不得掩盖本次导入失败、健康 readiness 超时。
- `ruff check`、KBD 定向 pytest、kb-service health pytest、`helm lint`、`helm unittest --strict`。
- `helm template` 渲染 dev/staging/prod，检查 namespace、ClusterFirst 与三类 probe。
- 合并并由 GitOps 收敛后，在 staging 验证 `postgres` 短名解析、`/health/ready`、KBD pending/categories 接口，再以案例 27582 重跑原命令。

## 已知独立阻塞

staging ArgoCD 当前还存在历史 `agent-service` Python 源码 ConfigMap volumeMount。该字段由集群内临时 patch 管理，不在当前 Helm desired manifest 中；任何 Deployment 更新都会被运行时代码完整性准入策略拒绝。此漂移必须按 D-020 精确移除或以不可变镜像替代，不能为本次 KBD 修复绕过准入策略。它不属于 PR #683 或本次 KBD 代码根因，但会阻止 Application 整体同步，发布前必须处理。
