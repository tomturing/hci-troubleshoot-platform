---
status: resolved
category: deploy
audience: dba, developer, operator, reviewer, release
last_updated: 2026-08-14
owner: team
---

# hci_sim PreSync Hook 死锁导致数据库未创建事故复盘

关联需求：hci_sim 独立数据库隔离（见 `2026-08-10-hci_sim独立数据库隔离方案.md`）
关联验证：`2026-08-13-主库-agent_test-僵尸表清理验证.md`

## 1. 现象

- dev 环境 `hci_sim` 数据库长期未创建，`agent_test_*` 僵尸表未从主库清理。
- ArgoCD `hci-platform-dev` 应用状态 `OutOfSync`，operation 从 2026-08-10 起持续 `Running`。
- 卡死对象：`waiting for completion of hook batch/Job/hci-sim-db-migrate-20260806-1304-6`。
- Job 的 Pod 卡在 `ContainerCreating` 约 4 天，事件：`configmap "hci-sim-db-migrations" not found`。

## 2. 第一性原理根因

ArgoCD PreSync hook 存在有向依赖图：带 `argocd.argoproj.io/hook: PreSync` 注解的资源在
PreSync 阶段创建，等其全部成功后才进入 Sync 阶段应用普通资源。

- #715/#716（`f7758aee`）提交时，`hci-sim-db-migrate` Job 有 `hook: PreSync`，但
  `hci-sim-db-migrations` ConfigMap 与 `hci-sim-db-credentials` Secret **当时没有**该注解，
  被归类为普通资源。
- env #36 推送 `hciSimDatabase.enabled: true` 于 2026-08-10 15:12 触发 ArgoCD 自动同步，
  渲染的正是 #716（早于 #717 修复 18 分钟）。
- 同步时 PreSync 阶段只创建了 Job，ConfigMap/Secret 被推迟到 Sync 阶段；而 Job 的 Pod 在
  `ContainerCreating` 时就要硬挂载该 ConfigMap（`Optional: false`）→ 永久等待 → 永不完成。
- Job 不成功也不失败（非 HookFailed），`hook-delete-policy` 不触发；ArgoCD 永远等待该 hook，
  后续所有资源（含 #717 修复本身）无法应用。

即：**ArgoCD 自动部署了，但部署的是修复前坏版本；坏版本让 PreSync hook 自我死锁，而
automated/selfHeal 无法越过卡死 hook 把修复版本部署上来。**

## 3. 对抗性审查：为什么 4 天不自愈

| 机制 | 期望 | 实际 |
|---|---|---|
| `automated` | 新 commit 自动 sync | 已触发，但 sync 的是坏版本 |
| `selfHeal` | 修已应用资源漂移 | 对卡在 hook 前的整次 operation 无效 |
| `hook-delete-policy: HookFailed` | hook 失败自动删 | Pod 一直 ContainerCreating，非失败态，不触发 |
| operation 重跑 | 新 commit 重 sync | operation 一直 Running，无新 htp commit 改变渲染 |

## 4. 处置（2026-08-14）

1. 删除卡死 Pod 与 Job：`hci-sim-db-migrate-20260806-1304-6`。
2. 清空应用 `operationState` / `operation`，触发基于 main（已含 #717/#718 修复）的 fresh sync。
3. 新 sync 成功：ConfigMap/Secret（带 PreSync 注解）先于 Job 应用；`hci_sim` 库创建，
   控制面 schema 就位；主库 `agent_test_*` 15 张表清理为 0。
4. 清理残留僵尸 Job。

> 关于 copy 阶段：主库 `agent_test_*` 已被 contract 阶段物理 DROP（早于 copy 执行，属顺序
> 错位），源数据不可得。`hci_sim` 当前为预期空壳终态（僵尸表本就为空、backend 零引用），
> 故未执行 `scripts/db/hci-sim-copy.sh`（执行只会拷 0 行制造假成功）。

## 5. 防御性加固（本 PR）

- `hci-sim-db-migrate-job.yaml`：ConfigMap volume 改 `optional: true`；启动脚本最前插入
  迁移 SQL 前置校验，文件缺失/为空时显式 `exit 1` → 触发 `HookFailed` 自愈，而非永久
  `ContainerCreating` 死锁 ArgoCD。
- 建议后续：为 ArgoCD 应用增加 hook 超时告警；对 PreSync hook 依赖资源统一加 `hook: PreSync`
  + `sync-wave` 保证有序。

## 6. 验收

- 本地 `helm template` 渲染确认 ConfigMap/Secret 含 `hook: PreSync` 且 SQL 非空。
- 集群 `hci-sim-db-migrate` Job `succeeded=1`，`hci_sim` 库存在，主库 `agent_test_*` count=0。
