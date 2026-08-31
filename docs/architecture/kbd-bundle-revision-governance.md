# KBD 与 Bundle 版本管理重构方案

> 状态：重构提案（2026-08-31）
> 目的：回答版本管理为什么复杂、业界如何建模，以及本项目应如何重构。
> 方法：第一性原理拆解 + 对抗性审查。
> 说明：本文件保留问题盘点和中间推导；最终落地基线请以 [KBD 与仿真资产统一版本管理最终设计](kbd-simulation-unified-version-final-design.md) 为准。

## 1. 结论先行

当前系统不是“一套 rev”，而是至少五个互不等价的身份空间：

| 身份空间 | 当前事实源 | 当前字段 | 能回答的问题 | 不能回答的问题 |
| --- | --- | --- | --- | --- |
| 知识快照 | `kbd_revision` | `revision_no`、`id`、`checksum` | 专家/模型保存了哪一份 KBD 内容 | Agent 当前是否正在使用它 |
| 运行时快照 | `dynamic_resource_revision` + `dynamic_resource_active` | `revision`、`checksum`、`active_revision` | Agent 运行时加载了哪一份资源 | 它对应哪个 `kbd_revision.revision_no` |
| Bundle 内容身份 | Manifest / `fixture.bundle` | `digest`、`object_digest` | Bundle 内容和对象字节是否相同 | 它是第几次编辑 |
| Bundle 编辑轮次 | `fixture.bundle.compile_input` | `draft_revision`、`parent_bundle_digest` | 当前 Bundle 是从哪个 Bundle 编辑出来的 | KBD 内容版本是多少 |
| Factory 资产快照 | `fixture.asset_revision` | `(asset_key, revision)`、`content_digest` | 模板/实例使用哪一版 | 它是否等于 Bundle revision |

因此，问题不是再增加几个 `revision_xxx` 字段，而是缺少清晰的领域边界：知识编辑、运行时发布、Bundle 构建和 Bundle 激活被不同模块分别建模，却没有一个明确的发布聚合把它们串起来。重构目标应是“一个对象、一种身份、一条生命周期”，而不是继续修补同名字段。

## 2. 当前版本管理的根本问题

### 2.1 把四个不同问题压缩成了一个词：revision

版本管理实际要回答四类不同问题：

| 问题 | 应有的身份 | 当前实现 |
| --- | --- | --- |
| 内容改了几次 | 不可变快照身份 | `kbd_revision`、资产 revision |
| 哪个内容可供 Agent 使用 | 发布/晋级身份 | `dynamic_resource_active` 指针 |
| 基于哪个输入构建了制品 | 构建身份 | `input_fingerprint`、Bundle digest |
| 当前工作副本是哪一轮 | 编辑会话身份 | `draft_revision`、父 digest |

当前把这些问题都暴露成整数 `revision`，导致调用方必须“猜这个数字属于哪个序列”。一旦两个序列碰巧相等，错误就会被隐藏；不相等时才暴露为查询失败或错误快照。

### 2.2 缺少“发布聚合”，导致 KBD 与 Runtime 之间没有显式血缘

`kbd_revision` 是编辑事实，`dynamic_resource_revision` 是运行时事实，但两者之间只有内容 checksum 或隐含调用关系，没有一等的“这次发布把哪个知识快照晋级为 Runtime 版本”的实体。

因此系统无法用一个稳定的对象回答：

> Agent 当前运行的内容，来自哪个 Expert KBD 快照、经过哪次发布、依赖哪些契约？

前端把 Runtime revision 传给 KBD 查询，正是这个缺失的直接表现，而不是单纯的前端 Bug。

### 2.3 不可变内容与可变状态分散在多处，状态转换无法形成一个事务边界

KBD 主记录、KBD revision、Runtime active、Bundle 状态、Bundle activation 分属不同表和不同函数。结果是：

- “创建子 Draft + 淘汰父 Draft”不是一个原子操作；
- “对象存储已提交 + 数据库已发布”不是一个原子事实；
- `status`、`active`、`published` 分别在不同表表达，调用方容易把它们当成同义词。

这说明当前设计缺少 aggregate root（聚合根）和明确的状态机拥有者。

### 2.4 依赖关系是编译输入的一部分，却没有成为一等 lineage

Bundle 的真实身份由 KBD、Signal、Tool、Policy、Factory asset、Compiler 共同决定；目前这些依赖散落在 `compile_input`、`dependency`、`route_sources` 和 Manifest 中。系统能保存它们，却没有统一的“构建声明”模型来约束：

- 输入是否完整；
- 输入是否可复现；
- 依赖是否被批准；
- 同一输入是否只能产生一个结果。

所以 `input_fingerprint` 只能作为应用层幂等技巧，尚未成为构建系统的不可变身份。

### 2.5 兼容主表仍被当作事实源，形成双写和语义漂移

`KbdEntry` 既是 Admin 兼容记录，又参与发布、检索和编辑；`kbd_revision` 又保存不可变快照。两者之间存在“先改哪边、何时同步、失败如何回滚”的隐含协议。

根本原则应改为：不可变快照是事实源，主表只能是查询投影；所有编辑和发布都通过明确命令生成新快照，再异步或事务性更新投影。

## 3. 业界最佳实践：共同模式而不是具体产品

### 3.1 内容身份使用不可变摘要，顺序号只做展示

- Git 用 commit/tree 内容图作为不可变身份，branch/tag 只是可变指针。
- OCI 镜像用 manifest digest 作为部署身份，tag 只是人类友好的别名。
- 软件包仓库将版本号用于兼容性沟通，但发布物仍以坐标 + 内容校验和唯一确定。

共同点：任何自动化系统都不应该只凭一个裸整数定位内容。

### 3.2 “构建”和“发布”分离，发布是晋级指针

CI/CD、ML Model Registry 和制品仓库普遍采用：

```text
source snapshot -> reproducible build -> immutable artifact -> promotion pointer
```

构建失败不会改变已发布制品；发布失败不会修改制品内容；回滚只是把指针切回旧制品。

### 3.3 编辑态、审核态、发布态、运行态分离

成熟的内容系统会把工作区（workspace）、审核结论（approval）、发布版本（release）和线上指针（active）分开。一个状态字段不负责表达全部生命周期；状态转换由单一服务拥有，并使用 CAS/事务保证并发语义。

### 3.4 每个衍生物都保存完整 provenance

数据平台、模型注册和供应链安全的共同要求是：衍生物记录完整输入快照、依赖版本、构建器版本、操作者、时间和 trace。不能通过“当前 active”回推历史，因为 active 会继续变化。

### 3.5 幂等必须由数据库约束和内容确定性共同保证

应用层先查再插只能降低重复概率，不能提供唯一性。正确模式是：规范化输入 -> 内容指纹 -> 数据库唯一约束 -> 冲突时比较内容摘要；不同摘要必须报确定性错误。

## 4. 本项目的最优重构模型

重构不再围绕“统一 revision 字段”，而是引入四个稳定对象和两个指针。

### 4.1 四个稳定对象

#### A. `KnowledgeSnapshot`

对应当前 `kbd_revision`，保存 KBD 的不可变 Proposal/Expert 内容。保留局部 `revision_no` 仅用于 UI 展示，真正跨服务身份使用 `knowledge_revision_id + knowledge_checksum`。

#### B. `KnowledgeRelease`

新增一等发布聚合，表示“一次经过审核、将某个 KnowledgeSnapshot 晋级为 Runtime 的事实”。建议字段：

```text
release_id
kbd_entry_id
knowledge_revision_id
knowledge_checksum
runtime_revision
runtime_checksum
release_status: prepared | published | revoked
approval_ref
dependency_snapshot
trace_id / actor / timestamps
```

`dynamic_resource_revision` 继续作为 Runtime 存储实现，但必须反向记录 `release_id` 和 `knowledge_revision_id`。`dynamic_resource_active` 只指向 `release_id/runtime_revision`，不再让调用方自行猜映射。

#### C. `BundleBuild`

对应一次不可变 Bundle 构建结果，唯一身份是 `bundle_digest`。它必须直接引用：

```text
knowledge_release_id
compiler_revision
normalized_build_input_digest
dependency_snapshot
manifest_digest
object_digest
```

`fixture.bundle.revision` 删除其业务含义，迁移为只读兼容字段 `source_knowledge_revision_no`；Bundle 不再拥有一个裸的“自身 revision”。

#### D. `BundleWorkspace`

对应专家编辑工作区，可以产生多个候选 `BundleBuild`。`draft_no` 只是工作区内的递增序号；父子关系用 `parent_bundle_digest`。工作区状态由 Bundle Registry 单独拥有，子创建和父淘汰必须在同一事务中完成。

Factory 模板/实例继续作为 `AssetSnapshot(asset_key, asset_revision, content_digest)`，由 `BundleBuild.dependency_snapshot` 冻结引用。

### 4.2 两个指针

```text
KnowledgeActive(kbd_entry_id) -> KnowledgeRelease
BundleActive(support_id)       -> BundleBuild
```

前者回答“Agent 使用哪个 KBD 发布”；后者回答“仿真运行使用哪个 Bundle 构建”。两个指针都支持 generation/CAS 和审计，但互不复用序号。

### 4.3 目标主链路

```text
KbdEntry（查询投影）
  -> KnowledgeSnapshot
  -> KnowledgeRelease（审核/发布聚合）
  -> KnowledgeActive
  -> Resolver 读取 release 的冻结内容
  -> BundleBuild（不可变制品）
  -> BundleWorkspace（编辑/审核上下文）
  -> BundleActive
  -> Runtime
```

规则只有三条：

1. 任何下游只能引用上游的稳定 ID + checksum，不能传裸 `revision`。
2. 任何状态变更只改变指针或生命周期，不改变快照和制品内容。
3. 任何回滚只切换指针，不重新编译、不复制内容、不修改历史行。

## 5. 重构后的 API 语义

跨服务请求统一使用以下结构，旧字段仅保留兼容期：

```json
{
  "knowledge_release_id": "kr_...",
  "knowledge_revision_id": 12345,
  "knowledge_checksum": "sha256:...",
  "bundle_build_id": "bb_...",
  "bundle_digest": "sha256:...",
  "bundle_input_digest": "sha256:...",
  "bundle_draft_no": 2,
  "compiler_revision": "bundle-factory-v4-fixture-assets",
  "trace_id": "..."
}
```

解析接口不再接受含义不明的 `?revision=17`，改为：

```text
GET /capabilities/{support_id}?selector=active
GET /capabilities/{support_id}?selector=working&knowledge_revision_id=12345
GET /capabilities/{support_id}?selector=explicit&knowledge_revision_id=12345
```

Bundle 工厂只允许 `selector=active`；Signal dry-run 才允许 `working`。这样“允许编译哪个版本”成为协议而不是前端约定。

## 6. 从当前系统迁移到目标模型

### 阶段 0：冻结语义，停止新增歧义

- 禁止新代码读取或写入裸 `revision` 作为跨领域字段。
- 为现有响应增加 `knowledge_revision_id/no`、`runtime_revision`、`bundle_digest` 的显式别名。
- 在日志、指标、审计中同时记录这些身份和 `trace_id`。

### 阶段 1：建立 KnowledgeRelease

- 新增 `knowledge_release` 表，回填每个当前 active KBD 的 release。
- 发布流程先创建 release，再创建/激活 Runtime revision；两者同事务提交。
- `dynamic_resource_revision.contract_json` 写入 `release_id` 和 `knowledge_revision_id`。
- Resolver 只从 release 解析 KBD，不再通过 Runtime revision 反查 KBD revision。

### 阶段 2：重构 Bundle Registry

- 将 `fixture.bundle` 的业务模型重命名为 `BundleBuild`，增加 `knowledge_release_id`、`bundle_input_digest`、`workspace_id`。
- `input_fingerprint` 改为 `bundle_input_digest`，增加数据库唯一约束。
- `ReviseDraft` 的子构建、父状态 CAS、依赖快照和 outbox 写入收敛到一个事务。
- 对象存储采用 prepare/commit/outbox 补偿，数据库状态机不再依赖一次跨系统事务假设。

### 阶段 3：切换消费者与清理兼容层

- 前端、Gateway、KB Service、hci-sim、离线同步全部改用显式 ID/digest。
- 旧 `kbd_revision`、`runtime revision`、Bundle digest 的映射由 release/build 直接提供，不允许调用方拼接。
- 一个完整发布周期后停止写入 `fixture.bundle.revision`，再删除兼容列和旧查询参数。

## 7. 对抗性审查：重构方案可能失败在哪里

- **引入 `KnowledgeRelease` 但仍允许直接读 active Runtime**：会形成第六套隐式版本，必须让 Resolver 以 release 为唯一入口。
- **把 `draft_no` 当排序真相**：并发或分支编辑会使其失去全局意义，父子 digest 才是 lineage 真相。
- **只加唯一索引不清理历史重复数据**：迁移会失败，必须先审计重复指纹并决定保留规则。
- **把对象存储 outbox 当成最终一致性借口**：Runtime 必须拒绝未确认对象，补偿任务必须可观测、可重试、可告警。
- **只改 API 名称不改权限边界**：working KBD 仍可能被当成可发布版本，selector 必须同时绑定用途和状态门禁。
- **保留 KbdEntry 双写**：投影落后或写入失败会重新制造“页面显示一个版本、Runtime 使用另一个版本”，必须提供校验指标和重建投影命令。

## 8. 重构验收标准

1. 任意一次 Agent 运行，都能从 `trace_id -> BundleBuild -> KnowledgeRelease -> KnowledgeSnapshot` 完整反查，且不依赖当前 active。
2. KBD 序号、Runtime 序号、Bundle draft 序号即使数值相同，也不会被系统视为同一版本。
3. 同一规范化构建输入在并发下最多产生一个 BundleBuild。
4. 回滚只改变 active 指针，历史快照、Manifest、对象摘要和审批记录不变。
5. 任意状态转换失败都不会留下半发布状态；可通过 outbox 和指标发现、重试和人工介入。
6. 所有请求日志使用同一 `trace_id`，并包含领域身份；数据库 lineage 能用唯一调用链关联。

以下章节保留当前实现的详细盘点，作为迁移前的事实基线。

## 附录 A：第一性原理基线

一个版本值只有在同时满足以下四个条件时才有业务意义：

1. **对象明确**：它属于哪个 KBD、资源名、Bundle 或资产键。
2. **事实唯一**：存在唯一行或唯一内容摘要可以反查它。
3. **生命周期明确**：它是草稿、已审核、已发布、已激活还是已退役。
4. **可复现**：给定版本和依赖，能够重新得到相同的内容摘要。

由此推导出三条硬约束：

- 顺序号只表示同一对象内的局部顺序，不能跨表比较。
- `checksum/digest` 是内容身份，状态字段不是内容身份。
- “发布”是把一个不可变快照接到一个可变指针上，不应改写快照内容。

## 附录 B：当前真实链路

### 3.1 KBD 知识生产链

```text
KbdEntry（兼容主记录）
  ├─ latest_proposal_revision_id ─> kbd_revision(type=proposal)
  └─ working_revision_id          ─> kbd_revision(type=expert)

LLM/分类/识图/信号生成
  -> append-only Proposal
  -> 专家保存工作稿（可多次）
  -> 审核通过时冻结 Expert 快照
  -> apply 到 KbdEntry
  -> DynamicResourcePublisher.ensure_published()
  -> dynamic_resource_revision
  -> dynamic_resource_active
  -> Agent Runtime
```

`kbd_revision.revision_no` 在同一 `kbd_entry_id` 内递增，`revision_type` 只有 `proposal` 和 `expert`。Expert 必须通过 `baseline_proposal_revision_id` 绑定其审核基线；不能用历史数组顺序或“最新 Proposal”猜测。

已发布 KBD 的维护路径是独立工作稿：工作稿保存只新增 `kbd_revision`，不覆盖 `KbdEntry` 的生效内容；维护发布才在同一数据库事务内更新主记录、冻结 Expert、创建运行时快照并切换 active 指针。

### 3.2 Bundle 编译与编辑链

```text
support_id
  -> KB Service Resolver
  -> 冻结 KBD/Signal/Tool/Policy 输入（CompileInput）
  -> hci-sim Compiler 生成 Manifest
  -> bundle.digest（Manifest 语义身份）
  -> object_digest（对象字节传输身份）
  -> input_fingerprint（编译输入幂等身份）
  -> fixture.bundle(status=draft)
  -> validate -> approve -> publish
  -> fixture.bundle_activation(desired/active generation)
```

Bundle 编辑不是覆盖原对象，而是：

```text
parent Bundle
  -> CompileInput.parent_bundle_digest = parent.digest
  -> CompileInput.draft_revision += 1
  -> 新 digest 的 Bundle draft
  -> 父 draft 变为 stale
```

`fixture.bundle.revision` 目前写入的是 `CompileInput.KBDRevision`。它不是 Bundle 自身的编辑轮次，名称具有误导性；Bundle 自身的编辑轮次在 `compile_input.draft_revision`。

### 3.3 Factory 模板/实例链

`fixture.asset_revision` 是独立的不可变资产表：

```text
(asset_key, revision)
  -> content_digest
  -> instance.template_asset_key + template_revision
  -> Bundle compile_input.dependencies / route_sources
```

资产发布、退役不应改写已有 Bundle，因为 Bundle 已将命中的资产修订和摘要冻结到 `compile_input` 中。

## 附录 C：现状字段对照

| 当前字段 | 正确含义 | 使用边界 |
| --- | --- | --- |
| `kbd_revision.revision_no` | 同一 KBD 的知识快照序号 | 只用于查 `kbd_revision` |
| `dynamic_resource_revision.revision` | 同一运行时资源的发布序号 | 只用于 Runtime 加载/审计 |
| `fixture.bundle.revision` | 历史兼容字段，实际存 KBD 序号 | 禁止新增调用方依赖；后续改名 |
| `compile_input.draft_revision` | Bundle 编辑轮次 | 只在 Bundle lineage 内递增 |
| `fixture.bundle.digest` | Manifest 语义内容身份 | 对外引用、父子关系、激活指针 |
| `fixture.bundle.object_digest` | 存储对象字节摘要 | 对象完整性校验，不替代 `digest` |
| `input_fingerprint` | 完整编译输入的幂等键 | 必须由数据库唯一约束兜底 |
| `asset_revision.revision` | 同一 Factory 资产键的修订序号 | 只用于资产表及依赖冻结 |
| `lock_version` | KBD 主记录乐观锁 | 只防编辑并发，不是业务版本 |
| `bundle.version` | Bundle 状态行乐观锁 | 只防状态迁移并发 |
| `bundle_activation.generation` | Runtime 激活指针代数 | 只用于激活确认/回滚 |

## 附录 D：已确认的缺口与风险

### P0：运行时 revision 被当成知识 revision

`KbdReviewView.vue` 在没有维护工作稿时把 `active_resource.revision` 传入 `SignalDryRunDialog`。该值来自 `dynamic_resource_revision.revision`，而 Gateway 的 `?revision=` 查询的是 `kbd_revision.revision_no`。

失败方式：两个序列恰好相等时问题被掩盖；不相等时可能返回 `KBD_REVISION_NOT_FOUND`，或命中同一 KBD 的另一条 Proposal/Expert 快照，导致 Bundle 编译到错误知识内容。

修复要求：运行时响应必须同时返回明确的 `knowledge_revision_no`（或 `knowledge_revision_id`），前端只使用该字段；`runtime_revision` 仅用于展示和运行时审计。不能通过整数相等推断映射关系。

### P1：`fixture.bundle.revision` 名称误导

数据库、Go 结构和前端同时暴露 `kbd_revision`、`draft_revision`、Bundle digest，另有名为 `revision` 的列。调用方很容易把 Bundle 编辑轮次、KBD 知识序号和运行时序号混用。

修复要求：新增协议字段 `knowledge_revision_no`，保留旧字段只读兼容；数据库列迁移为 `source_kbd_revision_no`，待所有消费者切换后删除旧列。

### P1：父 Draft 降级不是原子操作

`ReviseDraft` 先提交新 Draft，再独立更新父 Draft 为 `stale`。第二步失败会留下两个可见 Draft，且当前实现只记录日志。

修复要求：新子 Draft 插入、父 Draft 的条件更新、依赖/outbox 写入必须在同一事务中完成；条件更新必须带 `WHERE status='draft'` 并检查影响行数。若父状态已变化，应返回明确的并发冲突，而不是静默成功。

### P1：`input_fingerprint` 没有唯一约束

当前只有普通索引。并发 Compile 可能都查不到指纹并插入逻辑重复 Bundle，应用层幂等判断无法作为最终防线。

修复要求：增加 `UNIQUE (input_fingerprint)`，对历史 NULL 值使用部分唯一索引；插入统一采用 `ON CONFLICT`，再校验 digest 相同，否则报 `compiler_nondeterministic_output`。

### P1：对象存储与数据库提交存在窗口

Publish 先提交对象，再更新数据库状态。数据库失败时对象已 published，但 Bundle 仍可能是 approved，`object_uri` 与状态不一致。

修复要求：引入可重试的 outbox/发布任务，明确 `object_prepared -> db_published -> object_committed` 状态；读取端只信任 DB 中已确认的对象摘要，补偿任务负责清理孤儿对象或重试提交。

### P2：显式 KBD revision 缺少类型/状态门禁

Resolver 按 `kbd_entry_id + revision_no` 读取，没有约束必须是当前工作稿、已批准 Expert 或明确允许的 Proposal。测试允许工作稿进入编译，但生产协议没有把这个选择表达出来。

修复要求：API 增加 `revision_selector`：`active`、`working`、`explicit`；`explicit` 必须返回 revision 类型、状态、checksum，并由调用方声明用途（dry-run 或 publishable bundle）。默认 Bundle 工厂只接受 `active`，Signal dry-run 才可接受 `working`。

## 附录 E：原方案字段模型（兼容迁移参考）

### 6.1 对外协议字段

所有跨服务请求/响应使用以下命名：

```json
{
  "knowledge_revision_id": 12345,
  "knowledge_revision_no": 17,
  "knowledge_revision_checksum": "sha256:...",
  "runtime_revision": 9,
  "runtime_checksum": "sha256:...",
  "bundle_digest": "sha256:...",
  "bundle_input_fingerprint": "sha256:...",
  "bundle_draft_no": 2,
  "parent_bundle_digest": "sha256:...",
  "compiler_revision": "bundle-factory-v4-fixture-assets"
}
```

规则：

- `*_id` 是数据库实体身份；`*_no` 是局部展示序号；`*_checksum/digest` 是内容身份。
- Runtime 结果必须同时携带 `knowledge_revision_id/no` 和 `runtime_revision`，否则无法完成跨层追溯。
- Bundle 以 `bundle_digest` 作为唯一对外身份；`bundle_draft_no` 仅用于同一 lineage 的人类展示。

### 6.2 唯一事实源

| 事实 | 唯一来源 | 其他位置的角色 |
| --- | --- | --- |
| KBD 编辑快照 | `kbd_revision` | `KbdEntry` 仅作兼容主记录和 head 指针 |
| Agent 当前 KBD | `dynamic_resource_active` 指向 `dynamic_resource_revision` | 不从 `KbdEntry.status` 推断 |
| Bundle 内容 | Manifest `bundle_digest` + 对象摘要 | DB 保存索引与生命周期 |
| Bundle lineage | `compile_input.parent_bundle_digest/draft_revision` | 不从 DB 自增 id 推断 |
| Bundle 激活 | `fixture.bundle_activation` | 不从 Bundle status 推断 |
| Factory 资产 | `fixture.asset_revision` | Bundle 只保存冻结依赖 |

### 6.3 必须保持的数据库不变量

1. `(kbd_entry_id, revision_no)` 唯一；`kbd_revision` append-only。
2. 一个 KBD 最多一个 `working_revision_id`；Proposal 重抽自动使旧工作稿失去当前 head 资格。
3. 一个运行时资源只有一个 active 指针，且指向存在的 revision。
4. 一个 `input_fingerprint` 最多一条 Bundle；同指纹必须得到同 `bundle_digest`。
5. 一个 Bundle digest 对应不可变 Manifest；状态变更不得改 digest/object。
6. 一个 `support_id` 只有一个激活目标；激活变更由 generation CAS 确认。
7. 一个 `asset_key` 最多一个 published 修订；Bundle 依赖必须记录 revision + digest。

## 附录 F：当前状态机

### KBD

```text
proposal -> expert(working) -> expert(approved) -> runtime published -> active
    |            |                  |                    |
    +--重抽------+-保存--------------+--------------------+--维护发布
```

`working`、`approved` 是知识治理状态；`runtime published/active` 是运行时状态。两者不能合并成一个 status。

### Bundle

```text
draft -> validated -> approved -> published -> active
  |         |           |           |
  +---------+-----------+-----------+-- revise -> new draft
                                      \
                                       retire/stale（不可再作为工作稿）
```

`published` 表示 Bundle 可以被激活，不表示已经是某个 `support_id` 的 active。只有 `bundle_activation.status='active'` 才代表运行时确认使用。

## 附录 G：当前方案的落地顺序

### 阶段 A：先堵语义错配（无破坏性）

- Resolver 返回 `knowledge_revision_id/no/checksum` 与 `runtime_revision/checksum`。
- 前端 Signal dry-run 改用 `knowledge_revision_no`；没有映射时 fail closed，不回退到 runtime 序号。
- Gateway、hci-sim、前端日志统一打印 `trace_id + support_id + knowledge_revision_id + runtime_revision + bundle_digest`。
- 增加一个不相等序列的回归测试，证明不会误编译。

### 阶段 B：冻结 Bundle 幂等和 lineage

- 为 `input_fingerprint` 增加部分唯一约束并清理重复历史数据。
- 将 `ReviseDraft` 合并为单事务/CAS；父状态变化返回 409。
- 新增 `bundle_draft_no` 和 `source_knowledge_revision_no`，旧 `revision` 只读兼容。

### 阶段 C：收敛发布与激活

- Publish 使用 DB 状态机 + outbox 补偿对象存储提交。
- 统一通过 `bundle_activation.generation` 完成激活确认和回滚。
- Runtime 启动时校验 Manifest digest、object digest、依赖 digest 三者一致。

### 阶段 D：删除歧义字段

- 所有消费者切换完成后，停止写入 `fixture.bundle.revision` 的新语义。
- API 文档和前端显示全部改为领域前缀名称。
- 经过一个完整发布周期后再删除兼容字段，避免旧 Bundle/离线同步无法读取。

## 附录 H：对抗性测试清单

每次修改版本逻辑必须至少验证以下场景：

- KBD revision 序号为 17、Runtime revision 为 4 时，dry-run 是否仍读取 KBD 17。
- Proposal 重抽后，旧 Expert 是否被拒绝作为当前评估/Bundle 输入。
- 两个并发 Compile 使用同一 `input_fingerprint` 时，最终只有一条 Bundle。
- ReviseDraft 在父状态被其他请求改写后，是否返回冲突且不留下孤儿子 Draft。
- Publish 的 DB 提交失败后，是否可重试且不会产生错误 active 指针。
- 同一 digest 被 retire 后重新 Compile，是否只恢复状态、不改变 Manifest/object digest。
- Factory 模板退役后，历史 Bundle 是否仍能按冻结依赖读取。
- active 切换期间 Runtime 重启，是否只接受 generation 与 digest 同时匹配的目标。
- 每条请求能否通过同一个 `trace_id` 串起 Gateway、KB Service、hci-sim、DB 和 Runtime 日志。

## 附录 I：代码入口索引

- KBD 快照与 head：`backend/kb-service/app/services/kbd_revision_service.py`
- KBD Resolver：`backend/kb-service/app/services/hci_sim_resolver.py`
- KBD 发布/维护 API：`backend/kb-service/app/routes/admin.py`
- Runtime 发布器：`backend/shared/dynamic_resource/publisher.py`
- Bundle 内存控制面：`hci_sim/internal/controlplane/controlplane.go`
- Bundle PostgreSQL Registry：`hci_sim/internal/database/bundle_registry.go`
- KBD/Bundle Schema：`database/desired_schema.sql`、`database/hci-sim-migrations/000001_control_plane.sql`、`000003_controlplane_bundle_lifecycle.sql`、`000004_bundle_activation.sql`
- Factory 资产：`database/hci-sim-migrations/000006_fixture_asset_revision.sql`
- 前端版本展示与 dry-run 入口：`frontend/admin/src/views/KbdReviewView.vue`、`frontend/admin/src/components/editors/SignalDryRunDialog.vue`

这份文档是语义收敛基线。后续代码 PR 应在描述中标明修改了哪个身份空间、维护了哪条不变量，以及新增了哪一个对抗性测试。
