# kb-service Catalog 基线持久化与版本漂移修复（2026-08-13）

## 1. 背景

两类故障，根因不同但都落在 kb-service：

### 1.1 Catalog 基线被 pod 重建回滚

手动在 Admin 页面新增/修改的 Catalog 基线（`acli_command_catalog.json` /
`resolution_catalog.json`），在 kb-service Pod 滚动更新或节点重启后丢失，回滚到镜像内原始内容。

**第一性原理根因**：基线是 JSON 数据文件，打包在镜像层
`/app/shared/resolution/catalogs/`。页面修改经 `PUT /api/kb/catalogs/{filename}` 调用
`path.write_text()`，只写入容器可写层（内存/临时磁盘）。Pod 重建以镜像层重新挂载，
可写层丢弃 → 修改丢失。这是"有状态数据写在无状态容器文件系统"的反模式。

### 1.2 KBD 23821 诊断报 tool_contract_revision stale

参考案例 23821 在仿真测试中持续报 `expert publish tool contract revision is stale`，
根因为 agent-service 与 kb-service 的 shared 依赖（signals schema，PR #745）指纹分叉：
agent-service 已升级到含 #745 的镜像，而 kb-service 镜像停留在 #739（`4bfc2c5`），
两者 `tool_contract_revision` 不一致。

## 2. 约束与对抗性审查

- **运行时代码完整性防护（D-015/D-020）**：集群 `ValidatingAdmissionPolicy` 禁止任何
  `volumeMount` 覆盖 `/app/app/`、`/app/shared/` 或 `*.py`，写入前以 `Fail + Deny` 拒绝。
  → 不能把 PVC 直接挂载到 `/app/shared/resolution/catalogs/`，否则 Pod 创建被拒。
- **emptyDir 否决**：pod 重建即丢，必须用 PVC。
- **initContainer 覆盖用户修改否决**：必须"仅当目标文件不存在才拷贝"。

## 3. 修复方案

### 3.1 Catalog 持久化（数据/代码分离）

- 新增 `kb-service/pvc.yaml`：`kb-service-catalog-data`（RWO，默认 1Gi，`storageClass` 可配）。
- 新增 initContainer `init-catalog-baseline`：从镜像 `/app/shared/resolution/catalogs`
  拷贝基线到 PVC 挂载点 `/data/catalogs`，仅缺失时拷贝，保护已有页面修改。
- 主容器 `volumeMounts` 将 PVC 挂到**非受保护路径** `/data/catalogs`。
- `backend/shared/resolution/catalog.py` 支持环境变量覆盖路径：
  `ACLI_CATALOG_PATH` / `RESOLUTION_CATALOG_PATH` 设置时作为权威路径，否则回退镜像内置目录。
- kb-service deployment 在 `persistence.enabled` 时注入上述两个环境变量指向 `/data/catalogs`。
- agent-service 是 Shared Resolution Runtime 的实际执行者，必须以只读方式挂载同一 PVC，并注入相同的两个环境变量；否则页面接口与 QFK Runtime 会读取不同的 Catalog 副本，页面保存无法热生效到诊断执行。
- Catalog 保存使用同目录临时文件加原子替换，确保共享挂载上的读者只能看到旧完整 JSON 或新完整 JSON，不能因 mtime 热加载读到半截文件。
- `acli system qmpcmd` 已同步回 Git 内置 Catalog，保证 PVC 首次初始化、新环境部署和当前页面热修使用相同的命令基线。
- `values.yaml` 新增 `kbService.persistence`（默认 `enabled: true`）。

### 3.2 版本漂移修复（触发 kb-service 重建）

- `backend/kb-service/app/routes/resolution_catalogs.py` 写路径增加调用链 `trace_id`、
  Prometheus 指标（`hci_catalog_save_total` / `hci_catalog_save_errors_total`）与结构化日志。
  该改动触达 `backend/kb-service/`，CI 将重建并推进 kb-service 镜像到含 #745 的版本，
  消除与 agent-service 的 signals 指纹分叉。
- `backend/shared/observability/metrics.py` 新增 Catalog 写路径业务指标。
- `scripts/ci/resolve_image_build_plan.py` 新增回归测试
  `test_resolve_image_build_plan.py`，断言 `backend/shared/` 变更必选全部后端服务（含 kb-service），
  防止 shared 依赖漂移类故障复发。

## 4. 验证

- `helm template` 渲染确认 PVC、initContainer、主容器挂载与 env 正确，且挂载点不在 `/app/shared/`。
- Helm 单测覆盖 agent-service 在启用持久化时使用只读共享 PVC、关闭持久化时不引用该 PVC；kb-service 路由单测覆盖原子替换后无临时文件残留。
- `pytest scripts/ci/test_resolve_image_build_plan.py` 通过（含 shared→全部后端服务的断言）。

## 5. 已知权衡

存量 Pod 内已有的页面修改（从未持久化）在本 PR 部署后会被重置为当前镜像基线，
属一次性迁移；此后所有新增/修改均持久化到 PVC。

## 6. 架构决策记录（ADR）：Catalog 存储载体选型——文件 vs 数据库

**决策**：在 kb-service 当前架构（单/少副本、低频写、高频读、依赖 mtime 热加载）下，
Catalog 基线以 **Git 为唯一真相源的文件（JSON）** 作为权威存储，运行态经 **PVC 持久化**承载；
**不引入数据库**作为真相源或运行态。数据库仅在"需要中心化、跨实例强一致实时分发"时作为 Git 的派生缓存。

### 6.1 第一性原理推导

Catalog 基线的本质属性：
1. 结构化、有 schema、需被代码/规则校验（非任意二进制）。
2. 读写模式：低频写、高频读、读路径要求低延迟（进程热加载 + 内存缓存）。
3. 强一致性：安全白名单必须"看到即生效"，不允许半截读。
4. 可审计、可回滚、可 Git 追溯（属于"配置"而非"业务交易数据"）。

由此推导两种载体的物理/逻辑根基：
- **文件**：POSIX 原语，进程 `open/read/write` 直访；mtime 天然作为变更信号驱动热加载；
  单文件 `write_text` 原子替换 inode，读方不会半截读；文件可进 Git，PR/ArgoCD 全链路可见。
- **数据库**：表行存储，需经 SQL/连接池/事务；失去 mtime 这类零成本变更信号，须额外
  `updated_at` + 轮询/LISTEN-NOTIFY 替代；内容不在 Git，需额外"配置即代码"导出才可达审计。

### 6.2 对抗性审查（红队找茬）

**攻击文件方案（已通过修复对冲）：**
- 无状态 FS 下文件写随 pod 重建丢失 → 用 PVC 规避；RWO + 单副本避免多副本争用。
- 多环境一致性：dev/staging/prod 各一份 PVC，页面改 dev 不自动同步 staging——文件方案固有"每环境一份"特性。
- **协作分叉**：多人改同一 JSON 时 PVC 内容与 Git 分叉 → 须约定"Git 是单一真相源，PVC 仅是运行时覆盖层"。
- **GitOps 盲区**：PVC 内运行时修改 ArgoCD 不可见，无法声明式回滚 → 须定期将 PVC 热修回流 Git。

**攻击数据库方案（结构性劣势）：**
- 配置漂移不可见：DB 改白名单，Git/ArgoCD 无感，出事无法 diff 历史——运维级隐患。
- 启动依赖与故障域扩大：kb-service 启动须连 DB 才能加载 Catalog，DB 抖动 → 白名单加载失败 → 服务降级；文件方案无此依赖。
- 热加载机制需重写：mtime 热加载改轮询 `updated_at` 或通知，引入复杂度与延迟。
- 安全白名单原子性：多行更新中途崩溃可能读到"部分生效"白名单，需用事务或单 JSON 列规避。

### 6.3 结论与铁律

1. **真相源永远在 Git**（文件/JSON），保证可审计、可回滚、可评审、可被 ArgoCD 声明式管理——数据库天然做不到。
2. **运行态载体**：当前架构下文件 + PVC 持久化是最优解（零依赖、mtime 热加载、原子写一致、与 `_HotCatalog` 零改造），数据库属过度工程。
3. **数据库适用边界**：仅当多副本/多环境需中心化强一致实时分发、或多服务共享且要求跨实例秒级一致、或体量超单文件可管理时才引入，且 DB 只应作为 Git 的派生缓存而非真相源。
4. **铁律（封堵对抗性漏洞）**：Git 是 Catalog 唯一真相源；PVC 仅为运行时覆盖层；页面热修须定期回流 Git，否则 ArgoCD 无法声明式管理。
