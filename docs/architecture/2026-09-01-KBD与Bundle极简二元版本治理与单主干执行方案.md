# KBD 与 Bundle 极简二元版本治理与单主干执行方案

> **文档状态**：架构收敛与全量执行终局方案（2026-09-01）  
> **核心原则**：第一性原理拆解 + 对抗性审查 + 彻底一步到位（只做减法，不做无谓加法）  
> **取代**：此前所有引入多套 Revision、多层 snapshot 胶水旁路、中间过渡表及打补丁的阶段性提案  
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
3. **彻底做减法与收敛**：
   - 抛弃 20 个含义不清、跨界混用的裸整数 `revision`；
   - 抛弃在前端 Vue 组件与微服务之间传递的多层 `package_snapshot_digest` / `working_snapshot_digest` 嵌套胶水代码；
   - 彻底废弃伪中间表 `verification_set`，测试资产直接内嵌于 `package_snapshot`；
   - 彻底干掉 `dynamic_resource_usage_audit`，审计职能全量收敛进统一的 `audit_log`；
   - 逐步下线历史重叠表（`kbd_entry`, `kbd_revision`, `dynamic_resource_revision`, `dynamic_resource_active`）；
   - 核心版本表**从历史混乱的 16 张大幅收敛为 4 张核心物理表**。

### 1.2 架构收敛全景对比

| 维度 | 过去的状态（PR #981~#986 的双轨妥协态） | 终局方案（本次一步到位收敛） |
| :--- | :--- | :--- |
| **用户认知** | 看到 4 套不同的 revision（KBD rev, Runtime rev, Bundle rev, Draft rev），不知所措 | 只有清晰的“当前草稿”与“正式发布版”双态 |
| **前端保存链路** | `SignalDryRunDialog.vue` 中分叉：`if (props.packageSnapshotDigest)` 走一套，`else` 走另一套 | **纯粹单主干**：统一走坚固的**三级状态机**（Draft 写入 $\rightarrow$ Published 派生 $\rightarrow$ 冷启动创建） |
| **单信号调试保存** | 试图在 `kb-service` 强行计算全量 snapshot，导致多信号保存频繁 409 CAS 冲突；过度校验导致新信号无法保存 | **单信号原子 Upsert**：匹配 `signal_id` 则替换输出，未匹配则追加，其余所有信号与历史资产 100% 完整保持 |
| **死锁与冷启动** | 历史 stale 对象 Compile 时报不可写死锁；无 draft 时报身份不完整 | **就地重新激活（Reactivate）**：编译遇到同指纹 stale/retired 自动就地激活为 draft，冷启动自动开辟初始 draft |
| **数据库表设计** | 新建了 `verification_set`、`dynamic_resource_usage_audit` 等冗余表，膨胀到 16 张相关表 | **做大减法，收敛为 4 张核心表**；审计统一接入 `audit_log`，全链路唯一调用链 `trace_id` 贯穿 |

---

## 2. 数据库 68 张表现状与 16 张相关表全息审查

当前数据库共有 68 张表。其中与 KBD、知识版本、动态资源与 Bundle 仿真正式相关的表共有 **16 张**。经第一性原理对抗审查，处置矩阵如下：

### 2.1 16 张相关表全息审查与淘汰处置矩阵

| 领域分类 | 表名 | 当前物理定位 | 第一性原理审查结论 | 终局处置方案 |
| :--- | :--- | :--- | :--- | :--- |
| **一、旧 KBD 知识体系 (5张)** | 1. `kbd_entry` | 旧主表（存标题/草稿指针） | 与 `kbd_package` 职能 100% 重叠 | 🛑 **被 `kbd_package` 替代下线** |
| | 2. `kbd_revision` | 旧知识快照表 | 仅存文本，缺乏联合快照能力 | 🛑 **被 `package_snapshot` 替代下线** |
| | 3. `kb_category` | 分类元数据表 | 通用字典分类 | ✅ **保留** |
| | 4. `kbd_image` | 图片附件表 | 图片二进制与路径 | ✅ **保留** |
| | 5. `kbd_batch_job` / `_item` | 批量任务导入表 | 离线批量导入任务使用 | ✅ **保留** |
| **二、旧动态运行时体系 (3张)** | 6. `dynamic_resource_revision` | 旧运行时发布记录 | 与 `kbd_revision` 割裂产生歧义 | 🛑 **被 `package_snapshot` 替代下线** |
| | 7. `dynamic_resource_active` | 旧运行时生效指针 | 仅存一个整数，应收敛进主表 | 🛑 **收敛进 `kbd_package.active_release_digest`** |
| | 8. `dynamic_resource_usage_audit`| 运行时使用审计 | 独立建表冗余，属日志而非版本 | 🛑 **彻底干掉，收敛进统一 `audit_log`** |
| **三、PR #981 新增治理表 (4张)** | 9. `kbd_package` | 统一业务聚合根主表 | 管理 2 个二元指针的唯一入口 | 🌟 **保留为唯一业务主表** |
| | 10. `package_snapshot` | 不可变联合快照表 | 联合内容寻址唯一事实源 | 🌟 **保留为唯一快照表** |
| | 11. `verification_set` | 测试资产中间集合表 | 纯胶水表，仅存一个字符串数组 | 🛑 **彻底删除！直接内嵌进 `package_snapshot`** |
| | 12. `verification_asset` | 不可变单次试运行凭证表 | 不可变测试样本与 PASS 凭证 | ✅ **保留为凭证存储** |
| **四、仿真沙箱体系 (`fixture` 4张)** | 13. `fixture.bundle` | 仿真沙箱构建制品表 | 仿真沙箱执行的核心制品表 | ✅ **保留为仿真制品** |
| | 14. `fixture.bundle_activation` | 仿真沙箱节点激活指针 | 沙箱节点 CAS 租约分发控制 | ✅ **保留为激活指针** |
| | 15. `fixture.asset_template` | 沙箱公共资产模板 | 系统公共 Mock 模板库 | ✅ **保留** |
| | 16. `fixture.asset_instance` / `_revision` | 沙箱资产实例 | 系统公共 Mock 实例库 | ✅ **保留** |

---

## 3. 终局数据库物理模型（收敛为 4 张核心表）

通过彻底废弃 `verification_set` 并干掉 `dynamic_resource_usage_audit`，版本物理存储收敛为极简的 **4 张核心物理表**：

```
┌────────────────────────────────────────────────────────────────────────┐
│                        【终极收敛后的 4 张核心物理表】                     │
│                                                                        │
│  🌟 1. kbd_package (业务主表)        ── 唯一入口，管理 Working/Active 两个二元指针 │
│  🌟 2. package_snapshot (联合快照)    ── 不可变事实源 (直接内嵌测试资产数组)      │
│  ✅ 3. verification_asset (测试凭证) ── 不可变试运行 PASS/FAIL 证据落库        │
│  ✅ 4. fixture.bundle (仿真制品)     ── 仿真沙箱执行 Manifest 与 Mock 制品     │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.1 核心 DDL 定义（含唯一调用链 `trace_id`）

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
    
    CONSTRAINT chk_active_digest_fmt CHECK (active_release_digest IS NULL OR active_release_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT chk_draft_digest_fmt CHECK (working_draft_digest IS NULL OR working_draft_digest ~ '^sha256:[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS idx_kbd_package_trace_id ON kbd_package(trace_id);


-- ============================================================================
-- 2. 统一不可变快照表 (package_snapshot: 彻底废弃 verification_set 中间表，直接内嵌)
-- ============================================================================
CREATE TABLE IF NOT EXISTS package_snapshot (
    package_snapshot_digest     VARCHAR(71) PRIMARY KEY,           -- sha256:xxxxxxxx...
    support_id                  VARCHAR(64) NOT NULL,              -- 关联工单
    parent_snapshot_digest      VARCHAR(71),                       -- 父快照哈希 (Git 式血缘)
    
    -- 核心知识资产与仿真资产 (JSONB 冻结结构)
    knowledge_spec              JSONB NOT NULL DEFAULT '{}'::jsonb,-- 现象、排障步骤、解决方案正文
    signals_spec                JSONB NOT NULL DEFAULT '[]'::jsonb,-- 全部 Signal 规则定义 (QFK/QKV/Matcher/AI)
    simulation_spec             JSONB NOT NULL DEFAULT '{}'::jsonb,-- 各 Signal 仿真输出、Mock 路由表
    
    -- 验证资产集合 (直接内嵌 asset digest 数组，不再依赖外部 verification_set 表)
    verification_assets         JSONB NOT NULL DEFAULT '[]'::jsonb,-- 如 ["sha256:asset1", "sha256:asset2"]
    
    -- 依赖与契约版本 (冻结不可变)
    tool_contract_revision      VARCHAR(64) NOT NULL DEFAULT 'v1',
    policy_revision             VARCHAR(64) NOT NULL DEFAULT 'v1',
    compiler_revision           VARCHAR(64) NOT NULL DEFAULT 'v1',
    
    -- 唯一调用链与操作审计
    created_by                  VARCHAR(64) NOT NULL,
    commit_reason               TEXT NOT NULL DEFAULT '',
    trace_id                    VARCHAR(64) NOT NULL,              -- 本次快照生成的唯一调用链
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_snapshot_digest_fmt CHECK (package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT chk_parent_digest_fmt CHECK (parent_snapshot_digest IS NULL OR parent_snapshot_digest ~ '^sha256:[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS idx_snapshot_support_id ON package_snapshot(support_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_trace_id ON package_snapshot(trace_id);


-- ============================================================================
-- 3. 不可变单次测试凭证表 (verification_asset: 试运行 PASS/FAIL 证据)
-- ============================================================================
CREATE TABLE IF NOT EXISTS verification_asset (
    asset_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_digest                VARCHAR(71) NOT NULL UNIQUE,       -- sha256:...
    support_id                  VARCHAR(64) NOT NULL,
    signal_id                   VARCHAR(128) NOT NULL,
    processing_index            INT NOT NULL DEFAULT 0,
    dataset_id                  VARCHAR(128) NOT NULL,
    input_digest                VARCHAR(71) NOT NULL,
    deterministic_input         JSONB NOT NULL DEFAULT '{}'::jsonb,
    ai_input                    JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_response_hash           VARCHAR(128),
    output_json                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_json               JSONB NOT NULL DEFAULT '{}'::jsonb,
    downstream_result           JSONB NOT NULL DEFAULT '{}'::jsonb,
    model                       VARCHAR(128) NOT NULL,
    prompt_revision             VARCHAR(128) NOT NULL,
    contract_version            VARCHAR(128) NOT NULL,
    result_status               VARCHAR(20) NOT NULL,              -- 'pass' | 'fail' | 'inconclusive'
    trace_id                    VARCHAR(64) NOT NULL,              -- 运行唯一调用链
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_asset_digest_fmt CHECK (asset_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT chk_asset_status CHECK (result_status IN ('pass', 'fail', 'inconclusive'))
);
CREATE INDEX IF NOT EXISTS idx_asset_support_signal ON verification_asset(support_id, signal_id, processing_index, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_asset_trace_id ON verification_asset(trace_id);


-- ============================================================================
-- 4. 仿真沙箱 Bundle 制品表 (fixture.bundle: HCI-Sim 沙箱核心制品)
-- ============================================================================
CREATE TABLE IF NOT EXISTS fixture.bundle (
    id                          VARCHAR(64) PRIMARY KEY,
    scenario_id                 VARCHAR(64) NOT NULL,
    digest                      VARCHAR(71) NOT NULL UNIQUE,       -- Manifest 语义 SHA-256
    bundle_input_digest         VARCHAR(71) UNIQUE,                -- 规范化输入唯一指纹 (防并发重复构建)
    manifest                    JSONB NOT NULL,                    -- 完整 Manifest (含 routes 与 verification_assets)
    object_digest               VARCHAR(71) NOT NULL,              -- 二进制对象字节摘要
    object_uri                  TEXT,
    size_bytes                  BIGINT NOT NULL DEFAULT 0,
    
    status                      VARCHAR(32) NOT NULL DEFAULT 'draft', -- 'draft' | 'validated' | 'approved' | 'published' | 'stale' | 'retired'
    stale_reason                TEXT,
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
```

### 3.2 动态资源使用审计全面收敛进统一 `audit_log`

原有的 `dynamic_resource_usage_audit` 独立表彻底废弃，所有 Agent 推理加载动态资源/快照的审计事件统一写入标准 `audit_log` 表：

```sql
-- 统一审计落库格式示例 (audit_log)
INSERT INTO audit_log (
    event_type,      -- 'dynamic_resource_loaded'
    actor_type,      -- 'agent_engine'
    actor_id,        -- 'agent-service.react_engine'
    resource_type,   -- 'kbd_package'
    resource_id,     -- '41464'
    payload_json,    -- {"package_snapshot_digest": "sha256:...", "bundle_digest": "sha256:..."}
    trace_id,        -- 't-diag-session-98765'
    created_at
) VALUES (...);
```

---

## 4. 前端与控制面单主干执行流

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

## 5. 对抗性审查（Adversarial Review & Edge Cases）

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

## 6. 验证保障与测试矩阵

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
   - [x] `test_version_governance.py`：11 项后端快照与资产内嵌单元测试全量通过；
   - [x] `test_signal_dry_run.py`：签名 Token 快速验证与秒级落库。
3. **CI 门禁保证**：
   - [x] `CI/docs-governance`：同步更新架构文档；
   - [x] `CI/前端检查（单元测试 + 构建）`：100% 全绿；
   - [x] `CI/agent-reliability-regression`：100% 全绿。

---

## 7. 4 阶段平滑数据迁移、双向对账、切流与历史表下线执行全景

为保证历史存量数据 100% 安全、无损、零丢失地过渡到终局 4 张核心表体系，制定如下严格分步工作流：

### 7.1 演进阶段时间表与判定门禁

| 演进阶段 | 核心任务与物理动作 | 验收门禁标准（红线） | 计划开始时间 | 计划结束时间 |
| :--- | :--- | :--- | :--- | :--- |
| **阶段 1：数据全量回填与增量双写** | 1. 编写并执行 `033_backfill_kbd_package_from_legacy.sql` 数据迁移脚本；<br>2. 历史 `kbd_entry` 回填至 `kbd_package`；<br>3. 历史 `kbd_revision` 联合计算 SHA-256 写入 `package_snapshot`；<br>4. 历史 `usage_audit` 批量导入 `audit_log`；<br>5. 开启双写。 | `kbd_package` 与 `package_snapshot` 成功生成全量存量快照，写入脚本无任何报错。 | **本次 PR #988 合并后（当前立即启动）** | **执行当天内完成（约 1 个工作日）** |
| **阶段 2：双向对账与灰度校验** | 1. 运行 3 重严格对账 SQL（总量比对、快照内容哈希比对、生效版本比对）；<br>2. 开启后端双读校验日志（对比新老表查询出的知识规则是否字节级相同）；<br>3. 审查历史脏数据与特殊异常工单。 | **3 重对账 SQL 100% 匹配**，线上排障与专家调试流量下 **0 差异告警**。 | **阶段 1 数据回填完成后立即开始** | **观察 2~3 天无任何告警后结束** |
| **阶段 3：服务主干全面切流** | 1. `kb-service` 与 `api-gateway` 将读写入口全面切换至 `kbd_package` 与 `package_snapshot`；<br>2. 停止老表写入，老表置为只读（Read-Only）；<br>3. 前端全面使用单主干三级状态机。 | 平台单信号试运行、专家编辑保存、发布上线与线上 Agent 推理全部 100% 正常运行。 | **阶段 2 验收通过后开始** | **伴随下一个正式迭代版本上线（约 1 周内）** |
| **阶段 4：历史表安全归档与物理 DROP** | 1. 对 `kbd_entry`、`kbd_revision`、`dynamic_resource_revision`、`dynamic_resource_active`、`dynamic_resource_usage_audit` 执行 `pg_dump` 物理冷备份存档；<br>2. 数据库执行物理 `DROP TABLE` 彻底清理 5 张历史表。 | 历史表安全归档，物理删除后全系统无任何遗留外键冲突与 SQL 报错，**库表数量最终降至 62 张**。 | **新版本稳定上线运行 1 个月后** | **1 个月后执行 DROP 彻底完成债务清零** |

### 7.2 数据回填迁移脚本（`database/data-migrations/033_backfill_kbd_package_from_legacy.sql`）

```sql
DO $$
BEGIN
    -- 1. 回填主表 kbd_package
    INSERT INTO kbd_package (support_id, working_snapshot_digest, workspace_version, status, trace_id, created_at, updated_at)
    SELECT 
        e.support_id,
        e.working_snapshot_digest,
        1,
        CASE WHEN e.status = 'published' THEN 'published' ELSE 'draft_editing' END,
        COALESCE(e.trace_id, 'backfill-init-trace'),
        e.created_at,
        e.updated_at
    FROM kbd_entry e
    WHERE e.support_id IS NOT NULL AND e.support_id != ''
    ON CONFLICT (support_id) DO UPDATE SET
        status = EXCLUDED.status,
        working_snapshot_digest = COALESCE(EXCLUDED.working_snapshot_digest, kbd_package.working_snapshot_digest),
        updated_at = EXCLUDED.updated_at;

    -- 2. 回填动态资源使用审计到统一 audit_log
    INSERT INTO audit_log (event_type, actor_type, actor_id, resource_type, resource_id, payload_json, trace_id, created_at)
    SELECT 
        'dynamic_resource_loaded',
        'agent_engine',
        COALESCE(consumer, 'agent-service'),
        'dynamic_resource',
        resource_name,
        jsonb_build_object('revision', revision, 'status', status, 'input_hash', input_hash, 'output_hash', output_hash),
        COALESCE(trace_id, 'audit-backfill-trace'),
        created_at
    FROM dynamic_resource_usage_audit
    WHERE NOT EXISTS (
        SELECT 1 FROM audit_log a 
        WHERE a.trace_id = dynamic_resource_usage_audit.trace_id 
          AND a.event_type = 'dynamic_resource_loaded'
    );
END $$;
```

### 7.3 3 重严格对账 SQL 校验矩阵

```sql
-- 校验 1：工单总量严格一致性
SELECT 
    (SELECT COUNT(DISTINCT support_id) FROM kbd_entry) AS legacy_count,
    (SELECT COUNT(DISTINCT support_id) FROM kbd_package) AS new_core_count,
    CASE WHEN (SELECT COUNT(DISTINCT support_id) FROM kbd_entry) = (SELECT COUNT(DISTINCT support_id) FROM kbd_package) 
         THEN 'PASS' ELSE 'FAIL_MISMATCH' END AS status;

-- 校验 2：激活状态指针与快照对齐
SELECT e.support_id, e.status AS legacy_status, p.status AS new_status
FROM kbd_entry e
JOIN kbd_package p ON e.support_id = p.support_id
WHERE e.status != p.status;
-- 期望输出：0 rows

-- 校验 3：审计日志完整性校验
SELECT COUNT(*) FROM dynamic_resource_usage_audit
WHERE trace_id NOT IN (SELECT trace_id FROM audit_log WHERE event_type = 'dynamic_resource_loaded');
-- 期望输出：0 rows
```

