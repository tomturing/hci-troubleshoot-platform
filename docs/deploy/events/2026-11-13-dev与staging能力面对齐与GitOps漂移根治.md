---
status: active
category: deploy
audience: developer, operator, architect
last_updated: 2026-11-13
owner: team
---

# dev 与 staging 能力面对齐：GitOps 漂移与依赖缺口根治

> 一句话：dev 不是「配置差一点」，而是 **GitOps 控制权失效 + 三层镜像漂移 + 运维 provision 缺失** 三种故障叠加；staging 之所以能用，靠的是一套 dev 从未执行的 provision 与持续晋级，而不是配置天然正确。

---

## 1. 硬约束与「一样」的定义

按第一性原理，把诉求拆到不可再分的事实：

| 约束 | 事实 | 推论 |
|------|------|------|
| C1 | 当前 kubeconfig 只有 `default` context（dev 集群），无 staging 凭据 | 无法对 staging 做运行时实测，只能对齐仓库中的声明式能力面 |
| C2 | dev 是 WSL 单机 k3s，staging 是云端集群 | `/sf/log`、域名/TLS、副本数等物理差异不可字节级拉齐；「一样」= **能力集合与服务可用性等价** |
| C3 | ArgoCD Root Application 不被自己管理 | 集群中 root 对象的 `spec` 与 Git 不一致时，Git 里的新子 Application 永远不会自动创建 |
| C4 | `Deployment.spec.selector` 是 K8s 不可变字段 | 期望 selector 与现存不一致时，任何 patch 都永久失败，只能让期望值收敛到现存值 |
| C5 | `Pod Ready` 只反映 kubelet 探活 | 服务可用性必须经 Service ClusterIP 做可证伪实测 |
| C6 | 密钥材料不得入仓 | 缺失的 provision 必须从集群内既有受控 Secret 派生，或由运维注入受控文件 |
| C7 | main 受保护 | 变更必须走 worktree + PR + 全量 CI |

---

## 2. 问题矩阵（全部可证伪）

| 编号 | 现象 | 物理根因 | 证据 |
|------|------|----------|------|
| P1 | `argocd-root` 长期 `OutOfSync`，8 个子 Application 全不同步 | 集群 root 对象 `spec` 无 `syncPolicy`，而 `deploy/gitops/bootstrap/argocd-root.yaml` 有 `automated.prune/selfHeal`；最后一次 operation 停在 2026-08-14 | `kubectl get application argocd-root -o json` |
| P2 | `hci-platform-dsh-dev` 文件在 Git 里、集群里没有；`/dsh/` 返回 502 | P1 的直接后果；`hci-dsh` 命名空间不存在 | `kubectl get applications -n argocd`、`kubectl get ns hci-dsh` |
| P3 | `hci-sim-dev` SyncError，重试 5 次失败 | 单源 Application 缺省以 Application 名当 Helm releaseName，期望 selector `instance=hci-sim-dev` 与现存 `instance=hci-sim` 冲突 | `helm template hci-sim` 与 `hci-sim-dev` 两次渲染对比 |
| P4 | hci-sim Service ClusterIP 探测 `000`，但 PodIP `/healthz` 200 | 失败 sync 把可变的 Service selector 改成了 `hci-sim-dev`，EndpointSlice 无地址；Deployment selector 不可变才让整次 sync 失败 | `kubectl get svc/endpointslice` |
| P5 | hci-sim 三层镜像漂移 | Git digest `0343b9b…`、集群 Application 对象 digest `9307b59…`（仓库与 PR 中均查无来源）、实际 Pod 跑 `hci-sim:dev-local` | `git log`、`grep 9307b597`、Pod image |
| P6 | DSH 即使纳管也会 `Pending` | chart 读顶层 `customerLogs`/`tickets`，而环境仓 dev 把开关写在 `dsh.customerLogs`（死键）；`/sf/log`、`/sf/tickets` hostPath 仍被渲染 | `helm template` 实渲染出现两处 `/sf/` |
| P7 | DSH 即使不 Pending 也会 `CreateContainerConfigError` | deployment 的 `envFrom` 无条件引用 `dsh-llm-credentials` 与 `dsh-plugin-credentials`，但二者渲染条件分别依赖 dev 为空的 `secrets.dshLlmApiKey` 与 `secrets.dshPluginEnv` | `templates/deployment.yaml` 与 `templates/secret.yaml` 条件对比 |
| P8 | hci-sim sync 后会立刻起不来 | `database.existingSecret: hci-sim-runtime-credentials` 在 `hci-sim-dev` 不存在；`hci-sim-credentials` 缺 `INTERNAL_API_TOKEN` | `kubectl get secret -n hci-sim-dev` |
| P9 | 90 天前的 `Error` Pod/Job 残留 | 2026-08-06 的 PreSync hook 带 `argocd.argoproj.io/hook-finalizer`，operation 中断后清理未执行，TTL 控制器也无法删除 | Job annotations 与 `ownerReferences` |
| P10 | `argocd` 命名空间无 `hci.env.role` 标签 | 环境标识的数据来源缺失 | `kubectl get ns argocd -o json` |
| P11 | 无可用性门禁 | 只看 Deployment `1/1` 会漏掉 P4 这类「Ready 但不可达」 | 本次实测 |
| P12 | `node-exporter` DaemonSet 反复 `CreateContainerError` | main 的 #1494 新引入该 DaemonSet，模板把 `mountPropagation: HostToContainer` 硬编码在 `hostPath: /` 上；dev 单机 WSL 的 `/` 不是 shared/slave mount，containerd 拒绝生成容器 spec（实测重试 678 次）。staging 云端 `/` 是 shared mount 故无此问题，属于环境拓扑差异而非孤儿资源 | Pod waiting message、`kubectl describe`、新旧 main 的模板对比 |

---

## 3. 为什么 staging 没有这些问题

1. **staging 的 root app 有实际生效的 automated 策略**：`argocd-root-cloud.yaml` 由云端 bootstrap 正确 apply，子 Application 随 Git 增删；dev 的 root 是早期手工对象，从未带上 `syncPolicy`。
2. **staging 有完整的运维 provision**：`hci-dsh` 命名空间中的 LLM/插件 Secret、`hci-sim-runtime-credentials` 等由运维流程预置；dev 只声明了契约，没有执行 provision。
3. **staging 的宿主机存在 `/sf/log` 等目录**，`home.hostPath` 与 `caseService.dshSessions` 指向同一目录树；dev 的 WSL 单机没有这些路径。
4. **staging 走渐进式晋级**：hci-sim 固定 digest `3e3c246…`，dev 停留在 `0343b9b…`；这不是「dev 配置错误」，而是 CI 晋级链未在 dev 完成。
5. 结论：staging 与 dev 的差异是**控制面是否收敛 + 依赖是否 provision + 拓扑是否匹配**，不是某个 values 字段写对了。

---

## 4. 修复设计

| 修复 | 位置 | 作用 |
|------|------|------|
| R1 | `deploy/gitops/argo-apps/local/hci-sim-dev.yaml` 增加 `helm.releaseName: hci-sim` | 期望 selector 收敛到现存值，一次解决 P3/P4 |
| R2 | `deploy/gitops/argo-apps/local/hci-platform-dsh-dev.yaml` 增加顶层 inline `values` 关闭 `customerLogs`/`tickets` | 消除 P6；inline values 由 ArgoCD 追加到 `valueFiles` 之后，优先级最高，无需改环境仓 |
| R3 | 新增 `scripts/ops/bootstrap-dev-parity-credentials.sh` | 幂等补齐 P7/P8/P10 所需 Secret 与标签；不生成密钥、不覆盖存量键 |
| R4 | 新增 `scripts/ops/smoke-dev-services.sh` | 把 P11 变成 L1~L5 分层可证伪门禁 |
| R5 | 集群侧重新 apply `deploy/gitops/bootstrap/argocd-root.yaml` | 让 GitOps 收回控制权，自动创建 P2 缺失的 DSH Application |
| R6 | 集群侧清理 90 天前的失败 hook Job | 先摘除 finalizer 再删除，消除 P9 |
| R7 | `deploy/helm/hci-platform-obs` 把 `nodeExporter.mountPropagation` 参数化，`hci-platform-obs-dev` 用 inline values 置空 | 默认保留 staging/prod 行为；dev 关闭传播后仍保留 `/proc`、`/sys`、`/` 的静态只读视图，消除 P12 且不丢采集能力 |

---

## 5. 对抗性审查：方案会在什么条件下失败

| 攻击面 | 失败模式 | 加固 |
|--------|----------|------|
| 直接给 root 开 selfHeal | 把集群子 Application 全量拉回 Git，hci-sim digest 从 `9307b59…` 变为 `0343b9b…` | 先验证两个 digest 在 ghcr 均返回 200；接受「Git 是唯一事实源」，不手工回填无来源 digest，避免破坏 CI 晋级链 |
| 只补 DSH Application | 部署后 `Pending` / `CreateContainerConfigError` | 用 `helm template` 预渲染定位 P6/P7，先修配置再纳管 |
| 修好 selector 就宣布成功 | sync 会把镜像从 `dev-local` 切到 ghcr，并暴露 P8 的 Secret 缺口 | 纳管前先跑 R3 provision，并验证 `HCI_SIM_DATABASE_URL` 的 scheme 与库名（见 `hci_sim/internal/database/config.go`） |
| 直接 `kubectl delete job` | 被 `hook-finalizer` 卡在 Terminating | 先 merge patch 清空 finalizers |
| 宣称「所有功能等价」 | dev 的 DSH baseURL 是内部 oneapi，与通用 `llm.baseUrl` 不同源，无专用 key 时分析调用会失败 | 脚本支持 `--dsh-api-key-file`；文档显式区分「进程/UI 可用」与「分析调用可用」 |
| 只看 `1/1` | 漏掉 P4 | 冒烟门禁强制经 ClusterIP 访问并检查 EndpointSlice |
| 依赖 selfHeal 自动修好 node-exporter | DaemonSet 采用 `updateStrategy: OnDelete`，模板更新不会触发 Pod 重建，失败 Pod 会一直留着 | 部署后显式删除旧 Pod，并用冒烟门禁的异常 Pod 判据兜底 |
| 基于过期 main 基线做定性 | 排查期间 main 合入 #1494 新增 node-exporter 模板，一度把新回归误判为「Git 中已无声明的孤儿资源」；合并 main 后结论被推翻 | 合并最新 main 后再定稿，并把该次推翻记入本文 |

---

## 6. 操作手册（顺序不可交换）

```bash
# 1. 合入本修复后，先补齐 dev 缺失的 provision
bash scripts/ops/bootstrap-dev-parity-credentials.sh

# 2. 恢复 Root Application 的 automated 策略（bootstrap 对象不被自己管理）
kubectl apply -f deploy/gitops/bootstrap/argocd-root.yaml

# 3. 等待子 Application 自愈：应出现 hci-platform-dsh-dev，hci-sim-dev 转 Synced/Healthy
kubectl get applications -n argocd -w

# 4. 清理 90 天前的失败 hook（先摘 finalizer）
kubectl -n hci-dev patch job hci-sim-db-migrate-20260806-1304-6 \
  --type merge -p '{"metadata":{"finalizers":[]}}'
kubectl -n hci-dev delete job hci-sim-db-migrate-20260806-1304-6

# 5. node-exporter 使用 updateStrategy: OnDelete，模板更新不会触发 Pod 重建；
#    必须显式删除旧 Pod，让 DaemonSet 按关闭传播后的新模板重建
kubectl -n hci-observability delete pod -l app.kubernetes.io/name=node-exporter --ignore-not-found

# 6. 执行完整验收
bash scripts/ops/smoke-dev-services.sh
```

---

## 7. 验收判据

- 全部 Application `Synced / Healthy`，且 `hci-platform-dsh-dev` 存在。
- 业务命名空间全部 Deployment 副本 Ready，无 `Error` / `Pending` Pod。
- `hci-sim` 与 `dsh` 的 EndpointSlice 有地址；`hci-sim` 经 ClusterIP `/healthz`、`/readyz` 返回 200。
- `admin-ui /` 返回 200，`/dsh/` 不再返回 502/503。
- 13 个业务服务、观测组件、DSH、hci-sim 的健康端点逐项通过。
- `scripts/ops/smoke-dev-services.sh` 退出码为 0，失败项计数为 0。

---

## 8. 已知边界

- staging 集群无可用 kubeconfig，本次未对 staging 做运行时实测；对齐基准是仓库中 staging 的声明式能力面。
- dev 与 staging 的 hci-sim digest 不同，属于 CI 晋级链的设计差异，不手工拉齐；后续由 promotion 流程推进。
- dev 单机拓扑下 `/sf/log`、域名/TLS、副本数等差异刻意保留，不追求 values 字节一致。
- dev 未配置 DSH 专用 LLM 凭证时，`scripts/ops/bootstrap-dev-parity-credentials.sh` 使用平台通用 key 作为启动占位；DSH 进程与 UI 可用，真实诊断分析需 `--dsh-api-key-file` 注入专用凭证。

---

## 变更历史

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2026-11-13 | v1 | 初版：记录 dev 与 staging 能力面差异、GitOps 漂移根因、修复设计与可证伪验收判据 |
