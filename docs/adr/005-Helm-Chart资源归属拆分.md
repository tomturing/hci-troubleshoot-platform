---
status: accepted
date: 2026-03-20
deciders: team
---

# ADR-005：Helm Chart 资源归属拆分（ArgoCD 资源竞争消除）

## 状态

**已采纳**（2026-03-20），已在 PR `feat/delivery-standardization` 实施完成

---

## 背景

项目使用三个 ArgoCD Application（`hci-platform-dev`、`hci-platform-staging`、`hci-platform-prod`）部署到同一 K3s 集群的不同 namespace。三者均使用同一个 Helm Chart `deploy/helm/hci-platform`，渲染时产生以下问题：

### 冲突资源类型

| 资源 | 冲突原因 |
|------|---------|
| `StorageClass: local-path-retain` | 无 namespace，全局唯一，三方轮番同步随机覆写 |
| `ClusterRole: prometheus-hci` | 同上 |
| `ClusterRoleBinding: prometheus-hci` | 同上 |
| `ClusterRole: promtail` | 同上 |
| `ClusterRoleBinding: promtail` | 同上 |
| `Deployment: prometheus / loki / tempo / grafana` | 均指向 `hci-observability` namespace，三环境共抢同一目标 |
| `DaemonSet: promtail` | 同上 |
| `StatefulSet: postgres / redis` | 三环境各自有独立 PVC，但资源名相同导致混淆 |

---

## 决策问题

> 如何消除多 ArgoCD Application 渲染同名资源导致的所有权竞争？

---

## 评估选项

### 选项 A：保持单 Chart，靠 ArgoCD ignoreDifferences 跳过冲突字段

**劣势**：
- `ignoreDifferences` 是逃避而非解决，随时可能回归
- prune=true 时 ArgoCD 仍然互相删除对方管理的资源

### 选项 B：为每组资源类型创建独立 Chart（✅ 已采纳）

按资源归属特征拆为三层：

```
hci-platform-infra  ← 集群级无 namespace 资源（StorageClass + ClusterRole/CRB）
hci-platform-data   ← 数据层（PostgreSQL + Redis），按环境独立 Application + PVC 保护
hci-platform-obs    ← 可观测性栈（Prometheus/Loki/Tempo/Grafana/Promtail），共用单 Application
hci-platform        ← 原业务层（不再渲染上述资源）
```

---

## 决策

采用选项 B。将原 `hci-platform` Chart 中的集群级资源、数据层资源、可观测性资源分别迁移到独立 Chart。

### 实施原则

1. **原 Chart 只加开关，不删模板**：新增 `clusterResources.manage`、`dataLayer.manage`、`observabilityLayer.manage` 三个布尔开关，默认均为 `false`。保留旧模板以供本地 `helm install` 调试时用 `--set clusterResources.manage=true` 复原。
2. **数据层 prune=false**：PostgreSQL/Redis 的 ArgoCD Application 设置 `prune: false`，防止 PVC 被意外删除。
3. **可观测性层单一 Application**：三环境共用一套 `hci-observability` namespace，由唯一 `hci-platform-obs` Application 管理，消除所有权竞争。
4. **集群级资源打 sync-wave=-10**（待后续 Application 配置完善）：确保 StorageClass/ClusterRole 比业务资源先创建。

---

## 结果

| Chart | ArgoCD Application | prune | selfHeal | 保护目标 |
|-------|--------------------|-------|----------|---------|
| `hci-platform-infra` | `hci-platform-infra`（×1） | true | true | 无（全为无状态配置） |
| `hci-platform-data` | `hci-platform-data-{dev,staging,prod}`（×3） | **false** | true | PostgreSQL/Redis PVC |
| `hci-platform-obs` | `hci-platform-obs`（×1） | **false** | true | Loki/Tempo PVC |
| `hci-platform` | `hci-platform-{dev,staging,prod}`（×3） | true | true | — |

---

## 后续注意事项

- **存量集群迁移**：首次切换时，原三个 Application 渲染的集群级资源会因 `clusterResources.manage=false` 被 ArgoCD prune 掉。需先在集群内手动确认 `hci-platform-infra` Application 已同步成功，再启用原 Application 的 prune。
- **scrapeNamespace**：`hci-platform-obs` Chart 的 `values.yaml` 提供 `scrapeNamespace` 字段，生产环境 ArgoCD Application 需覆盖为对应 namespace（dev→`hci-dev`，prod→`hci`）。
