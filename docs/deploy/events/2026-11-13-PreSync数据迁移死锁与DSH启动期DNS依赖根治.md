---
status: active
category: deploy
audience: developer, operator, architect
last_updated: 2026-11-13
owner: team
---

# dev PreSync 数据迁移死锁与 DSH 启动期 DNS 依赖根治

> 一句话：环境「`Synced / Healthy`」曾经靠两个手工资源撑着——一个不存在的上游占位 Service，
> 和一个被人工改过的数据库。集群一旦重建，两者同时消失，管理台起不来、PreSync 停不下来。
> 本文把它们拆回物理事实，给出不依赖任何手工资源的仓库侧修复与可证伪验收。

---

## 1. 硬约束与彻底化判据

按第一性原理，先把「解决」定义成不可再分的事实，而不是「当前页面能打开」：

| 判据 | 含义 |
|---|---|
| C1 无手工资源 | 删除 `hci-dsh/dsh` 占位 Service 后，管理台仍能启动并通过探针 |
| C2 重建可恢复 | 集群重建后，仅凭两个 Git 仓库 + ArgoCD 即可自行收敛，无需 `kubectl create` 或改库 |
| C3 下次 sync 不复发 | 下一次 PreSync `db-migrate` Job 在存量库上 `exit 0`，不依赖此前的人工清洗 |
| C4 全程可用 | 修复合入与镜像滚动期间，旧 Pod 保持 `1/1 Ready`，不出现全站不可用 |
| C5 可证伪 | CI 有一条能复现存量库路径的断言，而不是只看空库全绿 |

对抗性审查的攻击面是「集群重建」「下次 sync」「新环境」三个极限场景；凡在这三个场景下需要人再敲一次命令的方案，一律判为临时处理。

---

## 2. 根因

### 2.1 管理台 CrashLoopBackOff：启动期硬解析

`frontend/admin/nginx.conf` 的 `/api/`、`/api/direct/` 已统一使用动态解析：

```nginx
resolver ${DNS_RESOLVER} valid=30s;
set $upstream ${API_GATEWAY_UPSTREAM};
proxy_pass $upstream;
```

唯独 `/dsh/` 写成字面量 `proxy_pass ${DSH_UPSTREAM};`。字面量上游会在 **Nginx 配置加载期**做一次 libc 域名解析，`dsh.hci-dsh.svc.cluster.local` 解析失败即 `[emerg] host not found in upstream`，进程直接退出。DSH 由独立 Chart `hci-platform-dsh` 部署在 `hci-dsh` 命名空间，dev 从未部署它，因此 dev 上这张 DNS 记录**物理不存在**。

当时用 `kubectl create namespace hci-dsh` + 一个无 selector 的 ClusterIP Service 造出 A 记录让 Nginx 启动。这不是修复：它伪造了一个永远没有 endpoint 的上游，且 DSH 真上线时会与独立 Chart 的同名 Service 冲突。Headless Service 首次尝试还因无 Endpoints 不产生 A 记录而返回 NXDOMAIN，进一步说明它是靠 DNS 实现细节凑出来的。

**与 `/terminal-bridge` 的区别必须说清**：后者也用字面量 `proxy_pass`，但 `TERMINAL_BRIDGE_UPSTREAM` 由同一 Helm release 注入，指向本 release 自己渲染的 Service（`enabled=false` 时退化成 `127.0.0.1:9999`），DNS 记录或 IP 必然存在；`TICKET_SYNC_UPSTREAM` 是节点 IP 字面量，根本不走 DNS。只有 `dshUpstream` 指向**另一个可选 release 的 Service**，这才是独有的启动期硬依赖。

### 2.2 PreSync 死锁：数据迁移依赖「未来才创建」的表

`scripts/db-migrate.sh` 对存量库的执行序：

```
Step 0  CORE_SCHEMA_READY=true → 跳过建表
Step 1  migration-runner（全部 data-migrations）
Step 2  desired_extras.sql
Step 3  atlas schema apply（建 log_collect_* 新表 + 加 kb_category_code_format CHECK）
Step 4  desired_extras.sql
Step 5  seeds
```

`056/057/058/061/062/064` 直接引用 `log_collect_task` / `log_collect_custom_scenario`，这些表只有 Step 3 才创建。于是：

- Step 1 报 `relation "log_collect_*" does not exist`，`migration-runner` 汇总 `Failed: 6` 并 `exit 1`；
- 入口脚本 `set -e` 立即终止，Step 3 永不执行；
- 缺表导致迁移失败，迁移失败又阻止建表——**闭环死锁**，`backoffLimit: 0` + `hook-delete-policy: HookFailed` 还让失败 Job 在几十秒内被删掉，日志几乎抓不到。

`db-migrate.sh` 的注释里其实已经记过「2026-10-29 staging 事故：056 报 relation does not exist」，但当时只把**种子**挪到 Step 5，没有处理数据迁移本身的依赖，注释反而把错误顺序固化成了「data-migrations 在存量库跑在 Atlas 之前，不能碰本次新建的表」。

### 2.3 为什么 CI 一直全绿

`.github/workflows/db-migration-test.yml` 的 Step 2 在**空库**上的顺序是：

```
函数 → Atlas（建全表）→ 触发器 → data-migrations → seeds
```

而生产存量库是 `data-migrations → 函数 → Atlas → 触发器 → seeds`。CI 验证的从来不是生产走的那条路径，所以「依赖未来表」这类迁移可以长期全绿合入，直到某次存量库升级才爆。这是**检测能力缺口**，不是 SQL 写错。

### 2.4 041 的硬编码映射

`041_category_baseline_leaf_governance.sql` 用一张硬编码 `_code_remap` 覆盖 staging dry-run 见过的脏 `kb_category.code`，末尾 `RAISE EXCEPTION` 保证清洗不彻底就回滚。dev 有 4 个手工建的分组（`平台-L2-主机删除/主机安装/主机扩容/系统盘或主机替换`）不在映射表也不在 DELETE 列表，导致 041 永久失败。上一轮为解锁环境把这 4 个 code 手工改成 `平台-091`~`平台-094`——数据在 Git 里没有对应事实，重建即复现。

---

## 3. 修复

### 3.1 `/dsh/` 统一为动态解析

```nginx
location /dsh/ {
    resolver ${DNS_RESOLVER} valid=30s;
    set $dsh_upstream ${DSH_UPSTREAM};
    proxy_pass $dsh_upstream;
    ...
}
```

变量式 `proxy_pass` 把解析从配置加载期推迟到请求期：DSH 缺失仅让 `/dsh/` 返回 502，探针与其余路由不受影响；DSH 上线后 `resolver valid=30s` 内自动生效，无需重建镜像或改 values。变量值不含 URI 部分，转发仍使用原始请求 URI，与字面量写法等价。改动落在 `frontend/admin/nginx.conf`，由 `frontend/admin/Dockerfile:55` 打进 admin-ui 镜像，CI `resolve_image_build_plan.py` 会把 `frontend/admin/**` 映射为 admin-ui 构建。

### 3.2 两趟数据迁移夹一次 Atlas：解开依赖闭环

不能简单把 Step 3 提到 Step 1 之前：041 是 Atlas 施加 `kb_category_code_format` CHECK 的**数据前置**，脏 code 未清洗时 Atlas 会失败。两类依赖方向相反，必须拆开：

```
Step 0   空库引导（不变）
Step 1   预迁移：runner 第一趟  ← 完成 041 等「约束前置」清洗；依赖新表的失败被容忍
Step 2   函数
Step 3   atlas schema apply     ← 脏数据已清洗 → CHECK 可加；新表已创建
Step 3.5 最终迁移：runner 第二趟 ← 056~064 重试成功；set -e 生效，失败即 Job 失败
Step 4   触发器
Step 5   种子
```

- `migration-runner.sh` 按 `version` 判重：成功的不重跑；失败的**不写** `migration_history`，第二趟自然重试；
- 第一趟局部 `set +e` 只表示「前置条件尚未满足」，是否真有缺陷由第二趟判定，**不会静默吞错**；
- 所有依赖新表的迁移（含未来新增的）都自动落在第二趟，属于结构性根治，不是给 056~064 逐个打补丁；
- `set -e` 之外引入 `HCI_MIGRATION_TRACE_ID` 唯一调用链，串联「扩展 → 预迁移 → 函数 → Atlas → 最终迁移 → 触发器 → 种子」全部日志；`migration-runner.sh` 复用同一 `trace_id`；
- `SCHEMA_FILE / EXTRAS_FILE / MIGRATION_RUNNER / MIGRATIONS_DIR / SEEDS_DIR` 参数化，缺省仍是镜像内绝对路径，CI 可覆盖以复现真实执行序；调用改成 `sh "$MIGRATION_RUNNER"`，不再隐含依赖 git 中的可执行位（两脚本实际是 `100644`）。

### 3.3 041 通用兜底

在 4)/5)（扁平化与垃圾节点删除）之后、6)（重命名）之前插入确定性兜底：把 `_code_remap` 未覆盖的非合规 code，按「域内共享序号空间」从该域现存最大序号 +1 开始分配 `{域}-{3 位序号}`，写入同一张映射表，并再 remap 一次 4 张下游引用表；`RAISE NOTICE` 输出重编号数量作为审计日志（唯一调用链 `category_leaf_governance_v1`）。

位置是关键：放在删除之前会把本应删掉的 `E2E-*` 垃圾节点误重编号；放在重命名之后引用会悬空。空库下 `pending` 为空、分配 0 行，CI 全量重放不受影响；已执行过 041 的存量库按 `version` 跳过，不改动现有数据。

### 3.4 CI 存量库路径门禁

`db-migration-test.yml` 新增一步：新建 `legacy_test` / `atlas_dev_legacy` 两个库 → 先按完整 `desired_schema.sql` 建全表 → **DROP 掉 `log_collect_task` / `log_collect_custom_scenario` / `log_collect_default_scenario`**，精确复现「核心 Schema 已在、新表缺失」的现场 → 直接执行真实 `scripts/db-migrate.sh` → 断言 `migration_history` 登记数等于 `data-migrations/*.sql` 文件数，且两张依赖表已重建。这一步会在**第一趟故意制造 6 个失败**，靠两趟收敛通过，因此能证伪「又退回单趟」或「CI 只看空库」。

### 3.5 签名密钥：声明式契约 + 幂等引导

`environments/dev/values.yaml` 已声明 `diagnosisService.existingCredentialsSecret: diagnosis-service-signing`，chart `secret.yaml` 在 `existingCredentialsSecret` 非空时**不渲染私钥**——GitOps 契约本身就是完整的，缺的是集群重建后可复现的注入流程。`scripts/ops/bootstrap-diagnosis-signing-secret.sh` 提供幂等引导：Secret 已存在且 4 个键齐全时原样不动；缺失时只从受控密钥源读取并创建，**拒绝自动生成随机密钥**。

---

## 4. 刻意否决的方案

| 方案 | 否决理由 |
|---|---|
| 把占位 Service 写进 Helm chart 变成「声明式」 | 仍是一个永远无 endpoint 的假上游；DSH 独立 Chart 真上线时同名冲突；prune 还会与另一个 release 抢资源所有权 |
| 把 `dshUpstream` 默认值改成 `127.0.0.1:9999` 回环 | 绕过症状而非消除启动期 DNS 依赖；DSH 上线时还得改 values，属于把维护成本推迟 |
| Helm hook Job 自动生成签名密钥 | 每次重建集群生成新密钥，历史采集签名无法验证、已加密诊断数据永久不可解；密钥必须**稳定可恢复**，不是「可自动生成」 |
| 把私钥写进 values 以求全自动 | 私钥入仓，直接违反安全基线；且 env 仓库的 GitOps 分支可读范围更广 |
| 数据迁移与 Atlas 整体对调顺序 | 041 未清洗时 Atlas 加 CHECK 失败，把死锁换成另一种失败 |
| 给 056~064 逐个加 `CREATE TABLE IF NOT EXISTS` | schema 事实源分裂成 Atlas 与迁移脚本两处；未来新迁移仍会复发 |
| 只调大 `backoffLimit` / 保留失败 Job 日志 | 只改善可观测性，不改依赖闭环 |

---

## 5. 验收判据（可证伪）

| 判据 | 命令 / 观察点 | 通过条件 |
|---|---|---|
| C1 | 删除 `hci-dsh/dsh` 后重启 admin-ui Pod | 新 Pod `1/1 Running`，日志无 `host not found in upstream` |
| C2 | 全新命名空间按 GitOps 重建 | 无需任何 `kubectl create secret` / 手工改库即可 `Healthy` |
| C3 | 触发一次 ArgoCD sync，观察 PreSync `db-migrate` | `exit 0`；日志出现 `Step 1` 与 `Step 3.5`，`Step 3.5` 后 `Failed: 0` |
| C4 | 滚动期间 `kubectl get deploy -n hci-dev` | 全部 `1/1`，旧 Pod 不提前终止 |
| C5 | `DB Schema 声明式验证` workflow | 「存量库升级路径验证」step 通过，`EXPECTED == ACTUAL` |
| 负向 | 临时把 `db-migrate.sh` 改回单趟 | C5 必须**失败**，否则门禁是假门禁 |

---

## 6. 已知边界

- `/dsh/` 在 dev 未部署 DSH 时返回 502，这是**业务能力缺失**而非故障；管理台其余功能不受影响。
- `hci-dsh/dsh` 占位 Service 属于手工资源，新镜像生效并验证 C1 后应删除；它不在任何 Git 声明中，不会被 ArgoCD prune 接管。
- `db-migrate.sh` 第一趟容忍失败会在日志里留下 `Failed: N`；这是**预期的两趟语义**，判定是否成功只看第二趟与 Job 退出码。
- 041 的通用兜底只对**尚未执行 041** 的库生效；已执行库按 `version` 判重跳过，dev 现存 `平台-091`~`平台-094` 不会被再次改写。
- CI 的空库路径（Step 2）与存量库路径（Step 6）是两条互补验证，不是互为替代：前者保证 SQL 本身正确，后者保证生产执行序可收敛。

---

## 相关

- [集群 DNS 上游治理](../集群DNS上游治理.md)（另一类 DNS 问题：解析成功但地址不可路由）
- [部署管理规范](../部署管理规范.md)
- [ArgoCD 应用架构说明](../ArgoCD应用架构说明.md)
