---
status: active
category: deploy
audience: developer, operator
last_updated: 2026-04-06
owner: team
---

# 部署事件文档

> 本目录存放部署阶段的事件文档，记录"部署相关的决策、改进计划和运维复盘"。

---

## 文档列表

| 文件 | 日期 | 说明 |
|------|------|------|
| 2026-08-07-CI发布链路按影响范围收敛方案.md | 2026-08-07 | main push 动态镜像 matrix、最小环境同步、超时/供应链固定与手动时延基线 |
| 2026-07-27-运行时代码完整性修复与防护.md | 2026-07-27 | 清理遗留源码热补丁，并以 Kubernetes 准入策略阻止镜像源码再被卷覆盖 |
| 2026-02-15-本地开发指南.md | 2026-02-15 | 本地开发环境搭建指南 |
| 2026-03-02-运维指南.md | 2026-03-02 | 运维操作指南 |
| 2026-03-19-发布工作流方案选型.md | 2026-03-19 | GitOps vs Agent 自动驾驶方案选型决策 |
| 2026-03-20-Helm-Chart资源归属拆分.md | 2026-03-20 | Helm Chart 资源拆分方案 |
| 2026-03-27-本地K3s部署指南.md | 2026-03-27 | 本地 K3s 部署指南 |
| 2026-03-28-K8s部署设计初版.md | 2026-03-28 | K8s 部署设计初版 |
| 2026-03-28-Task28-Prometheus进K3s.md | 2026-03-28 | Prometheus 进 K3s 部署任务 |
| 2026-03-28-Task29-Prometheus进K3s.md | 2026-03-28 | Prometheus 进 K3s 部署验证任务 |
| 2026-03-29-K3s集群健壮性改进计划.md | 2026-03-29 | K3s 集群健壮性改进计划 |
| 2026-03-29-K3s集群健壮性改进决策.md | 2026-03-29 | K3s 集群健壮性改进核心决策提炼 |
| 2026-03-29-K3s集群运维复盘.md | 2026-03-29 | K3s 集群运维复盘报告 |
| 2026-03-30-K8s部署规格.md | 2026-03-30 | K8s 部署规格定义 |
| 2026-04-01-dbmate数据库版本管理.md | 2026-04-01 | dbmate 数据库版本管理方案 |
| 2026-04-27-k3s重启后prod-TLS与Grafana入口点修复.md | 2026-04-27 | k3s 重启后 prod TLS 证书丢失与 Grafana 入口点修复 |
| 2026-04-27-staging-prometheus-reload-hook修复.md | 2026-04-27 | staging 环境 Prometheus reload Hook 卡死修复 |
| 2026-10-31-网关空窗量化与观测持久化PDB覆盖.md | 2026-10-31 | 入口网关重启空窗客户端实测（1.07 s / 157 次拒连 / 0 次 5xx）与「网关自身重启对 5xx 指标与 accesslog 是结构性盲区」；Prometheus TSDB 从 emptyDir 改 10Gi PVC + Recreate；Traefik 5xx accesslog 入 Loki；PDB 列表化覆盖 12 个 values 可控副本的服务（第 13 个 ops-agent-service 副本数写死）+ 不可扩副本渲染期守卫 + CI 门禁。含两项对旧文档的订正（§8.1 「无状态服务」清单、Loki structured_metadata 不等于 label） |
| 2026-10-31-prod误同步与镜像拉取策略固化.md | 2026-10-31 | 关键词通配批量触发 ArgoCD 同步时误命中手工同步的 prod 应用，把积压 8 周的 39 个资源推上生产并触发重建 → Loki ImagePullBackOff 不可用 2 分 28 秒（数据无损、Prometheus 历史指标按设计丢失）。根因：`imagePullPolicy` 缺省时由 tag 推导且写入即固化，Chart 十个容器一个都没声明 → ArgoCD 无从纠正；无外网集群里 `Always` = 重建必挂。修法：顶层默认 + 逐容器显式渲染 + CI 禁止 `Always`（不变量四） |
| 2026-11-01-网关5xx度量维度盲区与Loki抓取补齐.md | 2026-11-01 | 复核 staging 时发现：Service 无 endpoint 的 503 只进入口维度（`traefik_entrypoint_requests_total`），service 维度序列数为 0 → 既有三条网关告警对整类配置故障结构性失明，且表现为「0 告警」。新增 `GatewayRouterServerErrorFirstSeen` + `promtool test rules` 单元测试（正例 + 两个反例）；给 Loki 补 `prometheus.io/scrape` 注解，使「metadata 分组发生在写入侧还是查询侧」这个开放项终于可查（此前「拿不到数字」一直被读成「没有该指标」）。含一次自写假验证的记录（文本替换删错对象） |

---

## 相关目录

- `../` - deploy 主干文档（部署指南.md、发布指南.md 等）
- `../pitfalls/` - 部署类避坑指南
- `../../task/events/` - 任务事件文档
- `../../verify/events/` - 验证事件文档

---

*更新日期: 2026-11-01*
