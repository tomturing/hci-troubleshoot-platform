# KBD 与仿真资产统一版本管理最终设计

> 状态：归档版最终方案（2026-08-31）
> 适用范围：KBD 知识、Signal、仿真输出、Verification Asset、Bundle 构建与 Runtime 激活
> 设计方法：第一性原理 + 对抗性审查
> 取代：`kbd-bundle-revision-governance.md` 中的阶段性提案，以及“将全部内容物理合并为单表快照”的早期方案

## 1. 最终决策

最终采用“**业务统一、物理分离、显式血缘**”的组合方案：

```text
产品层：KbdPackage（用户只理解：当前草稿 / 正式发布版）
    ↓
领域层：PackageSnapshot（一次完整的业务视图）
    ↓
物理层：KnowledgeSnapshot + VerificationSet + BundleBuild
    ↓
运行时：KnowledgeRelease + KnowledgeActive + BundleActive
```

不采用两个极端：

- 不把所有内容都压进一张巨大 JSONB 快照表；这会把知识、测试凭证和运行制品的生命周期强行绑死。
- 不把所有对象直接暴露给前端；这会把内部的 release/build/asset 复杂度重新泄漏给用户。

用户看到一个 KbdPackage，系统内部保留可审计、可复现、可独立回滚的多个不可变对象。

## 2. 第一性原理：版本管理要保证的事实

版本系统最终必须能证明六件事：

1. **内容是什么**：某次修改产生了哪份不可变快照。
2. **谁批准了它**：哪次审核使该快照具备发布资格。
3. **构建出了什么**：使用哪套编译器、契约和资产生成了哪个 Bundle。
4. **当前运行什么**：Agent 和仿真 Runtime 当前分别指向哪个发布/制品。
5. **能否复现**：用同样的输入和依赖，是否得到同样的 digest。
6. **能否回滚**：回滚是否只移动指针，不修改历史内容。

因此，序号、状态、指针和内容摘要必须分工：

| 概念 | 作用 | 是否可变 |
| --- | --- | --- |
| `*_id` | 数据实体身份 | 不变 |
| `*_digest/checksum` | 内容身份 | 不变 |
| `*_no` | 同一对象内的人类展示顺序 | 新建时递增 |
| `status` | 生命周期状态 | 可变，但必须受状态机约束 |
| `active`/`desired` | 当前运行指针 | 可变，必须 CAS/generation |

任何跨服务协议都禁止传递无领域前缀的裸 `revision`。

## 3. 目标领域模型

### 3.1 `KbdPackage`：业务聚合根与用户工作区

`KbdPackage` 以 `support_id` 为业务唯一键，是前端和管理 API 的唯一工作区入口。它不直接保存大 payload，只保存当前指针和工作区状态：

```text
package_id
support_id
working_snapshot_digest
active_release_id
workspace_version
status: draft_editing | published
updated_at / trace_id
```

用户只能看到：

- 当前草稿：正在编辑、试运行和累积验证凭证的工作区；
- 正式发布版：已审核、已构建并可运行的版本；
- 历史：按需查看不可变快照和发布记录。

### 3.2 `PackageSnapshot`：业务一致性边界

`PackageSnapshot` 是一次完整业务视图的 manifest，不把所有大对象内嵌进去，而是引用其不可变摘要：

```text
package_snapshot_digest
support_id
parent_snapshot_digest
knowledge_snapshot_digest
signal_spec_digest
simulation_spec_digest
verification_set_digest
prompt_revision
tool_contract_revision
policy_revision
compiler_revision
created_by / commit_reason / trace_id / created_at
```

`PackageSnapshot` 是 KbdPackage 的版本身份。它保证“本次发布看到的知识、规则、仿真和验证集合”是一致的，但允许各部分在物理存储上独立演进。

### 3.3 `KnowledgeSnapshot`：KBD 内容事实源

对应现有 `kbd_revision` 的不可变快照：

```text
knowledge_snapshot_id
kbd_entry_id
knowledge_revision_no       # 仅同一 KBD 内展示
knowledge_checksum          # 跨服务身份
revision_type: proposal | expert
baseline_proposal_id
payload_json
actor / trace_id / created_at
```

`KbdEntry` 降级为查询投影和兼容接口，不再作为编辑事实源。

### 3.4 `VerificationSet` 与 `VerificationAsset`：验证凭证事实源

验证资产不再直接改写 Bundle，也不要求每个测试都重新编译完整 Bundle：

```text
VerificationSet
  ├─ verification_set_digest
  ├─ package_snapshot_digest
  └─ assets[]

VerificationAsset
  ├─ asset_id / asset_digest
  ├─ signal_id / processing_index
  ├─ dataset_id / source_type / input_digest
  ├─ deterministic_input
  ├─ ai_input / raw_response_hash
  ├─ output / evidence / downstream_result
  ├─ model / prompt_revision / contract_version
  └─ trace_id / run_id / created_at
```

单次试运行保存为不可变 `VerificationAsset`；工作区只追加新的集合引用。相同 `asset_digest` 幂等，不重复写入。

### 3.5 `KnowledgeRelease`：审核与 Runtime 发布事实

`KnowledgeRelease` 表达“哪份 PackageSnapshot 经过审核并晋级为可运行版本”：

```text
release_id
package_snapshot_digest
knowledge_snapshot_digest
approval_ref
runtime_revision
runtime_checksum
status: prepared | published | revoked
actor / trace_id / timestamps
```

现有 `dynamic_resource_revision` 可以继续作为存储实现，但必须记录 `release_id`、`package_snapshot_digest` 和 `knowledge_snapshot_digest`。Resolver 只能从 `KnowledgeRelease` 读取，不得通过 Runtime revision 猜 KBD revision。

### 3.6 `BundleBuild`：不可变仿真制品

`BundleBuild` 是编译结果，唯一身份是 `bundle_digest`：

```text
bundle_build_id
bundle_digest
package_snapshot_digest
knowledge_release_id
bundle_input_digest
manifest_digest
object_digest / object_uri
compiler_revision
dependency_snapshot
status: draft | validated | approved | published | retired
created_by / trace_id / timestamps
```

BundleBuild 的 Manifest、对象字节和依赖摘要不可修改。状态变化只改变生命周期，不改变内容身份。

### 3.7 两个运行指针

```text
KnowledgeActive(kbd_entry_id) -> KnowledgeRelease
BundleActive(support_id)       -> BundleBuild
```

二者分别负责 KBD Runtime 和仿真 Runtime，不复用任何序号。激活必须携带 `generation`、`desired_digest`、`active_digest` 和 `trace_id`。

## 4. 目标主链路

```text
专家打开 KbdPackage
  -> working PackageSnapshot
  -> Signal DryRun（绑定 observed_snapshot_digest）
  -> VerificationAsset 不可变落库
  -> VerificationSet 新 digest
  -> PackageSnapshot 新 digest
  -> KnowledgeRelease（审核发布）
  -> BundleBuild（基于同一 PackageSnapshot 构建）
  -> BundleActive（Runtime 激活）
```

其中：

- “保存测试结果”只改变工作区和验证集合，不直接修改已发布 BundleBuild；
- “发布”才创建新的 Release/Build，并通过门禁校验；
- “回滚”只移动 active 指针；
- “重新编译”可以复用相同内容 digest，但必须产生新的工作流事件和审计记录。

## 5. API 契约

### 5.1 读取当前上下文

```http
GET /api/v1/kbd/{support_id}/context?scope=working_draft
GET /api/v1/kbd/{support_id}/context?scope=active_release
```

响应必须返回完整上下文身份：

```json
{
  "support_id": "41464",
  "scope": "working_draft",
  "package_snapshot_digest": "sha256:...",
  "knowledge_snapshot_digest": "sha256:...",
  "knowledge_revision_id": 12345,
  "knowledge_release_id": null,
  "bundle_build_id": null,
  "workspace_version": 7
}
```

### 5.2 Signal 试运行

```http
POST /api/v1/kbd/{support_id}/signals/{signal_id}/dry-run
```

请求必须绑定读取时的快照：

```json
{
  "scope": "working_draft",
  "observed_snapshot_digest": "sha256:...",
  "input_payload": "...",
  "dry_run_mode": "full_signal"
}
```

后端只能在 `observed_snapshot_digest` 仍是当前工作头时执行；否则返回 409，并要求前端刷新或执行三方 Diff。

### 5.3 保存验证凭证

```http
POST /api/v1/kbd/{support_id}/working-draft/verification-assets
```

请求携带：

```json
{
  "observed_snapshot_digest": "sha256:...",
  "verification_token": "...",
  "asset_digest": "sha256:...",
  "update_simulation_stdout": true
}
```

服务端在同一事务中完成：验证 token、校验 asset、追加 VerificationSet、生成 PackageSnapshot、CAS 更新 `working_snapshot_digest`。任何一步失败都不改变工作头。

### 5.4 Bundle 构建

```http
POST /api/v1/kbd/{support_id}/bundle-builds
```

请求只接受明确的 `package_snapshot_digest` 或 `knowledge_release_id`，禁止接受 `kbd_revision` 或裸 `revision`。构建输入必须包含：

```text
package_snapshot_digest
knowledge_release_id
verification_set_digest
tool_contract_revision
policy_revision
prompt_revision
compiler_revision
approved_artifact_digests
```

## 6. 状态机与事务边界

### 6.1 Package 工作区

```text
published
    -> open maintenance -> draft_editing
draft_editing
    -> save asset/content -> new working snapshot
    -> publish -> published + new active release
    -> discard -> restore previous working pointer
```

同一 `support_id` 只能有一个 working head；并发更新使用 `observed_snapshot_digest` 或 `workspace_version` CAS。

### 6.2 VerificationAsset

```text
previewed -> validated -> attached_to_working_snapshot
```

试运行结果一旦落库不可修改；错误结果也保留，不能只保存 PASS 样本，否则无法审计失败原因。

### 6.3 KnowledgeRelease

```text
prepared -> published -> revoked
```

审核、Runtime snapshot、active 指针更新必须在同一数据库事务内完成；跨对象存储的动作使用 outbox 补偿。

### 6.4 BundleBuild

```text
draft -> validated -> approved -> published
  \-> stale / retired
```

新 Build 创建、父工作区 CAS、依赖快照和 outbox 事件必须在同一事务中完成。旧 Build 永不覆盖；父子关系使用 digest。

## 7. 关键数据库约束

必须建立以下约束和索引：

1. `kbd_package.support_id` 唯一。
2. `(kbd_entry_id, knowledge_revision_no)` 唯一。
3. `package_snapshot_digest` 全局唯一，且必须通过格式校验。
4. `parent_snapshot_digest` 自引用外键，禁止悬空父节点。
5. `verification_asset.asset_digest` 唯一。
6. `bundle_build.bundle_digest` 唯一。
7. `bundle_build.bundle_input_digest` 唯一，历史 NULL 使用部分唯一索引。
8. 一个 `support_id` 只有一个 active Bundle 指针。
9. 一个 `asset_key` 只有一个 published Factory revision。
10. 任何状态转换使用条件更新并检查影响行数。

## 8. 兼容当前系统的迁移路径

### 阶段一：协议防错

- 增加 `package_snapshot_digest`、`knowledge_revision_id`、`knowledge_release_id`、`bundle_build_id`。
- 前端停止使用 `active_resource.revision` 作为 KBD revision。
- 所有 dry-run 请求携带 `observed_snapshot_digest`。
- 旧 `kbd_revision` 字段仅保留兼容读取，不允许新代码新增依赖。

### 阶段二：建立 PackageSnapshot 和工作区

- 从当前 `KbdEntry + kbd_revision + fixture.bundle.compile_input` 回填 PackageSnapshot。
- 将当前 Draft 的 `verification_assets` 拆成 VerificationAsset/VerificationSet。
- `KbdEntry` 改为投影，新增投影校验和重建命令。

### 阶段三：建立 KnowledgeRelease

- 为每个当前 Runtime active KBD 创建对应 Release。
- 在 `dynamic_resource_revision.contract_json` 写入 Release 和 Snapshot 身份。
- Resolver 只通过 Release 读取；历史接口通过兼容层转换。

### 阶段四：重构 Bundle Registry

- `fixture.bundle` 增加 `package_snapshot_digest`、`knowledge_release_id`、`bundle_input_digest`、`workspace_id`。
- 将 `fixture.bundle.revision` 改为只读兼容字段 `source_knowledge_revision_no`。
- 子 Build 创建、父工作区 CAS、依赖冻结和 outbox 写入收敛为单事务。
- 处理历史重复 input fingerprint 后再增加唯一约束。

### 阶段五：对象存储与激活收敛

- Bundle 发布采用 prepare/commit/outbox 补偿。
- Runtime 只接受 DB 已确认的 object digest。
- `BundleActive` 使用 generation CAS，支持失败重试和指针回滚。

### 阶段六：用户体验切换与清理

- Bundle 工厂改为“当前草稿沙箱 + 正式发布版 + 审计历史”。
- Signal DryRun 改为“保存到当前草稿测试集”。
- 一个完整发布周期后停止写入旧 `revision` 字段，最后删除旧查询参数。

## 9. 数据库加法与减法清单

### 9.1 统计口径

本节只统计 KBD、仿真资产、Bundle 构建与 Runtime 激活版本域；诊断工单、会话、工具执行、SOP 执行等平台业务表不属于本次减法范围。

当前仓库的基线是：

| 数据库 | 当前物理表数 | 本方案新增 | 本方案改造 | 本方案最终退役 | 迁移后净变化 |
|---|---:|---:|---:|---:|---:|
| 主库 `hci_troubleshoot` | 64 | 4 | 5 | 0 | +4 |
| hci-sim 独立库 | 19 | 0 | 8 | 3 | -3 |
| 合计（本次范围） | 83 | **4** | **13** | **3** | **+1** |

“改造”按现有物理表计算，不把逻辑重命名重复计数。`fixture.bundle` 演进为 `BundleBuild`、`fixture.bundle_activation` 演进为 `BundleActive`，第一阶段保留物理表名以降低切换风险，兼容窗口结束后再执行重命名或建立同义视图。

仓库还存在一组历史 Atlas 迁移曾创建的 15 张主库 `agent_test_*` 表。`20260813000000_drop_orphan_agent_test_tables.sql` 已明确删除它们；若某环境尚未执行该迁移，应先执行并完成行数、外键、备份恢复校验。这 15 张表不再计入当前 64 张 desired schema，也不应在新方案中重新创建。

逐张名单如下（全部属于主库历史孤儿表，不是 hci-sim 当前运行表）：

| 分组 | 已删除/不得重建的表 |
|---|---|
| Scenario | `agent_test_scenario` |
| Bundle | `agent_test_fixture_bundle`、`agent_test_fixture_dependency`、`agent_test_fixture_provenance`、`agent_test_fixture_approval`、`agent_test_fixture_audit`、`agent_test_fixture_stale_outbox` |
| Artifact | `agent_test_artifact`、`agent_test_artifact_scan`、`agent_test_artifact_approval` |
| Run/Runtime | `agent_test_run`、`agent_test_run_attempt`、`agent_test_run_event`、`agent_test_run_result`、`agent_test_runtime_instance` |

### 9.2 新增表（4 张）

| 新表 | 所属库 | 不可再拆的职责 | 关键约束 | 不替代的现有表 |
|---|---|---|---|---|
| `kbd_package` | 主库 | 以 `support_id` 为唯一键的用户工作区与当前指针（working snapshot、active release、workspace CAS 版本） | `support_id` 唯一；workspace_version CAS；trace_id 非空 | 不保存完整 KBD payload，不替代 `kbd_entry` 查询投影 |
| `package_snapshot` | 主库 | 一次发布视图的完整业务 manifest，冻结知识、Signal、仿真、验证集合及 Prompt/Tool/Policy/Compiler 依赖 | `package_snapshot_digest` 唯一；父 digest 不可悬空；输入字段完整性检查 | 不保存原始图片、LLM 全量响应或 Bundle 字节 |
| `verification_set` | 主库 | 某个 PackageSnapshot 使用的验证资产集合版本 | `verification_set_digest` 唯一；集合成员不可变；变更生成新 digest | 不把测试结果写回 `fixture.bundle` |
| `verification_asset` | 主库 | 单次 Signal/仿真试运行的不可变证据（输入、输出、模型、Prompt、Contract、trace） | `asset_digest` 幂等唯一；执行失败也落库；敏感原文按保留策略脱敏 | 不替代 `kbd_batch_job_item`，后者仍是异步作业状态 |

不新增 `knowledge_release`、`knowledge_active`、`bundle_build` 三张重复表：`dynamic_resource_revision`、`dynamic_resource_active` 和 `fixture.bundle` 分别通过增加显式身份字段承载这三个逻辑对象，避免再造一套平行 revision。

### 9.3 需要改造的既有表（13 张）

| 表 | 库/schema | 改造内容 | 最终语义 | 证据/风险 |
|---|---|---|---|---|
| `kbd_entry` | 主库 | 增加 `working_snapshot_digest`、`active_release_id` 等投影/兼容字段；禁止新代码把它当编辑事实源 | KbdPackage 查询投影 | ingest、admin、Vision pipeline 仍直接读写，不能删除 |
| `kbd_revision` | 主库 | 规范为 KnowledgeSnapshot；补齐 digest、parent、generation metadata 和唯一约束 | KBD 不可变内容事实源 | 已有 Proposal/Expert 血缘和大量 API 依赖 |
| `dynamic_resource_revision` | 主库 | `contract_json` 固定写入 release/package/knowledge 身份；checksum 覆盖全部输入 | KnowledgeRelease 的物理存储 | Agent Resolver、离线同步以该表为运行时快照来源 |
| `dynamic_resource_active` | 主库 | 增加 generation、desired/active checksum 的 CAS 语义；KBD 类型映射为 KnowledgeActive | KBD Runtime active 指针 | 现有 Runtime 热切换和回滚依赖它，不新增平行 active 表 |
| `dynamic_resource_usage_audit` | 主库 | 审计字段补齐 package/knowledge/release/bundle 身份 | 运行时使用审计 | 属于可观测性与合规事实，不能因版本重构删除 |
| `control_plane.scenario` | hci-sim | 降级为 `PackageSnapshot`/Bundle 编译输入索引；新写入只用 `indexed/gap` | 去生命周期化的索引 | 现有 Bundle/Run FK 和列表查询仍使用，不能首期删除 |
| `control_plane.run` | hci-sim | 新增/迁移 `package_snapshot_digest`、`knowledge_release_id`、`bundle_build_id`；旧 `kbd_revision/scenario_id` 只读兼容 | 绑定精确版本的 TestRun | 运行、Lease、诊断回查必须能反查完整血缘 |
| `fixture.bundle` | hci-sim | 增加 package/release/input/compiler 身份；`revision` 改为只读兼容；输入与对象 digest 不可变 | BundleBuild 物理表 | BundleRegistry、对象存储、审批、stale 逻辑均已接线 |
| `fixture.dependency` | hci-sim | 依赖类型扩展为 Snapshot/Release/Asset/Tool/Policy，并强制保存 revision+digest | BundleBuild 依赖冻结表 | stale 反向查找依赖，不能并入可变 JSON 后丢索引 |
| `fixture.approval` | hci-sim | 审批记录增加 package_snapshot/release 关联和 trace；允许同角色重审但保留历史 | BundleBuild 发布门禁事实 | Bundle 发布代码明确查询 expert/security 双批准 |
| `fixture.bundle_activation` | hci-sim | 增加 package/release 身份，保留 generation、desired/active/previous、失败码 | BundleActive Runtime 指针 | 失败重试、CAS、回滚均依赖该表，不能用 Bundle status 替代 |
| `control_plane.outbox` | hci-sim | 统一承载 run、fixture stale、activation、projection rebuild topic；补齐唯一事件身份 | 可靠事件投递唯一事实源 | 000005 已建立，消费者和恢复逻辑已接线 |
| `fixture.asset_revision` | hci-sim | 增加 package/snapshot 关联或由 dependency_snapshot 引用；保留 asset_key+revision+digest 不可变约束 | Factory 模板/实例资产快照 | QKV 模板/实例独立发布，不能塞进 Bundle JSON 或删除 |

### 9.4 最终退役表（3 张）

以下表的替代能力已由更早的 `000005` 建立并完成兼容镜像。本方案通过后续 `000008` contract 迁移退役；执行时仍必须先满足逐表门禁，不能把“仓库已有 DROP”解释为允许绕过生产观察和备份：

| 表 | 所属库/schema | 为什么非必要 | 替代方案 | 删除门禁 |
|---|---|---|---|---|
| `fixture.provenance` | hci-sim/fixture | 当前代码无生产读写；原语只能表达 route→artifact，无法表达 template/instance，且与 `compile_input.route_sources` 重复 | `fixture.bundle.compile_input.route_sources` + `fixture.dependency` + `artifact.metadata` | 全量回填 route_sources；查询 0 命中；至少一个完整发布周期无写入 |
| `control_plane.run_outbox` | hci-sim/control_plane | 与统一 `control_plane.outbox` 重复，000005 已镜像历史事件 | `control_plane.outbox(topic=run)` | 新代码停止写入；镜像窗口结束；pending=0；回放校验通过 |
| `fixture.stale_outbox` | hci-sim/fixture | 与统一 outbox 重复，无法和 Run/activation 使用同一 claim/retry 语义 | `control_plane.outbox(topic=fixture_stale)` | 新代码停止写入；stale 事件回放一致；pending/processing=0 |

### 9.5 明确不删除的“看起来像冗余”表

| 表/表组 | 表面理由 | 对抗性结论 |
|---|---|---|
| `kbd_image` | 图片也可放在 `images_json` | 现有 ingest、Vision 重识图和降级脚本仍读取原始字节；删除会破坏重算和证据复核。后续只能迁移到对象存储并保留 digest 后再评估 |
| `kbd_batch_job`、`kbd_batch_job_item` | 与版本状态都有 status 字段 | 它们记录异步任务编排、重试和逐条失败，不是版本实体；应考虑抽象通用 Job，但不能在本方案中删除 |
| `fixture.dependency`、`fixture.approval` | 可以塞进 Bundle JSON | 依赖反向失效和双人审批需要可查询、可约束、可审计的关系事实；合并 JSON 会丢失门禁能力 |
| `fixture.asset_revision` | Bundle 已有 object digest | Factory 资产可独立发布、复用和回滚；Bundle digest 不能替代 asset revision |
| `artifact.metadata`、`artifact.scan`、`artifact.approval` | 验证资产已有输出字段 | Artifact 是安全扫描和批准后的输入凭证，删除会使 realistic route 失去 provenance gate |
| `control_plane.run*`、`runtime_instance` | 版本重构后似乎可合并 | Run、Attempt、Event、Result、Runtime 是运行事实和租约边界，合并会破坏幂等、重试和回放 |
| `dynamic_resource_usage_audit` | 不是版本主表 | 它是每次 Agent 使用版本的合规审计；必须扩展血缘字段而非删除 |

### 9.6 删除顺序与数量验收

1. 先执行 4 张新表的建表和历史回填，再对 13 张既有表增加可空兼容列；任何 digest/外键校验失败都 fail closed。
2. 切换读路径到 PackageSnapshot/KnowledgeRelease/BundleBuild 身份，旧字段只读；确认 `000005` 统一 outbox 兼容镜像已经过完整发布周期。
3. 完成 `fixture.provenance` 回填和三张退役表的 pending/processing 清零校验后，才允许 `000008` contract 段执行 DROP；执行前必须保留可恢复备份。
4. 验收数字固定为：**新增 4、改造 13、退役 3、净增 1**（不含已存在的主库 `agent_test_*` 清理）；若环境仍有 15 张 `agent_test_*`，执行既有清理迁移后的实际删除总数为 **18 张**。

## 10. 对抗性审查

### 10.1 正文改动但 Signal 未改

不能仅凭 Signal JSON 未变化就自动宣称所有验证资产仍有效。必须同时比较 Prompt、Tool Contract、Policy、Compiler 和 Simulation 依赖；只有兼容证明通过，才允许复用 VerificationSet。

### 10.2 两个专家同时保存

第二个请求必须因 digest/version CAS 失败返回 409，不能覆盖第一个工作头。若支持自动合并，合并结果必须重新生成完整 PackageSnapshot，并重新执行结构化 Signal 门禁。

### 10.3 Prompt 在试运行期间切换

试运行开始时固定 `prompt_revision`；中途切换只影响后续请求。VerificationAsset 必须记录实际使用的 Prompt/Model/Contract，不能从当前 active 反推。

### 10.4 重试造成重复副作用

LLM 纯生成调用可有限重试；带工具调用或外部副作用的请求必须携带幂等键并显式声明 retry policy。流式响应首 Token 后禁止透明重试。

### 10.5 旧 Bundle 重新进入编辑

可以复用相同的 Bundle 内容 digest，但必须创建新的 Workspace/Promotion 事件；不能把 retired 记录静默改回 draft 而丢失生命周期事实。

### 10.6 快照过大和隐私泄露

PackageSnapshot 只保存引用 digest，不把全部原始输出和大对象复制进 JSONB。VerificationAsset 按保留策略管理原始输入，敏感字段脱敏后再进入日志和审计。

### 10.7 新增 Release 反而形成第六套版本

Resolver、Runtime 和 Bundle 必须以 `KnowledgeRelease`/`PackageSnapshot` 为唯一入口；禁止继续直接根据 `dynamic_resource_active.revision` 或 `kbd_entry.status` 猜测版本。

## 11. 可观测性与指标

每条请求必须贯穿同一个 `trace_id`，并至少记录：

```text
support_id
package_snapshot_digest
knowledge_snapshot_digest
knowledge_release_id
bundle_build_id / bundle_digest
verification_asset_id
signal_id
dataset_id
prompt_revision / model_id
tool_contract_revision / policy_revision / compiler_revision
```

建议指标：

- `kbd_package_cas_conflicts_total`
- `verification_asset_attach_total{status}`
- `bundle_build_total{status}`
- `bundle_build_idempotency_conflicts_total`
- `knowledge_release_activation_total{status}`
- `bundle_activation_total{status}`
- `projection_rebuild_total{status}`

高基数 digest、trace 和 asset id 进入结构化日志/Trace，不作为 Prometheus label。

## 12. 验收标准

1. 前端和跨服务 API 不再传裸 `revision`。
2. 任意 Agent 或仿真运行都能沿 `trace_id -> BundleBuild -> PackageSnapshot -> KnowledgeRelease -> KnowledgeSnapshot` 完整反查。
3. KBD 内容、Runtime 序号、Bundle Draft 序号数值相同也不会被视为同一版本。
4. 试运行结果只能附着到执行时观察到的工作快照；快照变化时返回冲突。
5. 同一规范化 Bundle 输入并发构建最多生成一个 BundleBuild。
6. 验证资产、Bundle Manifest、对象字节和依赖摘要均不可变。
7. 发布失败不会改变旧 active；回滚只移动指针。
8. 正文、Signal、仿真资产或 Prompt 任一依赖变化都能触发正确的重新验证/重新构建判断。
9. 通过一个 `trace_id` 能重建完整调用和版本血缘。
10. 旧数据可读取、可审计、可迁移；迁移失败时 fail closed，不静默猜版本。

这份文档是后续实现、评审和验收的唯一版本管理基线。任何新 PR 必须说明涉及的对象、维护的不变量、迁移兼容策略以及新增的对抗性测试。

## 13. 六阶段落地结果（2026-09-01）

本节记录实现后的事实，不以“代码已合并”等同于“生产数据已安全切换”。六阶段代码、迁移和兼容读写已完成；生产执行仍必须经过备份、观察窗口和迁移门禁。

| 阶段 | 已落地能力 | 可验证出口 |
|---|---|---|
| 一：协议防错 | API、Resolver、CompileInput、Run、Lease 全链路透传 `package_snapshot_digest / knowledge_release_id / bundle_build_id`；Bundle/Run 发布身份必须成对出现 | 缺失任一发布身份时 fail closed；旧 `kbd_revision` 只作内部兼容来源序号；工作稿不得借用旧 active Release |
| 二：PackageSnapshot | 新增 Package、Snapshot、VerificationSet、VerificationAsset；工作头使用行锁和 observed digest CAS；Signal DryRun 主路径直接追加不可变验证资产并推进 Package 工作头 | 并发旧工作头返回 409；相同资产重试不推进 workspace；知识或冻结依赖变化自动清空旧 VerificationSet |
| 三：KnowledgeRelease | KBD 发布事务先冻结 PackageSnapshot，再创建带 UUID `release_id` 的动态资源 revision，并原子推进 Package/KbdEntry active 指针和 generation | Resolver 从 active revision 读取 Release UUID，不再把数据库自增 ID 当跨服务版本 |
| 四：Bundle Registry | 编译输入规范化指纹、`bundle_input_digest` 唯一、`workspace_id`、只读 `source_knowledge_revision_no`；子 Build、依赖、`bundle.compiled` outbox 和父 Draft 淘汰同事务 | 指纹排除自身计算结果且校验调用方声明；相同输入最多一个 Build；过期父 Draft CAS 失败 |
| 五：对象与激活 | 复用 prepare/verify/commit 对象协议；BundleActive 使用 desired/active/previous + generation，Runtime 校验后 ACK，失败保留旧 active | 发布或激活失败不覆盖旧 active；pending/failed 可恢复和重试 |
| 六：体验与清理 | DryRun 携带 observed PackageSnapshot；PASS 结果正文、证据和 AI 原始响应摘要全部进入签名边界并保存到当前草稿测试集；无 Package 身份时才走旧 Bundle Draft 兼容路径 | 篡改预览正文验签失败；旧 outbox 有未完成事件或 provenance 未回填时，迁移主动失败，不执行 DROP |

### 13.1 数据迁移门禁

1. 主库先执行历史 KBD/active revision 回填，再检查 `kbd_entry -> kbd_package -> package_snapshot -> dynamic_resource_revision.release_id` 无断链。
2. hci-sim 必须确认旧 `run_outbox/stale_outbox` 全部为 `processed`，且每条 `fixture.provenance.route_id` 已进入 `compile_input.route_sources`。
3. 任一检查失败均停止 contract 迁移；不得临时删除约束或跳过异常行。
4. 回滚只移动 active 指针并恢复旧消费者；不可修改或删除已经生成的 Snapshot、Release、BundleBuild 和验证资产。

`000005_minimize_control_plane.sql` 已先完成统一 outbox 和兼容镜像；`000008_unified_version_contract.sql` 是其后的 contract 迁移。它只在旧 outbox 全部完成且 provenance 已冻结到 `compile_input.route_sources` 时删除三张旧表。仓库中的迁移演练通过不代表生产已执行；生产仍需备份、恢复演练和门禁查询证据。

### 13.2 对抗性验证补强

本次实现审查主动构造并修复了以下失败场景：

1. 同一 VerificationAsset 重试曾因父 digest 变化生成空内容新快照；现命中当前 VerificationSet 时直接幂等返回。
2. 客户端曾可声明任意 `asset_digest`；现由服务端用冻结 Package 上下文和完整验证结果计算，声明不一致返回 422。
3. preview token 曾只绑定元数据、未绑定输入和结果正文；现按 Agent 同一算法重算输入 digest，并签名完整预览结果，篡改 dataset/value/evidence/raw response 均失败。
4. Bundle 子 Draft 创建与父 Draft stale 曾跨两个事务；现锁父行并在同事务完成子 Build、依赖、outbox 和父状态 CAS。
5. `bundle_input_digest` 曾参与自身哈希，持久化读回后不稳定；现计算时排除结果字段，显式声明必须等于服务端重算值。
6. KBD 内容变化曾继承旧 VerificationSet；现依赖变化使证据 fail closed 失效，Package 返回 `draft_editing`。
7. 编辑与发布曾对不同字段集合计算 Knowledge digest，导致发布误判内容变化；现统一以 `KbdRevision.payload_json` 为知识事实源，Runtime checksum 保持独立。

### 13.3 可观测性

新增 `hci_kbd_package_snapshot_total{result}`、`hci_kbd_package_cas_conflicts_total`、`hci_kbd_verification_asset_attach_total{status}`，并沿用 Bundle 编译、发布、激活指标。digest、Release ID、Bundle ID 和 `trace_id` 写结构化日志与链路，不作为 Prometheus 高基数标签。
