# case-service 发布空窗归零：从「先杀后起」到「先起后停」，以及必须先修好的临时文件互踩

- 日期：2026-10-30
- 涉及：`backend/case-service/app/services/log_collect_storage.py`、`deploy/helm/hci-platform/templates/case-service/deployment.yaml`、`deploy/helm/hci-platform/values.yaml`
- 关联：`2026-10-28-日志收集会话状态自愈与中继重试方案.md`（P0 代传租约）、`2026-10-30-观测命名空间白名单与发布可用性度量.md`（本轮的验收手段）

## 一、现场事实

| 事实 | 证据 |
| --- | --- |
| case-service 用 `Recreate` 发布，每次合并后有一段时间 endpoints 为空 | `kubectl -n hci-staging get endpoints case-service` 在窗口内无地址 |
| 2026-10-30 这次发布实测不可用 ≈21s | 新 Pod start 09:13:22 → Ready 09:13:43；旧 Pod 在此之前已被删除 |
| 集群是单节点（`aihci`，v1.34.5+k3s1），存储类只有 `local-path` / `local-path-retain`，二者都是 RWO | `kubectl get sc`、`kubectl get node` |
| RWO 的约束是「节点级」而非「Pod 级」：同节点两个 Pod 可以同时挂载同一 local-path 卷 | Kubernetes 语义（RWO 限制的是挂载节点，不是 Pod 个数）；本轮发布时实测确认，见第五节 |
| 分片、会话元数据、合并成品的临时文件**只由目标名推导** | `3.part.tmp`、`meta.json.tmp`、`<task>.zip.tmp` |

## 二、根因（第一性原理）

发布空窗不是「Recreate 的必然代价」，而是**两个独立约束被混为一谈**的产物：

- 真实约束：RWO 卷上，**同一个数据目录是否允许多个进程同时写**。
- 被误用的推论：RWO ⇒ 不允许两个 Pod ⇒ 只能 Recreate。

把这两件事拆开看，判据是「应用怎么用这份目录」：

| 应用形态 | 多进程共享同一目录 | 结论 |
| --- | --- | --- |
| 数据库 / 带 WAL / 持有排他锁（langfuse-clickhouse） | 进程会争用锁与日志文件，必崩（PR #567 的现场就是 exit code 76） | 必须 `Recreate` |
| 对象存储语义（按 key 分路径写、原子 rename 落盘） | 只要**临时文件名不共享**，多写者互不影响 | 可以 `RollingUpdate` |

case-service 属于后者，但它的三点写入（分片 / 元数据 / 合并）都用固定临时文件名，所以**当前确实不能滚动**：两个 Pod 会写同一个 inode，互相截断；更糟的是一个进程 `os.replace` 把对方正在写的临时文件搬走，对方再 rename 时直接 `FileNotFoundError`（见第四节的实测）。

因此顺序不能反：**先让临时文件名带上写者身份，再改发布策略**。

## 三、修复设计

1. **临时文件名唯一化**：新增 `_temp_for(target)`，文件名 = `{目标名}.{pid}-{uuid4[:12]}.tmp`，与目标同目录（保证 `os.replace` 不跨卷）。
   - pid 区分进程（滚动发布时新旧 Pod 并存）；
   - 随机串区分同进程内的并发请求（同一分片重传、重试与首次写重叠）。
2. **发布策略改 `RollingUpdate(maxSurge=1, maxUnavailable=0)`**：新 Pod Ready 之后才停旧 Pod，窗口内始终至少有一个 Ready 副本。
   - `maxUnavailable` 不开放配置（恒为 0）：放开就是回到「先杀后起」；
   - 保留 `rollout.strategy=Recreate` 作为退路：换到拒绝多挂载的 CSI 时改回来即可。
3. **优雅期 300s**（`terminationGracePeriodSeconds`）：覆盖「在途合并」这一最长请求。uvicorn 收到 SIGTERM 先停接新连接、再等在途请求结束，所以 300s 只是上限，不会拖慢正常发布。
4. **`preStop` 等待 10s**（`caseService.preStopSleepSeconds`）：见第六节——这是二次验收时才发现必须补的一块。
   - 渲染期硬约束：`preStopSleepSeconds >= terminationGracePeriodSeconds` 直接 `fail`，否则 preStop 吃掉优雅期后是 SIGKILL；
   - 置 0 即不渲染 `lifecycle`，留给不关心该问题的服务。
5. **测试锁**：
   - `backend/case-service/tests/unit/test_log_collect_storage_temp.py`：临时路径唯一（机制）、并发写同一分片只能得到完整副本（行为）、并发合并只能得到完整压缩包；
   - `deploy/helm/hci-platform/tests/case_service_rollout_test.yaml`：默认必须 RollingUpdate、maxUnavailable=0、优雅期 300、必须有 preStop 且等于 `sleep 10`、preStop 不小于优雅期时必须渲染失败、置 0 时不写 `lifecycle`、可显式退回 Recreate。

## 四、对抗性审查：什么条件下会失效

| 失效条件 / 攻击 | 结果 | 兜底 |
| --- | --- | --- |
| 测试写了但永远绿 | 回归锁形同虚设 | 已做反向验证：把 `_temp_for` 临时改回固定名，3 个测试全部失败（并发用例正是 `FileNotFoundError`） |
| 存储换成拒绝多挂载的 CSI | 新 Pod 卡在 `ContainerCreating`，发布停住 | `maxUnavailable=0` 下旧 Pod **继续服务**，用户无感；把 `rollout.strategy` 改回 `Recreate` |
| 进程被 SIGKILL 留下孤儿 `.tmp` | 占磁盘 | 分片目录由 `delete_multipart` 整体清理；对象目录残留与发布次数同阶，人工兜底；成品只认 `os.replace` 后的文件，孤儿不影响正确性 |
| 分片 1 落到旧 Pod、分片 2 落到新 Pod | 会话状态不一致 | 会话状态全在 `meta.json`（共享卷）+ DB，无进程内缓存（已 grep 核实，仅静态错误码表用了 `lru_cache`）；P0 的代传租约保证同一任务不会被两个 Pod 同时代传 |
| 300s 优雅期被误认为「发布会等 5 分钟」 | 误判成本 | uvicorn 停机时立即停止接收新连接；只有真的有在途合并才会用满 |
| 有人把 `terminationGracePeriodSeconds` 改回 30s | 大包合并被 SIGKILL，留下半成品 + 租约要再等 120s | helm-unittest 已锁 300 |
| 以为 `maxUnavailable=0` 就等于「用户零 5xx」 | 旧 Pod 停止监听与负载均衡视图更新之间仍有差集，发布瞬间出现 502（**实测已发生**，见第六节） | 补 `preStop` 等待，并用 2338 次施压复验 502 = 0 |

## 五、验收口径

发布一次后检查：

```promql
# 1. 窗口内是否还有用户侧 5xx（网关口径，见 2026-10-30 观测文档）
sum(rate(traefik_entrypoint_requests_total{code=~"5.."}[1m]))

# 2. case-service 是否出现「可采目标为 0」
count(up{job="hci-services", namespace="hci-staging", app="case-service"})
```

命令行侧：

```bash
# 发布期间持续采样：期望始终 >= 1 个 Ready 副本，且 endpoints 从不为空
kubectl -n hci-staging get endpoints case-service -w
kubectl -n hci-staging get pods -l app.kubernetes.io/name=case-service \
  -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready
```

验收判据：**发布窗口内 endpoints 地址数从 1 变 0 的次数应为 0**，且网关 5xx 为 0。

> ⚠️ 光看 `endpoints` 和「网关 5xx」两行是不够的：如果这段时间根本没人访问，5xx 必然为 0，这种「空证据」看起来和真的没问题一模一样。验收必须**自己制造流量**，见第六节。

## 六、二次验收：endpoints 空窗归零 ≠ 用户侧零 5xx

### 6.1 为什么第一轮验收不算数

第一轮只看两件事，结果都很漂亮：188 次采样 `ep=0` 出现 0 次；`sum(increase(traefik_service_requests_total{code=~"5.."}[30m])) = 0`。

但追问一句这个结论就站不住了：**那 30 分钟里 case-service 自己收到了多少请求？**

```text
hci-staging-admin-ui-80@kubernetes      -> 5.26 次/5m
hci-staging-api-gateway-8000@kubernetes -> 47.37 次/5m
hci-staging-customer-ui-80@kubernetes   -> 8.42 次/5m
hci-staging-case-service-8001@kubernetes -> 0
```

全站共 61 次/5m 的真实流量，一次也没落在 case-service 上。**没有请求就永远不会 5xx**，所以「5xx=0」在这里等价于「什么都没测」。

### 6.2 主动施压后暴露的真问题

构造一条能穿透 Traefik 直达 case-service 的路径（`GET /api/direct/log-collect-uploads`，预期 404 作为基线码），4 并发 × 100s 约 24 rps，共 2338 次请求；第 10 秒 `kubectl rollout restart deploy/case-service`：

| 指标 | 结果 |
| --- | --- |
| 总体请求数 | 2338（p50 12.0ms，p95 34.8ms） |
| 基线码 404 | 2310 |
| **502** | **28** |
| endpoints 为 0 的采样 | 0 次（107 次采样全为 1） |
| 502 时间窗 | `10:47:19` 起连续 3 个整秒（每秒 9/13/6 次） |
| 旧 Pod `Killing` 事件时间 | `2026-10-30T10:47:19Z` |

关键对比：**`endpoints` 从没空过，但用户吃到了 28 次 502**。502 耗时 6–30ms（连接被拒的典型值），不是后端报错。

### 6.3 根因（第一性原理）

请求会不会打到「正在死的 Pod」，取决于两个**互相独立**的时钟：

| 时钟 | 内容 | 量级 |
| --- | --- | --- |
| A | Pod 拿到 `deletionTimestamp` → 从 EndpointSlice 消失 → 经 Traefik / kube-proxy 传播到每条转发路径 | 秒级（本次实测 ≈2s） |
| B | 进程收到 `SIGTERM` → 停止 `accept` | 毫秒级（uvicorn 立即关监听） |

只有 `A < B` 时才没有 502。`maxUnavailable=0` 回答的是「还有没有别的 Pod 在服务」，回答不了「流量是不是正好打在已经闭眼的那个上」——两件事被当成一件了。

修复只需把 B 拉长：`preStop: sleep 10`。kubelet 先跑 `preStop` 再发 `SIGTERM`，这 10s 里旧 Pod 仍在正常服务，而 A 早已完成。

- 为何是 10s 而不是 3s：实测 A≈2s 是在**空闲**的单节点上，Traefik 重载/API 抖动只会把它拉长；5 倍余量，代价只是发布多花 10s（新 Pod 早已接管）；
- 为何不是「Traefik 加 retry 中间件」：那只能遮住 Traefik 入口这一条路径，集群内的 `api-gateway → case-service` 走 Service DNS + kube-proxy，同样会撞上，且重试会把延迟翻倍。

### 6.4 这不是 case-service 独有的

实测同 namespace 13 个 Deployment 的 `lifecycle.preStop` 全部为空。ad-hoc 修一个服务不改变系统性缺陷。本次只改 case-service（已有施压手段可复验）；其余 12 个服务建议单独排期，用同一参数推开，避免一次性触发 13 次同时滚动。

## 七、与历史决策的关系

- `2026-10-27-日志收集上传存储与错误可定因.md` 里「`strategy: Recreate`：RWO + 本地文件系统，禁止新旧 Pod 同时挂载」是**在临时文件名唯一化之前**的正确结论，本文取代其中的发布策略部分，**不取代**其对 PVC/权限/错误码的设计。
- PR #567 把 obs chart 的单副本 + RWO 工作负载统一改 `Recreate` 依然正确：那些是数据库/带锁的存储（clickhouse），判据见第二节的表格，不要把它们改回滚动。

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
| --- | --- | --- | --- |
| 2026-10-30 | v1.0 | 初版：临时文件按 pid+随机串唯一化、case-service 改 RollingUpdate(maxUnavailable=0)、优雅期 300s、单测与 helm 单测锁行为、验收口径 | 本文 |
| 2026-10-30 | v1.1 | 二次验收订正：主动施压 2338 次发现「endpoints 空窗归零但仍有 28 次 502」，根因是缺 `preStop`（旧 Pod 立即停监听 vs 负载均衡视图更新有差集）；新增 `caseService.preStopSleepSeconds: 10` 与渲染期硬约束、3 个 helm 单测用例、验收口径改为「必须主动施压」 | 本文 |