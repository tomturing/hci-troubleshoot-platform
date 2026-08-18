---
status: accepted
category: solution
audience: product, architect, developer, qa, sre
last_updated: 2026-08-17
owner: team
---

# hci-sim 环节细化、规模化构造与双模式 Bundle 工厂分析

## 2026-08-18 修复：Synthetic Draft 场景变量门禁

Bundle 工厂的 `positive-minimal` 编译路径现在会读取 C1 Resolver 返回的
`required_variables`，为受控的 Synthetic 变量提供稳定值，并把这些值写入 manifest
的 `variables` 后参与 Bundle digest 计算。`HOST` 使用虚拟节点，`END` 使用固定时间点，
`VM`、`REQUEST_ID` 等使用 support_id 派生值；它们只表示仿真输入，不是客户环境事实。

变量名没有内置提供器且没有由场景画像显式提供时，编译仍返回
`capability_gap`，禁止猜测。这修复了 KBD `40061` 的 `{{END}}` 在 C1 已合法解析、
但 Bundle 工厂因未注入场景变量而返回 409 的问题。专家仍可在 Draft 创建后修订
manifest；场景画像路径继续支持显式覆盖这些默认值。

> **编者按（2026-08-17）**
> - 本文档归档自 2026-08-17 的三轮深化分析：①环节 1（C1 Capability 解析）与环节 2（Fixture Bundle 生产）的细化说明；②"1000 个已发布 KBD 如何快速构造仿真环境"与"测试不通过/专家审核编辑走哪个环节"；③realistic Bundle 工厂设计及其与 synthetic 批量编译的关系判定。
> - **基线更新（重要）**：PR #779（2026-08-17 09:13 合并，配套设计 [`docs/solution/events/2026-08-16-hci-sim多bundle发布闭环修复.md`](../../events/2026-08-16-hci-sim多bundle发布闭环修复.md)）引入 `hci_sim/internal/fixture/pool.go` BundlePool，**Runtime 已支持一份部署加载多个 KBD 的 Bundle**（当前 `values.yaml` 装载 27123 + 23821）。此前评审中"单 manifest 单 KBD 世界"的表述自本合并起部分失效；本文所有分析以 #779 之后代码为准。
> - 姊妹篇：[`../hci-sim能力缺口对抗性审查与仿真环境构造流程.md`](../hci-sim能力缺口对抗性审查与仿真环境构造流程.md)（2026-08-16 全景基线，其"单 manifest"结论由本文编者按与本篇第 3 节修正）。

---

## 1. 环节 1 · C1 Capability 解析细化

### 1.1 设计：它解决什么问题

第一性问题：**编译 Bundle 之前，必须先回答"这条 KBD 的哪些不可变事实，可以安全地变成仿真路由？"** C1 把这个问题建模为一个纯函数：

```text
输入：KBD entry + dynamic_resource_active 不可变快照 + Tool Registry active 快照
输出：ready_for_artifact_binding + ResolvedKbdInput（或结构化 capability_gap 列表）
```

核心设计不变量（均可对应到代码，`backend/kb-service/app/services/hci_sim_resolver.py`）：

| # | 不变量 | 代码证据 |
|---|---|---|
| 1 | **无副作用**：绝不调用 `ensure_published`，批量验证不可能把编辑态 KBD 意外推进到 Agent active | 模块 docstring :1-6；`resolve_entry` 全程只读 |
| 2 | **fail-closed 不补猜**：缺 revision/Tool Contract/Artifact 事实时返回 gap，而不是造一个伪值 | :5；`_digest` 注释"拒绝空值而不是制造伪 checksum"（:36-41） |
| 3 | **三重一致性校验**：`entry.status=published` ∧ `snapshot.status=published` ∧ `active.checksum == snapshot.checksum` ∧ 快照身份属于该 support_id | `resolve_entry:233-249`，gap 码 `KBD_NOT_PUBLISHED`/`KBD_ACTIVE_SNAPSHOT_NOT_PUBLISHED`/`KBD_ACTIVE_CHECKSUM_MISMATCH`/`KBD_SNAPSHOT_IDENTITY_MISMATCH` |
| 4 | **Signal 文档强校验**：必须是 v2 文档、`publish_validation.status=passed`、带 `tool_contract_revision` | :250-265 |
| 5 | **Tool Registry 逐 Signal 校验**：`acquire.tool` 必须有 published + `is_active` + checksum 一致的 active 修订 | `_resolve_synthetic_routes:328-341` |
| 6 | **命令与 Agent 同源**：路由 argv 不取自 Signal 原文，而取自 Shared Resolution Runtime 的裁决（`review_signal_document(feature=PUBLISH)`）；BLOCKED 或无 command 的信号直接排除 | :315, 343-353；`backend/shared/resolution/review.py:348` |
| 7 | **staleness 观测不阻断**：tool_contract_revision 字节哈希只做变化探测（#755 对齐） | :266-269 注释 |
| 8 | **policy_revision 是内容指纹**：对安全边界契约 JSON 做 sha256，不依赖部署时 active 指针 | `backend/shared/schemas/hci_sim_policy.py:42-46` |

不变量 6 最关键：**仿真路由的 argv 与 Agent 真实执行时解析出的命令来自同一个裁决器**，从机制上防止"仿真能跑通、真实环境跑不通"的脱节。

### 1.2 代码实现情况

- **实现**：`hci_sim_resolver.py`（387 行）。主入口 `resolve_entry`（:218-307）做事实校验，`_resolve_synthetic_routes`（:308-387）生成 `SyntheticRouteInput` 元组（signal_id/tool/argv/tool_revision/tool_checksum/required_variables/role/matcher/produces）。
- **API 面**：`backend/kb-service/app/routes/hci_sim.py`--`GET /api/kb/hci-sim/capabilities`（批量 `resolve_all`，支持 `sample_suite` 过滤）与 `/capabilities/{support_id}`（单条），均要求 `INTERNAL_API_TOKEN` 内部身份（:24-30），**没有任何写路径**。
- **状态**：`implemented`（C1 任务完成项全勾），有 `test_hci_sim_resolver.py` 覆盖。
- **消费方**：`bootstrap --kbd-id` 的 `fetchCapability`、`capability-matrix.sh` CI 门禁、`diagnosis-lab.py check`。
- **易混淆点（对抗性提示）**：Runtime 自己的 `GET /v1/simulations/capabilities/{kbd_id}`（`main.go`）与 kb-service 的 C1 是**两套实现**。Runtime 版只对已加载 Bundle 池做 gap 判定（kbd_not_loaded/revision_mismatch/digest_missing/synthetic），不查数据库；权威解析在 kb-service。Admin UI 构建前看到的 gap 来自 Runtime 简化版，与 C1 全量报告语义不同。

### 1.3 作用

1. **KBD 世界与仿真世界的唯一翻译器**：把发布态 KBD 冻结为 `ResolvedKbdInput`--环节 2 唯一合法的编译输入格式。
2. **把"能不能仿真"变成可审计数据**：gap code 结构化输出，落 `scenario.capability_gap` jsonb、进 CI 门禁、进 Admin UI 展示；"为什么不能"永远有答案。
3. **编译确定性的起点**：`signals_digest = sha256(signals_document)` 派生出 Bundle 的 `input_fingerprint`，保证"同一 KBD + 同一 Signal 集合 ⇒ 必然编译出相同 Bundle"。

### 1.4 关键障碍

1. **上游漏斗空转**：解析器只能解析 published KBD，dev 现状 126 条中 120 条未发布、6 published、2 ready（C1 验证报告 :22-32）。瓶颈不在 C1，在人工审核发布。
2. **TOOL_CONTRACT_STALE 的一致性风险**：4 条 published KBD 因工具契约过期不 ready；"仅观测不阻断"意味着 Signal 引用的工具修订已变时仿真可能仍按旧 argv 跑--被显式接受的风险，等 E 阶段差分证据量化。
3. **裁决覆盖度**：所有信号都解析不出命令时返回 `SYNTHETIC_ROUTE_UNRESOLVED`（:379-386）--resolver/catalog 覆盖不足会锁死整条 KBD。
4. **双实现漂移**：Runtime capabilities 端点与 kb-service C1 并存，gap 语义不一致，长期有误导风险。

---

## 2. 环节 2 · Fixture Bundle 生产细化

### 2.1 设计：为什么必须分叉

第一性问题：**"命令进来回什么"的数据从哪来？** 两条互斥口径，验证目标不同：

| | 路径 A · synthetic | 路径 B · positive-realistic |
|---|---|---|
| 输出来源 | C1 synthetic_routes 模板生成 | 真实客户环境脱敏 Artifact |
| 验证目标 | **契约与链路**（命令能跑通、exit_code 对） | **数据口径**（输出长得像真的） |
| 标记 | `SYNTHETIC=true` + facts_boundary 声明 | 必须绑定获批 Artifact provenance |
| 地位 | 已实现已接线 | 设计完整、实现是参考内核、未接线 |

分叉的强制点在编译器（`hci_sim/internal/controlplane/controlplane.go:204-206`）：`hasRealisticRoute(manifest) && len(input.Artifacts)==0 -> 拒绝`。设计逻辑：**"真实口径"一旦能被模板伪造，E 阶段 real/sim 差分就失去意义**。

两条状态机（均含防伪造设计）：

```text
Bundle：draft ──Validate(仅 compiler；mutation/secret/独立证据三前置全真)──► validated
            ──Approve(Expert 与 Security 双角色；compiler 不得自审；同人不得双角色)──► approved
            ──Publish(仅 publisher；对象存储 commit)──► published ──MarkStale(依赖变化)──► stale

Artifact：staged ──RecordScan(仅 security；secret/PII/license/schema 四重全过；scanner revision 入审计)──► scanned
            ──Approve(Expert+Security 双人；登记者不得自审)──► approved；任意态 ──Revoke(仅 security)──► revoked
```

配套防伪造细节：
- **默认拒绝的 Gate**：`NewMemoryRegistry` 默认挂"拒绝一切 Artifact"的 Gate（:174-176）；可伪造的 `Artifact.Approved` 字段已移除。
- **双重摘要不可混用**：manifest 的 `bundle.digest` 是排除自引用字段的语义指纹；对象存储校验的是实际字节的 payload digest（:227-228 注释 + `GetPublished:350-364` 双重完整性复核）。
- **编译确定性**：`CompileInput` 冻结后 canonical JSON 指纹；同指纹重复编译输出不同 ⇒ `compiler_nondeterministic_output`（:243）。

### 2.2 代码实现情况（三条实现线）

1. **synthetic 编译器（已实现、已接线）**--`bootstrap.go`：`fetchCapability`（调 C1，未 ready 即拒）-> `buildScenarioManifest`（:266-365）七层校验（路由事实完整、argv 变量渲染不留 `{{}}`、`NormalizeArgv`、**argv 必须以 `acli` 开头**、重复 RouteKey 拒绝、七变体 fault 映射、`ComputeBundleDigest`+`fixture.Parse` 自校验）；场景画像与 KBD 信号集**双向零漂移校验**（`loadScenarioProfile:224-264`）。
2. **控制面参考内核（已实现、未接线）**--`controlplane.go`（837 行）+ `artifact.go`（330 行）：状态机、双角色审批、对象存储 prepare/verify/commit/abort、stale 扩散、`ResolvePublished` 歧义拒绝全部实现并有单测。**但 `main.go` 的 import 不含 `internal/controlplane`，全仓无非测试引用**。内置 `scan()` 只匹配三个字符串（:454-463），注释自认"生产扫描器须扩展为 DLP/许可证策略"。
3. **现实替代（手工路径，事实上在用）**：27123/23821 的 manifest 手写 JSON 入 `deploy/helm/hci-sim/files/` + `values.yaml` + Argo digest（#779 后经 `fixture.manifestFiles` 列表 + `HCI_SIM_REQUIRED_BUNDLES` 声明式校验 + ConfigMap 挂载）。Runtime 对 synthetic 的防线是 build 闸门二（`authority_scope != runtime_fixture` 且非 synthetic），Helm 默认值恰恰是 `runtime_fixture`，dev 靠手工覆盖 `dev_golden` 放行。

辅助线 `offline-manifest`（`offline_manifest.go`）：把客户 Verification Bundle ZIP 的 execution_items 映射为离线 aCLI manifest，歧义即拒。

### 2.3 作用

1. **Bundle 是仿真环境的"物理定律表"**：RouteKey 精确匹配、六种 fault、输出限额以它为唯一事实源；digest 贯穿 lease claims、`/status`、run 持久化。
2. **分叉决定"验证了什么"**：synthetic 通过 ≠ 数据真实；只有 realistic Bundle 存在，E 阶段差分才有比较对象。
3. **状态机把数据准入变成多人治理**：compiler/expert/security/publisher 四角色分离 + 不可自审。

### 2.4 关键障碍

C2 任务"后续阻断项"五项全未勾（`docs/task/hci-sim/events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry任务.md:21-31`）：

1. **治理先于工程**：数据政策（采集范围、保留期、脱敏规则、许可证规则、真实审批身份源）未定--四重扫描的"通过标准"无从定义。
2. **基础设施未部署**：受控 OCI/S3/WORM、KMS、生产级 DLP 扫描器；Memory 实现被显式禁止替代。
3. **持久化 Repository 缺位**：PostgreSQL CAS/version、审计、outbox worker、超时恢复未实现；`fixture` schema 5 张表已建好但无生产写入方。
4. **历史债**：Atlas migration 目录重复版本问题，versioned migration hash/lint 工作流暂停。
5. **冷启动悖论**：首条 realistic Bundle 必须为 ready KBD 完整走一遍"绑定获批 Artifact -> 编译 draft -> 不得直接发布"的人工流程。
6. **对抗性补充**：手工路径惯性（27123/23821 模式可用，工厂化动力取决于新 KBD 接入频率）；未接线内核的能力幻觉（维护成本照付、生产链路未用）。

---

## 3. 规模化专题：1000 个已发布 KBD 如何快速构造仿真环境

### 3.1 前提更新：#779 多 Bundle 池

#779 落地内容（`git show 311f2af7`）：
- `internal/fixture/pool.go`（153 行）：`BundlePool` 从 `HCI_SIM_FIXTURE_DIR` 目录或单 manifest 兼容入口加载；**同一 support_id 不允许重复 digest**；`ValidateRequired` 要求部署声明与实际加载集合完全一致（缺失/夹带/digest 漂移即启动失败）。
- `main.go`：`loadRuntimeBundlePool()`；启动时 `SyncPublishedBundles` 把池内全部 Bundle 幂等 upsert 进 `fixture.bundle`/`scenario`（actor=`gitops-runtime`，带 trace_id）；capabilities/build 按 kbd_id 从池中取 Router；`/status` 报告 `bundle_count`/`loaded_support_ids`。
- `server.go`：SSH exec 按 lease claims 的 `SupportID` 取 Router，`routerMatchesClaims` 对 bundle digest + tool/policy revision 做**常时比较**，不匹配即 `lease_bundle_contract_mismatch`。
- `values.yaml`：`fixture.manifestFiles` 列表（27123+23821）+ `defaultSupportID`；注释明言"每次集合或内容变化都会改变 Pod checksum 并触发失败关闭的滚动更新"。
- 配套文档：`docs/solution/events/2026-08-16-hci-sim多bundle发布闭环修复.md`。

### 3.2 构造流水线与各环节控制点（1000 规模）

```text
C1 批量解析 ──► 逐 KBD 编译 Bundle ──► 集中打包进 Helm ──► GitOps 发布 ──► 逐 KBD build ──► 逐 KBD TestRun
(环节1,自动)    (环节2,分叉)          (环节3,人工PR)      (环节3,Argo)     (环节4,自动)      (环节5,半人工)
```

| 环节 | 1000 规模下的机制 | 自动化程度 | 瓶颈性质 |
|---|---|---|---|
| 1 · C1 解析 | `resolve_all` 一次批量查询全部 1000 条 ready/gap | 全自动，已实现 | 非瓶颈（秒级） |
| 2 · Bundle 生产 | synthetic：`bootstrap` 可脚本化并行；realistic：C2 管线 5 项全未落地 | synthetic 自动 / realistic 0% | **第一瓶颈** |
| 3 · Runtime 加载 | 多 Bundle 池；manifest 经 ConfigMap 挂载；`HCI_SIM_REQUIRED_BUNDLES` 声明式校验 | 半自动（GitOps PR 人工合并） | **第二瓶颈** |
| 4 · build | 按 kbd_id 从池中取 Router；lease 绑定该 KBD 的 bundle_digest | 全自动，已实现 | 仅剩 authority_scope 全局闸门 |
| 5 · TestRun | title/description 必填人工字段、逐 KBD 建 Case/Conversation | 半人工，无批量 | **第三瓶颈**（无 Headless Runner） |
| 6-7 · 执行/证据 | 单租约单命令确定性执行、result CAS 落库 | 全自动 | 非构造瓶颈 |

**环节 2 细化**：synthetic 口径今天可批量（1000 次 `bootstrap` 并行，秒级/条），但 synthetic Bundle 过不了 build 闸门二，**只能用于 C3 本地验收，不能成为正式仿真环境**；realistic 口径每条需 Artifact 采集->脱敏->四重扫描->双人审批->编译，生产管线 0%，纯人工下限 = 1000 × 2 次审批 = 2000 次人工判断（无法用工程优化消除的串行资源）。

**环节 3 的新约束**：
- **ConfigMap 1MiB 硬上限**：manifest 全部经 `hci-sim-fixture` ConfigMap 挂载（`templates/configmap.yaml` 内联 `manifestFiles`）。synthetic manifest 每份几 KB，1000 份约几 MB，**必然超过 K8s ConfigMap 1MiB 限制**--需拆分多部署或改对象存储挂载（未实现）。
- **全量滚动重启**：任何 Bundle 集合/内容变化 -> Pod checksum 变化 -> 全部环境一起滚动重启，进行中的 15 分钟租约会话全部中断。
- **单副本**：`replicaCount=1`（内存配额 Tracker 单副本）；1000 并发环境的容量/SLO 未验证（阶段 E 全 pending）。
- 脚枪：`HCI_SIM_ACTIVE_REVISION` 是全局 env，若设置会作用于所有 KBD（默认不设则按各 Router revision，正确）。
- 好消息：`HCI_SIM_REQUIRED_BUNDLES` 由 Helm 从 `manifestFiles` 自动生成；`SyncPublishedBundles` 幂等同步，DB 侧天然支持 1000。

**环节 5**：每个 TestRun 的 title/description 是必填人工字段（api-gateway 强校验），阶段 D 的持久 Scheduler 与 Headless Runner 未接入--构造出 1000 个环境后逐个跑验收仍是 1000 次 UI 操作。

### 3.3 进度结论

| 目标能力 | 进度 |
|---|---|
| 批量判定 1000 条 KBD 可测性（环节 1） | ✅ 完成（`resolve_all` + capability-matrix.sh 只读矩阵） |
| 单部署多 KBD（环节 3/4 架构前提） | ✅ 2026-08-17 完成（#779） |
| synthetic 批量编译 | ✅ 可脚本化，但产不出正式环境 |
| realistic Bundle 工厂 | ❌ 0%（C2 五项阻断） |
| 1000 份 manifest 的分发载体 | ❌ ConfigMap 1MiB 撞墙，无分片/对象存储方案 |
| 批量测试（Headless/Scheduler） | ❌ 未接入（阶段 D 尾款） |

**控制环节排序**：环节 2（人工审批 ×1000 + 管线缺失）≫ 环节 3（ConfigMap 上限 + 全量重启 + 单副本）＞ 环节 5（无批量验收）＞ 环节 4（authority_scope 全局闸门无按-KBD 粒度）。环节 1 已不控制。

---

## 4. 测试不通过与专家审核编辑的控制环节

### 4.1 检测与根因分流

检测面在环节 6/7：命令卡片逐条 passed/failed/blocked（`exit_code≠0 -> failed`）-> 会话级 `agentOutcome` -> result API（api-gateway 校验 `report_summary` 后计算 digest 转发）-> `run_result.outcome` CAS 落库，全程 `run_event` 带 trace_id。证据查询：`control_plane.run/run_event/run_result` + bridge 日志 + `capability-matrix.sh` 行。

| 失败根因 | 责任环节 | 修复动作 |
|---|---|---|
| `fixture_not_found`（Agent 执行的命令不在 Bundle 里） | 环节 2 | Bundle 路由覆盖缺口 -> 编译新 Bundle 版本 |
| 命令跑通但结论错误 | 环节 0（KBD 内容错）或环节 2（仿真输出错） | 先判定哪边错：KBD 错 -> 改 KBD；输出错 -> 改 Bundle |
| `inconclusive`（证据不足） | 环节 2 | 丰富 Route 输出信息量 |
| lease/context/协议类失败 | 环节 3/4 | 部署与配置问题，不走内容审核 |

### 4.2 设计控制点 vs 现实操作

**设计语义**（controlplane 参考内核）：Bundle 不可变，"编辑"永远是产生新修订：新冻结输入 -> Compile(draft) -> Validate(mutation/secret/独立证据) -> Approve(双角色、不可自审) -> Publish(对象存储 commit) -> 旧 Bundle MarkStale -> stale_outbox 扩散。角色模型：`compiler/expert/security/publisher/runtime`（`controlplane.go:35-43`）。

**现实操作**（今天实际生效的控制面是 git PR + Argo）：
1. **改 KBD 内容**：kb-service 管理端 draft -> 人工审核 -> 发布 -> 新 revision -> C1 重新解析 -> 重编译 Bundle。
2. **改仿真数据**：synthetic 族编辑场景画像 JSON -> `diagnosis-lab.py up` 重编译（双向零漂移校验兜底）；realistic/Helm 族编辑 `deploy/helm/hci-sim/files/kbd-*.json` -> PR（**PR 评审是事实上的"专家审批"**，CI 门禁机械把关：digest 一致性、Helm/testdata 字节一致、退役 marker 拒绝、helm lint）。
3. **发布生效**：合并 -> Argo sync -> ConfigMap 变更 -> Pod checksum -> fail-closed 滚动重启 -> `ValidateRequired` 校验 -> `SyncPublishedBundles` upsert。旧租约因 digest 常时比较自动失效--**不可变性在 Runtime 侧被强制执行**。
4. **回滚/退役**：revert PR 或从 `manifestFiles` 移除。

### 4.3 专家闭环缺口

1. **无审批 UI**：Bundle 审核编辑接口是 git PR；1000 规模下"专家审批 = 谁合并了 PR"，双角色/不可自审全靠自觉。
2. **stale 不扩散**：KBD 升 revision 后旧 Bundle 在接线路径不会被标 stale（`SyncPublishedBundles` 只 upsert published；`MarkStale`/`stale_outbox` 无驱动方）。
3. **authority 无按-KBD 粒度**：`authority_scope` 是全局 env，无法表达"这个 KBD 的 Bundle 已过审、那个没有"。
4. **专家角色操作性缺位**：真实审批身份源本身就是 C2 第一阻断项。

---

## 5. realistic Bundle 工厂设计与 synthetic 的关系

### 5.1 判定

**设计上：一个工厂、两条原料线、同一张交付契约--相辅相成且深度共生，不是两个独立系统。** synthetic 与 realistic 的全部差异收敛在一个点：**Route 输出（stdout/stderr/exit_code）的数据来源**。路由几何、编译输入契约、产物格式、digest 语义、Runtime、DB schema、lease 绑定完全共享。**实现上今天确实像两个系统**（两个入口、两条路径、变体命名漂移）--工程进度假象，见 5.5。

### 5.2 工厂五段设计

**（1）原料准入线**：授权只读采集 -> 受控对象存储（原始字节永不进 DB/Bundle）-> metadata 登记（provenance 只存 `source_ref_digest`+`redaction_digest`，对象地址服务端生成）-> 四重扫描（scanner revision 入审计，仅 security 可记录）-> 双人批准 -> revoked 永久不可绑定。

**（2）编译线（核心）**：Observation 提取与参数化固定六步（C 方案 :94-105）：①验证 Artifact 与 KBD/Tool/节点来源一致；②显式规则定位 observation；③客户名/真实 IP/主机名/路径映射为类型化变量；④**保留 output shape**（换行、列宽、编码、stdout/stderr 划分、exit code）；⑤参数化前后 Matcher dry-run + 结构 diff；⑥记录 provenance，不在 Bundle 内保存客户身份。配套变量图（:81-92）：复用生产 Agent 的 Tool/Command Compiler（"不维护第二套命令规则"），拒绝循环依赖/未定义变量/类型不一致/跨节点越权。OCR 只能辅助人工草稿。

**（3）反自洽门禁（灵魂）**（:107-127, 189-192）：**禁止 Matcher 反向生成字符串充当 positive-realistic**（同源循环论证、自证通过）；独立性证据三选一且真实 Artifact 优先；mutation 门禁（关键字删除/替换、阈值边界、stdout/stderr 互换、节点/路径改变、producer 错误、参数变化、顺序变化），mutation 后错误实现仍得相同结论 ⇒ `insufficient_oracle`，不得 validated。

**（4）发布与存储**：双重 digest（`bundle.digest` 语义指纹 vs `object_digest` payload 字节，合并会自引用）；两阶段 prepare/verify/commit；只追加 `bundles/{object_digest}`；DB digest 唯一键 + version CAS。

**（5）Stale 引擎**：Artifact 撤销/KBD revision 变化 -> stale_outbox -> `FOR UPDATE SKIP LOCKED` 消费 -> version CAS -> 审计；崩溃由 pending/processing 超时 reconciliation 恢复。

**治理前提**（C 方案 :193-196）："机器自动化负责规模和一致性，专家审批负责语义、安全与业务风险；未经审批不得发布。"

### 5.3 synthetic 在设计里的位置

synthetic 是**同一个 CompileInput 契约的"Artifacts 为空"模式**（`controlplane.go:204-206` 是唯一分岔判定）：无 positive-realistic route + Artifacts 为空 -> 不触发 Gate，编译零人工。现实实现者是 `bootstrap.go`（C1 synthetic_routes 骨架 + 场景画像 + 七变体），输出标记 `SYNTHETIC=true` + facts_boundary 声明。

### 5.4 关系论证

**五层共享**：①同一冻结输入（synthetic_routes 骨架对两族完全相同）；②同一编译契约与状态机（synthetic 走同一条 Registry 状态机，只是无 Artifact）；③同一 Manifest v2 与 digest 语义；④同一运行时（BundlePool/Router/lease 无差别）；⑤同一变体方法论（C 方案 :107-121 的 variant 表中 `positive-minimal` 与 `positive-realistic` 并列）。

**四层互补**：①验证目标正交（契约/链路 vs 数据口径，facts_boundary 是书面边界）；②时序递进（C3 先用 synthetic 走通链路，"Fixture 可信性必须先独立闭环"再谈 Scheduler）；③证据互补（E 阶段 `CompareObservations(real, sim, allow)` 差分要求 realistic 存在才成立；mutation/near-miss 防 synthetic 自证通过）；④规模漏斗互补（synthetic 全自动铺广度，realistic 人工双审批建 Golden 深度；C 方案 :229"synthetic 23821 仅用于开发契约"）。

**一道强制边界**：build 闸门二对 `IsSynthetic()` 一律 409--synthetic 永远不能成为正式仿真环境，Runtime 强制保证"正式环境 = realistic"。

### 5.5 对抗性审查：实现造成的"两个系统"假象

1. **两个入口、两条路径**：synthetic 走 `bootstrap.go` + 本地 Docker / 手写 git manifest；realistic 工厂（controlplane）未接线。设计里是同一条 Compiler Pipeline 的两个执行模式。
2. **变体命名漂移**：bootstrap 实现集（positive-minimal/positive/negative/missing-evidence/command-failed/version-incompatible/timeout）与 C 方案目标集（positive-minimal/positive-realistic/negative/near-miss/timeout/permission/unknown）命名与覆盖不一致，未统一到一张注册表。
3. **synthetic 线缺失反自洽门禁**：bootstrap 无 near-miss、无 mutation 门禁、无 Matcher dry-run/结构 diff；当前人工画像事实上扮演"专家手工证据"这一独立性来源，但该等价关系未机制化--若未来用程序从 Matcher 反向生成 synthetic 输出，循环论证风险在 synthetic 线上无门禁。

**架构债结论**：不是"要不要合并两个系统"，而是**把 bootstrap 的 synthetic 编译并回统一 Compiler Pipeline（补齐变体注册表与 mutation 公共段），再让 controlplane 从参考内核变成生产 Registry**。

---

## 附录 · 证据索引

| 主题 | 证据位置 |
|---|---|
| C1 解析器 | `backend/kb-service/app/services/hci_sim_resolver.py:218-387`；`backend/kb-service/app/routes/hci_sim.py:33-74` |
| Signal 命令裁决同源 | `backend/shared/resolution/review.py:280-360`（`command=acquisition.command`） |
| 编译器分岔判定 | `hci_sim/internal/controlplane/controlplane.go:204-206` |
| Bundle/Artifact 状态机 | `controlplane.go:193-396`；`artifact.go:105-218` |
| 双重 digest 语义 | C2 方案 :25-33；`controlplane.go:227-228` |
| Compiler Pipeline / 参数化六步 / 变体表 / 反自洽 | C 方案（2026-08-05 阶段 C）:61-127, 181-200 |
| 多 Bundle 池（#779） | `hci_sim/internal/fixture/pool.go`；`git show 311f2af7`；`docs/solution/events/2026-08-16-hci-sim多bundle发布闭环修复.md` |
| ConfigMap 挂载 | `deploy/helm/hci-sim/templates/configmap.yaml`；`values.yaml` fixture 段 |
| Registry 启动同步 | `hci_sim/internal/database/run_repository.go:186+`（SyncPublishedBundles） |
| lease/Bundle 匹配强制 | `hci_sim/internal/server/server.go`（routerForSupport/routerMatchesClaims，#779 引入） |
| C2 阻断项 | `docs/task/hci-sim/events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry任务.md:21-31` |
| KBD 漏斗基线 | `docs/verify/hci-sim/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证报告.md:22-32` |
