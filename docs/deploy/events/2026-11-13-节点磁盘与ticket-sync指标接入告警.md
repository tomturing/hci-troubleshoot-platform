---
status: active
category: deploy
audience: developer
last_updated: 2026-11-13
owner: team
related:
  - ../../solution/events/2026-11-12-ticket-sync清理机制与磁盘水位治理.md
  - ../../solution/可观测性设计.md
---

# 节点磁盘与 ticket-sync 指标接入告警

## 文档信息

- **版本**: 1.0
- **创建日期**: 2026-11-13
- **状态**: 已实施，合并后由 ArgoCD 自动同步
- **关联事件**: 2026-11-12 单节点磁盘压力导致全栈不可用 36 分钟（该事故全程零告警）

---

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-11-13 | v1.0 | 首版：补 node-exporter 数据源、静态采集 ticket-sync、新增 5 条余量/熔断/采集自检规则与 5 个 promtool 用例 | 本文件 |

---

## 1. 问题：告警链上的两个 0

2026-11-12 全栈中断 36 分钟，一条告警都没出。事后核查发现两个独立原因，任一个都足以让告警彻底失效：

| 环节 | 现状 | 后果 |
|---|---|---|
| `DiskUsageHigh` 的数据源 | 依赖 `node_filesystem_size_bytes` / `node_filesystem_avail_bytes`，而集群里只有 kube-state-metrics（Pod 元数据），**从来没有 node-exporter** | 实测 `count(node_filesystem_size_bytes)` 返回**空向量**（不是 0）。规则自上线起就没有执行过任何一次判断 |
| `ticket-sync` 的指标 | 它是 systemd 用户服务（`192.168.0.4:18080`），不在 k8s 内 | `role: pod` 发现不到它。它的 `/metrics` 里明明有 `ticket_sync_disk_free_bytes`，但没有任何组件在抓 |

`MemoryUsageHigh` 同理，依赖 `node_memory_*`，同样空。

**第一性原理**：告警是一个乘法链——`规则 × 数据源 × 通知通道`。乘积里任何一项为 0，整条链就是 0，而前两项为 0 时，告警系统的表现与"一切健康"完全无法区分。所以验收告警不能只看"规则已加载"，必须验证"该指标有序列"。

同时发现第三个问题：可观测栈与业务同节点，11-12 那次它自己被一起驱逐，当前 Prometheus/Alertmanager 实例的 uptime 只有 8 小时——**事故期间的告警历史和告警能力同时消失**。

## 2. 改动

### 2.1 node-exporter DaemonSet

文件：`deploy/helm/hci-platform-obs/templates/node-exporter.yaml`、`deploy/helm/hci-platform-obs/values.yaml`

- `hostNetwork: true`：抓取地址就是节点 IP，不需要 hostPort 映射
- hostPath 只读挂载 `/proc`、`/sys`、`/`，**不 privileged**，`runAsNonRoot: 65534`，drop ALL capabilities
- `--path.rootfs=/host/root` 是关键参数：少了它，`node_filesystem` 报的是容器内 overlay，对节点磁盘告警毫无意义
- `--collector.filesystem.mount-points-exclude` 排除容器运行时与虚拟文件系统，避免噪声
- **`var/lib/kubelet/.+` 必须排除**（实测发现）：k3s 的 local-path Provisioner 给每个 PVC 建了一个 bind mount，本节点有 **19 个**，fstype 全是 `ext4`、size 全是那块 2.1T 盘。不排除的话 `node_filesystem_*` 会对同一个盘产生 20 条序列，`NodeDiskFreeLow` 一次 firing 20 条、`DiskUsageHigh` 同样重复判定 20 次——告警风暴，且真正的根盘告警被淹没
- `updateStrategy: OnDelete`：单节点集群里滚动更新等价于中断，宁可手动删 Pod 也不让一次 apply 断档
- Pod 自带 `prometheus.io/scrape=true` 注解，由既有 `hci-services` job 通过 `role: pod` 自动发现，**不需要改 Prometheus 的发现逻辑**

### 2.2 ticket-sync 静态目标

文件：`deploy/helm/hci-platform-obs/templates/configs.yaml`

新增 `job_name: 'ticket-sync'`，`static_configs` 指向 `192.168.0.4:18080`，`scrape_interval: 30s`（与它的清理轮次一致，更密没有信息增量）。

**对抗性说明**：静态 IP 意味着节点 IP 变更时必须同步改这里。若忘了改，表现为 `up{job="ticket-sync"} == 0` → `TicketSyncDown` 误报，而不是静默失效——这是有意选择的方向（误报可被发现，静默不可）。

### 2.3 新增 5 条规则

文件：`deploy/helm/hci-platform-obs/rules/alert_rules.yml`，新组 `ticket_sync`。

| 规则 | 判据 | 级别 | 阈值来源 |
|---|---|---|---|
| `TicketSyncDown` | `up{job="ticket-sync"} == 0` for 2m | critical | 2m 覆盖一次 systemd 重启（实测 4 秒恢复） |
| `TicketSyncDiskLow` | `ticket_sync_disk_free_bytes < 500 GiB` for 5m | warning | 早于软阈值 400 GiB 100 GiB，留一轮清理窗口 |
| `TicketSyncDiskCritical` | `ticket_sync_disk_free_bytes < 300 GiB` for 2m | critical | 已进入淘汰区，距熔断 250 GiB 仅 50 GiB |
| `TicketSyncWaterlineBlocking` | `increase(ticket_sync_waterline_blocks_total[10m]) > 0` | critical | 熔断已发生，不是"余量偏低" |
| `NodeDiskFreeLow` | `node_filesystem_avail_bytes{fstype=~"ext4\|xfs"} < 500 GiB and on(...) size > 200 GiB` for 10m | warning | 节点级兜底，覆盖非 ticket-sync 的增长 |

**为什么用余量绝对值而不是使用率**：2.1T 盘的 80% 意味着只剩 420 GiB，那时已经低于 ticket-sync 的 400 GiB 软阈值——报警即事故，没有处置窗口。既有 `DiskUsageHigh`（80%）保留作为其他场景的兜底。

`NodeDiskFreeLow` 必须带 `and on(...) size > 200 GiB`：否则 `/boot`（1 GiB 左右）这类小盘余量天然低于 500 GiB，会天天误报。

### 2.4 promtool 单测

文件：`deploy/helm/hci-platform-obs/rules/alert_rules_test.yml`，新增 5 个用例：

- `TicketSyncDiskLow` 正例（450 GiB，越过 5m for）
- 反例一：600 GiB 高于阈值 → 静默（防止比较方向写反）
- 反例二：恰好 500 GiB 边界 → 静默（边界最容易被后续改动碰坏）
- `TicketSyncWaterlineBlocking` 正例（计数器增长）
- `TicketSyncDown` 正例（up=0 持续）

## 3. 验证

### 3.1 渲染与规则

```
helm lint deploy/helm/hci-platform-obs            → 1 chart linted, 0 failed
helm template obs ... --set global.domain=...     → 退出码 0，DaemonSet 与 static job 均正确渲染
promtool check rules .../alert_rules.yml          → SUCCESS: 24 rules found
promtool test rules .../alert_rules_test.yml      → SUCCESS
```

### 3.2 反向验证（测试有效性）

把 `TicketSyncDiskLow` 的阈值从 500 GiB 临时改成 600 GiB，`promtool test rules` 立刻 `FAILED`（正例不再触发）；还原后 `SUCCESS`。**测试能失败才算测试**，只跑绿的不构成证据。

### 3.3 上线前可行性验证（两个 go/no-go）

| 验证 | 结果 |
|---|---|
| Prometheus Pod 能否访问 `192.168.0.4:18080/metrics` | 可达，取到 `ticket_sync_disk_free_bytes 753458126848` |
| `prom/node-exporter:v1.8.2` 能否拉取 | 13 秒拉取成功（12 MB），当前镜像源无 403 |

### 3.4 上线后验收（必须逐条执行）

```bash
# 1) node-exporter 起来了且被采集
kubectl -n hci-observability get ds node-exporter
kubectl -n hci-observability get pod -l app.kubernetes.io/name=node-exporter

# 2) 关键指标有序列（不是空向量！）
kubectl -n hci-observability exec deploy/prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=count(node_filesystem_size_bytes)'
# 期望等于真实挂载点数（本节点为 2：根盘 2.1T + /boot 1.9G）。
# 若返回 20+，说明 kubelet 的 local-path PVC bind mount 没被排除，会引发告警风暴

# 3) ticket-sync 目标 up
kubectl -n hci-observability exec deploy/prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=up{job="ticket-sync"}'
# 期望 value=1

# 4) 新规则已加载且处于 inactive
kubectl -n hci-observability exec deploy/prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/rules' | grep -o 'TicketSync[A-Za-z]*'
```

## 4. 失效边界（对抗性审查）

| 场景 | 行为 | 说明 |
|---|---|---|
| 节点 IP 变更 | `TicketSyncDown` 误报 | 有意选择：误报可被发现，静默不可 |
| node-exporter 自身崩溃 | `node_filesystem_*` 序列消失，指标变"陈旧" | Prometheus 保留最后值，`DiskUsageHigh` 可能不再变化。缺少"序列陈旧"告警，登记为已知缺口 |
| 飞书机器人被移出群 / URL 轮换 | Alertmanager 收到非 2xx 会重试并计入 `notifications_failed_total`，但**若机器人仍在群但无人看，返回 2xx 即视为成功** | 通道可达性 ≠ 有人看见。需人工确认群里能收到 |
| 可观测栈与业务同节点 | 事故时可能一起被驱逐，告警能力随之消失 | 根治需要把 obs 栈迁到独立节点；当前未做，登记为已知风险 |
| Prometheus 配置变更 | `checksum/config` 触发滚动重启，RWO PVC + `Recreate` 会有秒级抓取空窗 | `TicketSyncDown for 2m` 不会误报；`NodeDiskFreeLow for 10m` 也不会 |

## 5. 关联改动

- `deploy/helm/hci-platform-obs/templates/node-exporter.yaml`（新增）
- `deploy/helm/hci-platform-obs/values.yaml`（新增 `nodeExporter` 段）
- `deploy/helm/hci-platform-obs/templates/configs.yaml`（新增 `ticket-sync` 静态目标）
- `deploy/helm/hci-platform-obs/rules/alert_rules.yml`（新增 `ticket_sync` 规则组，5 条）
- `deploy/helm/hci-platform-obs/rules/alert_rules_test.yml`（新增 5 个 promtool 用例）
- `docs/solution/可观测性设计.md`（v3.10：采集三路、节点与宿主机服务指标、失效模式登记）
- 上游修复：[ticket-sync 清理机制与磁盘水位治理](../../solution/events/2026-11-12-ticket-sync清理机制与磁盘水位治理.md)
