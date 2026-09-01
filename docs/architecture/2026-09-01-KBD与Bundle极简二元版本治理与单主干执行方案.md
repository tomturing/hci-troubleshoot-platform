# KBD 与 Bundle 极简二元版本治理与单主干执行方案

> **文档状态**：架构收敛与全量执行终局方案（2026-09-01）  
> **核心原则**：第一性原理拆解 + 对抗性审查 + 彻底一步到位  
> **取代**：此前所有引入多套 Revision、多层 snapshot 胶水旁路及打补丁的阶段性提案  
> **适用范围**：KBD 知识管理、Signal 试运行、仿真沙箱（hci-sim）、验证资产沉淀、数据库物理设计与 Runtime 激活  

---

## 1. 执行摘要与核心定论（Executive Summary）

### 1.1 核心定论
经过第一性原理推导与对抗性审查，彻底厘清：
1. **业务与用户心智层面**：真正需要的有且仅有 **2 个业务指针**：
   - **`🟡 Working Draft`（当前正在维护调试的工作草稿）**
   - **`🟢 Active Published`（当前线上生产生效的正式发布版）**
2. **底层物理实现层面**：真正需要的有且仅有 **1 种身份体系**：
   - **基于 SHA-256 的不可变内容寻址（Content-Addressed Immutable Snapshot）**。
3. **彻底抛弃**：
   - 抛弃 20 个含义不清、跨界混用的裸整数 `revision`；
   - 抛弃在前端 Vue 组件与微服务之间传递的多层 `package_snapshot_digest` / `working_snapshot_digest` 嵌套胶水代码；
   - 抛弃为单信号调试强加的全局 CAS 大事务（彻底根除 409 CAS Conflict）。

### 1.2 架构收敛全景对比

| 维度 | 过去的状态（PR #981~#986 的双轨妥协态） | 终局方案（本次一步到位收敛） |
| :--- | :--- | :--- |
| **用户认知** | 看到 4 套不同的 revision（KBD rev, Runtime rev, Bundle rev, Draft rev），不知所措 | 只有清晰的“当前草稿”与“正式发布版”双态 |
| **前端保存链路** | `SignalDryRunDialog.vue` 中分叉：`if (props.packageSnapshotDigest)` 走一套，`else` 走另一套 | **纯粹单主干**：统一走坚固的**三级状态机**（Draft 写入 $\rightarrow$ Published 派生 $\rightarrow$ 冷启动创建） |
| **单信号调试保存** | 试图在 `kb-service` 强行计算全量 snapshot，导致多信号保存频繁 409 CAS 冲突；过度校验导致新信号无法保存 | **单信号原子 Upsert**：匹配 `signal_id` 则替换输出，未匹配则追加，其余所有信号与历史资产 100% 完整保持 |
| **死锁与冷启动** | 历史 stale 对象 Compile 时报不可写死锁；无 draft 时报身份不完整 | **就地重新激活（Reactivate）**：编译遇到同指纹 stale/retired 自动就地激活为 draft，冷启动自动开辟初始 draft |
| **数据库追溯** | 散落在多个 revision 表，缺乏统一调用链关联 | **全表唯一调用链 `trace_id`** 贯穿知识、仿真、测试资产与激活指针 |

---

## 2. 第一性原理推导（First Principles Foundation）

### 2.1 实体与同源性事实
* **事实 1（KBD 规则与 Bundle 仿真是同源制品的两面）**：
  KBD 规则（现象、排障步骤、Matcher、Signal 提取规则）是业务代码，Bundle 仿真资产（Mock 输出、Route Stdout、验证凭证）是对应的测试基准。在物理上，它们同属于一个排障知识包，**在发布时共同冻结，在调试时共同演进**。
* **事实 2（物理不可变快照与业务可变指针）**：
  线上生产生效的内容必须只读（不可变快照），专家调试必须在沙箱中进行（工作区草稿）。
  因此：系统只需要维护指向不可变快照的 2 个可变指针：
  $$\text{KbdPackage} = \{ \text{support\_id}, \text{working\_draft\_digest}, \text{active\_release\_digest} \}$$
* **事实 3（单信号渐进式调试物理法则）**：
  专家调试单信号 $sig_i$ 并点击保存时，系统的唯一物理动作是：
  $$\text{Manifest}_{new} = \text{Manifest}_{parent} \oplus \{ \text{Route}(sig_i) \leftarrow \text{Stdout}_{new} \} \cup \{ \text{Asset}(sig_i) \}$$
  **绝不破坏、绝不覆盖父快照中的其余信号和测试资产**。

---

## 3. 终局架构与单主干执行流

```mermaid
flowchart TD
    subgraph 用户交互层 (Vue Front-End)
        A[专家完成单信号试运行并获得 PASS] --> B[点击「保存到 Bundle 草稿」]
        B --> C{前端单主干三级状态机寻址}
        C -- 分支 1: 有活跃 Draft --> D1[锁定 targetDigest = draft.digest]
        C -- 分支 2: 无 Draft 有 Published --> D2[锁定 targetDigest = published.digest]
        C -- 分支 3: 无 Draft 无 Published --> D3[调用 POST /control-plane/bundles 初始化 Draft<br/>锁定 targetDigest = new_draft.digest]
    end

    subgraph API 网关层 (API Gateway)
        D1 --> E["POST /api/v1/signals/dry-run/bundles/{targetDigest}"]
        D2 --> E
        D3 --> E
        E --> F[签名 Token 校验: 命中则秒级落库，避免重复调用 LLM]
    end

    subgraph 控制面与数据层 (HCI-Sim Control Plane & DB)
        F --> G[控制面 appendVerificationAsset]
        G --> H1[深拷贝父 Manifest 保持其余信号 100% 不变]
        H1 --> H2[更新/追加当前 signal_id 的 Route Stdout]
        H2 --> H3[追加 VerificationAsset 并重算 SHA-256]
        H3 --> H4[原子执行 ReviseDraft 生成新 Draft<br/>若命中历史 stale 对象则就地重新激活为 draft]
    end

    H4 --> I([保存成功 · 前端刷新数据集并提示用户])
```

---

## 4. 数据库物理模型与唯一调用链设计（Database Schema & Traceability）

按照全局约束：**所有数据库表设计必须包含唯一调用链（`trace_id`），支持全链路穿透追溯。**

### 4.1 核心数据表设计 DDL

```sql
-- ============================================================================
-- 1. 排障知识包聚合根表 (业务主表，管理二元指针)
-- ============================================================================
CREATE TABLE IF NOT EXISTS kbd_package (
    support_id                  VARCHAR(64) PRIMARY KEY,           -- 业务工单标识 (如 '41464')
    title                       TEXT NOT NULL DEFAULT '',          -- 故障标题
    status                      VARCHAR(32) NOT NULL DEFAULT 'published', -- 'published' | 'draft_editing' | 'validating'
    
    -- 业务指针 1：当前线上生产生效的正式发布版指纹 (只读)
    active_release_digest       VARCHAR(71),                       -- sha256:xxxxxxxx...
    active_release_version      VARCHAR(32) DEFAULT 'v1.0.0',      -- 语义化版本展示号
    
    -- 业务指针 2：当前正在维护调试的工作草稿指纹 (可写，未维护时为 NULL)
    working_draft_digest        VARCHAR(71),                       -- sha256:yyyyyyyy...
    workspace_version           INT NOT NULL DEFAULT 1,            -- 工作区自增代数
    
    -- 唯一调用链与审计
    trace_id                    VARCHAR(64) NOT NULL,              -- 触发最后一次变更的唯一调用链
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- 约束
    CONSTRAINT chk_active_digest_fmt CHECK (active_release_digest IS NULL OR active_release_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT chk_draft_digest_fmt CHECK (working_draft_digest IS NULL OR working_draft_digest ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_kbd_package_trace_id ON kbd_package(trace_id);


-- ============================================================================
-- 2. 统一不可变快照表 (Content-Addressed Immutable Snapshot)
-- ============================================================================
CREATE TABLE IF NOT EXISTS kbd_package_snapshot (
    package_snapshot_digest     VARCHAR(71) PRIMARY KEY,           -- sha256:xxxxxxxx...
    support_id                  VARCHAR(64) NOT NULL,              -- 关联工单
    parent_snapshot_digest      VARCHAR(71),                       -- 父快照哈希 (Git 式血缘)
    
    -- 核心知识资产 (JSONB 冻结结构)
    knowledge_spec              JSONB NOT NULL DEFAULT '{}'::jsonb,-- 现象、排障步骤、解决方案正文
    signals_spec                JSONB NOT NULL DEFAULT '[]'::jsonb,-- 全部 Signal 规则定义 (QFK/QKV/Matcher/AI)
    
    -- 核心仿真资产
    simulation_spec             JSONB NOT NULL DEFAULT '{}'::jsonb,-- 各 Signal 仿真输出、Mock 路由表
    verification_assets         JSONB NOT NULL DEFAULT '[]'::jsonb,-- 测试样本库与 PASS 断言凭证集
    
    -- 依赖与契约版本 (冻结不可变)
    tool_contract_revision      VARCHAR(64) NOT NULL DEFAULT 'v1', -- 工具协议版本
    policy_revision             VARCHAR(64) NOT NULL DEFAULT 'v1', -- 策略规则版本
    compiler_revision           VARCHAR(64) NOT NULL DEFAULT 'v1', -- 编译器版本
    
    -- 唯一调用链与操作审计
    created_by                  VARCHAR(64) NOT NULL,              -- 操作人 / Actor ID
    commit_reason               TEXT NOT NULL DEFAULT '',          -- 演进修改原因
    trace_id                    VARCHAR(64) NOT NULL,              -- 本次生成的唯一调用链
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_snapshot_digest_fmt CHECK (package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT chk_parent_digest_fmt CHECK (parent_snapshot_digest IS NULL OR parent_snapshot_digest ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_snapshot_support_id ON kbd_package_snapshot(support_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_trace_id ON kbd_package_snapshot(trace_id);


-- ============================================================================
-- 3. 仿真沙箱 Bundle 存储表 (HCI-Sim 核心制品表)
-- ============================================================================
CREATE TABLE IF NOT EXISTS fixture.bundle (
    id                          VARCHAR(64) PRIMARY KEY,           -- 内部唯一 ID
    scenario_id                 VARCHAR(64) NOT NULL,              -- 关联场景 ID
    digest                      VARCHAR(71) NOT NULL UNIQUE,       -- Manifest 语义 SHA-256
    bundle_input_digest         VARCHAR(71) UNIQUE,                -- 规范化输入唯一指纹 (防并发重复构建)
    manifest                    JSONB NOT NULL,                    -- 完整 Manifest (含 routes 与 verification_assets)
    object_digest               VARCHAR(71) NOT NULL,              -- 二进制对象字节摘要
    object_uri                  TEXT,                              -- 对象存储存储路径
    size_bytes                  BIGINT NOT NULL DEFAULT 0,
    
    -- 状态机
    status                      VARCHAR(32) NOT NULL DEFAULT 'draft', -- 'draft' | 'validated' | 'approved' | 'published' | 'stale' | 'retired'
    stale_reason                TEXT,                              -- 如 'superseded_by_revision:sha256:...'
    version                     INT NOT NULL DEFAULT 1,            -- 乐观锁版本
    
    -- 唯一调用链与审计
    created_by                  VARCHAR(64) NOT NULL,
    trace_id                    VARCHAR(64) NOT NULL,              -- 构建调用的唯一调用链
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_bundle_digest_fmt CHECK (digest ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_bundle_status ON fixture.bundle(status);
CREATE INDEX IF NOT EXISTS idx_bundle_trace_id ON fixture.bundle(trace_id);


-- ============================================================================
-- 4. 仿真运行时激活指针表 (Runtime Active Pointer)
-- ============================================================================
CREATE TABLE IF NOT EXISTS fixture.bundle_activation (
    support_id                  VARCHAR(64) PRIMARY KEY,           -- 业务工单标识
    active_digest               VARCHAR(71) REFERENCES fixture.bundle(digest), -- 当前生效 Bundle
    desired_digest              VARCHAR(71),                       -- 期望生效 Bundle (支持灰度/异步)
    generation                  BIGINT NOT NULL DEFAULT 1,         -- 代数指针 (CAS 控制)
    status                      VARCHAR(32) NOT NULL DEFAULT 'active',
    trace_id                    VARCHAR(64) NOT NULL,              -- 激活切换的唯一调用链
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_activation_active_fmt CHECK (active_digest IS NULL OR active_digest ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_activation_trace_id ON fixture.bundle_activation(trace_id);
```

### 4.2 全链路调用链穿透图（End-to-End Trace Lineage）

通过在每一张表与接口中强制落库 `trace_id`，实现**从用户操作 $\rightarrow$ 网关请求 $\rightarrow$ 控制面编译 $\rightarrow$ 沙箱执行 $\rightarrow$ 数据库沉淀**的 100% 唯一调用链可观测闭环：

```
[前端请求 Trace ID: t-dryrun-41464-001]
       │
       ▼
[API Gateway: /signals/dry-run/bundles/{digest}] ──记录日志 [trace_id=t-dryrun-41464-001]
       │
       ▼
[HCI-Sim Control Plane: ReviseDraft] ────────────写入 fixture.bundle (trace_id=t-dryrun-41464-001)
       │
       ▼
[KBD Package Pointer Update] ────────────────────写入 kbd_package (trace_id=t-dryrun-41464-001)
```

---

## 5. 详细技术改造与代码落地清单

### 5.1 前端单主干收敛与胶水代码彻底清理

#### ① [`frontend/admin/src/components/editors/SignalDryRunDialog.vue`](file:///aihci/hci-troubleshoot-platform-fix-bundle-draft-derive-and-compile-deadlock/frontend/admin/src/components/editors/SignalDryRunDialog.vue)
- **彻底删除**：删除 `props.packageSnapshotDigest` 属性声明及其对应的 `if (props.packageSnapshotDigest)` 分叉代码；
- **彻底删除**：删除底部 footer 提示中关于 packageSnapshotDigest 的双模文案判断；
- **全面收敛**：统一采用坚固的三级状态机（Draft -> Published -> Create）；
- **防并发与边界加固**：
  - 维持 `saveLoading` 互斥遮罩，避免连续连点引发覆盖；
  - 维持对 `route_sources` 预先包含限制的移除，支持新建信号即调即存。

#### ② [`frontend/admin/src/views/KbdReviewView.vue`](file:///aihci/hci-troubleshoot-platform-fix-bundle-draft-derive-and-compile-deadlock/frontend/admin/src/views/KbdReviewView.vue)
- **彻底删除**：删除向 `SignalDryRunDialog` 传递 `:package-snapshot-digest` 的属性绑定；
- **彻底删除**：删除 `handleVerificationAssetSaved` 中复杂的 `snapshot.package_snapshot_digest` 嵌套解构与 CAS 回退逻辑；
- **极简维护**：条目加载后直接读取当前 `support_id` 与 `kbdRevision`，只负责向弹窗注入基础上下文。

---

### 5.2 API 网关与后端控制面收敛

#### ① [`backend/api-gateway/app/routes/signal_dry_run.py`](file:///aihci/hci-troubleshoot-platform-fix-bundle-draft-derive-and-compile-deadlock/backend/api-gateway/app/routes/signal_dry_run.py)
- **唯一保存端点**：确立 `POST /signals/dry-run/bundles/{bundle_digest}` 作为试运行保存沉淀的唯一入口；
- **秒级保存保障**：维持 `_verify_preview_token` 签名快速校验，避免保存时再次重复调用耗时的大模型推理；
- **清理多余旁路**：停用/标记废弃旧的 `POST /signals/dry-run/verification-assets` 全量 CAS 旁路接口。

#### ② [`hci_sim/cmd/hci-sim/controlplane_api.go`](file:///aihci/hci-troubleshoot-platform-fix-bundle-draft-derive-and-compile-deadlock/hci_sim/cmd/hci-sim/controlplane_api.go) & [`hci_sim/internal/controlplane/controlplane.go`](file:///aihci/hci-troubleshoot-platform-fix-bundle-draft-derive-and-compile-deadlock/hci_sim/internal/controlplane/controlplane.go)
- **单信号 Stdout Upsert 算法**：
  ```go
  func updateRouteStdout(manifest *fixture.Manifest, signalID string, routeID string, stdout string) (string, error) {
      for i := range manifest.Routes {
          if (routeID != "" && manifest.Routes[i].ID == routeID) || (signalID != "" && manifest.Routes[i].SignalID == signalID) {
              manifest.Routes[i].Result.Stdout = stdout
              return manifest.Routes[i].ID, nil
          }
      }
      // 未匹配则追加新 Route，保障新增信号即调即存
      newID := fmt.Sprintf("route-%s", signalID)
      manifest.Routes = append(manifest.Routes, fixture.Route{
          ID: newID, SignalID: signalID, Result: fixture.ExecutionResult{Stdout: stdout, ExitCode: 0},
      })
      return newID, nil
  }
  ```
- **Stale/Retired 就地激活防死锁**：维持 `Compile` 时当检测到相同指纹对象处于 `stale`/`retired` 时直接更新为 `draft`，消灭死锁。

---

## 6. 对抗性审查（Adversarial Review & Edge Cases）

| 极端场景 | 攻击推演 / 失败条件 | 终局方案防御机制 |
| :--- | :--- | :--- |
| **场景 1：全新信号首次调试保存** | Draft 中此前从未有过该 Signal，若系统做严格校验会报 404 或误判无 Draft。 | **Upsert 算法**：未找到历史 Route 时自动构造 `route-<signal_id>` 追加进 Manifest，平滑支持新信号。 |
| **场景 2：0 发布版本的全新条目（冷启动）** | 既没有 Draft 也没有 Published Bundle。 | **三级状态机分支 3**：自动调用 `POST /control-plane/bundles` 完成首个 Draft 初始化。 |
| **场景 3：已发布条目的日常维护调试** | 当前处于生产生效态，无维护草稿。 | **三级状态机分支 2**：以 Published Bundle 为基线调用 `appendVerificationAsset`，自动派生新 Draft，继承所有历史信号。 |
| **场景 4：多信号连续调试保存** | 专家依次调通 `sig_001`、`sig_002`、`sig_003` 并连续保存。 | **深拷贝 + 原子演进**：每次保存都在上一次生成的 Draft 基础上只替换当前信号，三者完整累加，无 409 CAS 冲突。 |
| **场景 5：快速手速连点保存** | 极短时间内多次触发保存请求。 | **前端遮罩 + 幂等 Key**：`saveLoading` 禁用二次点击；后端基于 `Idempotency-Key` 与签名 Token 防重复落库。 |
| **场景 6：反复调试产生大量 Stale 快照** | 产生大量历史 stale 对象，再次生成相同指纹。 | **控制面就地激活**：`UPDATE fixture.bundle SET status = 'draft'` 重新激活为可用草稿，彻底杜绝死锁。 |
| **场景 7：生产环境安全防护** | 专家反复保存草稿，是否会污染线上生产？ | **强门禁隔离**：保存仅生成 `draft` 状态对象，只有点击“发布”并通过校验审批后才移动 `Active` 生产指针。 |

---

## 7. 验证保障与测试矩阵

1. **前端单测矩阵（`SignalDryRunDialog.spec.ts`）**：
   - [x] 显示已绑定的 KBD 与 Signal，不提供编辑位置选择；
   - [x] AI 范围开启与禁用条件断言；
   - [x] 结果展示单一终态并可展开原始审计响应；
   - [x] 从已发布 Bundle 载入只读预览并流转 Fork 编辑状态机；
   - [x] 当前版本无任何 Draft 与 Published Bundle 时的冷启动创建；
   - [x] 存在匹配草稿时直接复用已有 Draft 并写入；
   - [x] 无草稿但有已发布版时自动基于发布版派生新草稿；
   - [x] 正式发布版条目正常发起试运行；
   - [x] 向已有草稿保存全新 Signal 正确复用与追加。
2. **后端与控制面集成测试**：
   - [x] `controlplane_api_test.go`：QKV / QFK 单信号保存定向更新 Route Stdout；
   - [x] `controlplane_test.go`：Stale / Retired 重新激活为 Draft 防死锁；
   - [x] `test_signal_dry_run.py`：签名 Token 快速验证与秒级落库。
3. **CI 门禁保证**：
   - [x] `CI/docs-governance`：同步更新架构文档；
   - [x] `CI/前端检查（单元测试 + 构建）`：100% 全绿；
   - [x] `CI/agent-reliability-regression`：100% 全绿。
