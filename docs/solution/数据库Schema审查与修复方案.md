# 数据库 Schema 审查与修复方案

> 审查对象：`database/desired_schema.sql`（77 张表、1268 个字段；另有 2 个视图由 `desired_extras.sql` 定义）
> 数据依据：`hci-staging` 集群 `postgres-0` / `hci_troubleshoot` 库实测（只读查询）
> 审查方法：第一性原理（以「数据完整性能否被数据库保证」为唯一判据）+ 对抗性审查（先假定疑点是合理设计，再用数据证伪）
> 审查日期：2026-10-25

---

## 1. 审查范围与方法

### 1.1 判据（第一性原理）

不以「业界惯例」评判，只问三个不可再分的问题：

1. **同一事实是否只存一处？** 若存多处，是否有一处是权威源，其余可由它推导且受约束保护？
2. **跨表引用是否被数据库本身保证？** 若只靠应用层自觉，则随时间必然腐化。
3. **同一概念是否只有一种表示？** 若同一业务键有多种类型/宽度，则无法建立约束，也难以正确关联。

### 1.2 验证手段

- 静态：解析 `CREATE TABLE` 块 + 文件末尾 `ALTER TABLE` 追加段
- 动态：对 staging 库做**只读**核查，包括行数、孤儿数据、类型格式、计数一致性

**解析 `desired_schema.sql` 的四个陷阱**（本次均踩过，都会导致结论失真）：

1. **只解析 `CREATE TABLE` 块**会漏掉文件末尾 23 处 `ALTER TABLE` 追加（9 个后补外键、20 余个后补列）。
2. **列定义可能以逗号开头**（如 `    ,event_id uuid`），按「标识符开头」匹配会整段漏掉。
3. **约束关键词必须用词边界匹配**：用前缀匹配会把 `origin_node_ip`（OR）、`check_verdict`（CHECK）、`excluded_causes`（EXCLUDE）、`primary_hypothesis`（PRIMARY）误当约束行丢弃。
4. **多列 `ALTER` 的第 2 项起没有 `ALTER TABLE` 前缀**（`ALTER TABLE t\n ADD COLUMN a,\n ADD COLUMN b;`），须单独提取。

→ 因此**字段数以实库 `information_schema.columns` 为准**，静态解析仅用于结构分析。

---

## 2. 实测基线

### 2.1 表规模与活跃度（staging）

| 表 | 行数 | 判定 |
|---|---|---|
| `dynamic_resource_usage_audit` | 20957 | 活跃 |
| `bridge_execution_logs` | 16505 | 活跃 |
| `kbd_revision` | 10208 | 活跃 |
| `fact` | 5000 | 活跃 |
| `message` | 4040 | 活跃 |
| `tool_result` | 3677 | 活跃 |
| `kbd_entry` | 2806 | 活跃 |
| `package_snapshot` | 1675 | 活跃 |
| `dynamic_resource_revision` | 706 | 活跃 |
| `case` | 520 | 活跃 |
| `conversation` | 499 | 活跃 |
| `terminal_operation` | 482 | 活跃 |
| `kbd_package` | 432 | 活跃 |
| `sop_document` | 5 | 活跃（数据量小） |
| `kbd_batch_job` | 77 | 活跃 |
| **`verification_asset`** | **0** | **零数据** |
| **`bundle_metadata`** | **0** | **零数据** |
| **`claim_evidence_link`** | **0** | **零数据** |
| **`operation_audit`** | **0** | **零数据（T1/T25 待实施）** |

### 2.2 孤儿数据核查

| 检查项 | 孤儿数 | 结论 |
|---|---|---|
| `message.case_id` → `case` | **0** | ✅ 可安全加外键 |
| `tool_result.case_id` → `case` | **0** | ✅ 可安全加外键 |
| `package_snapshot.support_id` → `kbd_package` | **0** | ✅ 可安全加外键 |
| `kbd_entry.support_id` → `kbd_package` | **2374** | ❌ **语义不同，不可加**（见 4.1） |
| `dynamic_resource_usage_audit.case_id` → `case` | 208 | ⚠️ 审计表有意保留历史 |
| `dynamic_resource_usage_audit.conversation_id` → `conversation` | 178 | ⚠️ 含 `dry-run:` 模拟标识（见 4.2） |

### 2.3 冗余计数一致性

| 检查项 | 不一致行数 |
|---|---|
| `conversation.message_count` vs 实际消息数 | **0** |
| `kbd_batch_job.completed_count` vs 三项之和 | **0** |
| `kbd_batch_job.total_count` vs `requested_kbd_ids` 长度 | **0** |

结论：当前应用层维护是准确的，冗余计数**尚未腐化**，但缺乏数据库层保障。

### 2.4 声明式 Schema 与实库零漂移（正向结论）

| 项 | `desired_schema.sql` | staging 实库 | 结论 |
|---|---|---|---|
| 基表 | 77 | 77 | 一致 |
| 字段 | 1268（含多列 `ALTER` 追加的列） | 1268 | 一致 |
| 主键 | 77 张表全部有主键 | 77 张表全部有主键 | 一致，**无无主键表** |
| 外键 | 92 | 92 | 一致 |
| 视图 | 2（`active_cases`、`case_statistics`） | 2 | 一致，不计入 77 张表 |

**结论**：不存在「实库有、声明无」的列。这一点很关键——若存在，下次 `atlas schema apply --auto-approve` 会自动 `DROP COLUMN`，而本次核查确认该风险为零。

---

## 3. 确认的缺陷

### 3.1 【P0】审核人身份不可追溯

`kbd_entry.reviewer_id`（integer）实测：取值 **1 ~ 95394**，distinct 仅 5 个值。

而可用的身份实体只有两种：
- `kbd_review_owner.id`：bigint，仅 **6 行**
- `user.user_id`：**uuid 主键**，无整数 ID

因此 `reviewer_id = 95394` 既无法指向 `kbd_review_owner`，也无法指向 `user`。函数 `_freeze_approved_expert_revision()` 已在代码中如实记录了这一事实：

```python
identity_status="unverified_body_reviewer_id",
...
"legacy_reviewer_id": reviewer_id,
```

同类字段（staging 实测，非空/总行数）：

| 字段 | 类型 | 非空/总行 | 问题 |
|---|---|---|---|
| `kbd_entry.reviewer_id` | integer | **318 / 2806** | 指向不明，取值 1~95394 无对应实体 |
| `kbd_entry.review_owner_id` | bigint | 124 / 2806 | ✅ 有外键，指向 `kbd_review_owner` |
| `kbd_entry.unpublishable_by` | integer | **0 / 2806** | 结构性无法写入（见 3.2） |
| `sop_document.reviewer_id` | integer | 5 / 5 | 指向不明 |
| `kbd_revision.actor_id` | varchar(128) | **0 / 10208** | 版本历史**完全不记录创建人** |
| `*_audit.actor_user_id`（6 表） | uuid | **非空均为 0** | 底层已就绪但从未启用 |
| `*_audit.actor_id`（5 表） | varchar(128) | — | 无外键 |

其中最具决定性的一条：`dynamic_resource_usage_audit` 有 **20957 行**、`audit_log` 有 **1610 行**、`diagnosis_management_audit` 有 **92 行**，三者的 `actor_user_id` **全部为 NULL**——说明 uuid 形态的身份列虽已建好，但没有任何写入路径在用。

**「操作人」概念在 77 张表中存在 8 种类型表示、约 50 个列**：

| 类型 | 列数 | 典型列 |
|---|---|---|
| `varchar(128)` | 24 | `actor_id`×5、`created_by`×5、`approved_by`×3、`requested_by`×2 … |
| `uuid` | 12 | `actor_user_id`×6、`user_id`×6（唯一能指向 `user` 的表示） |
| `varchar(64)` | 5 | `user_id`（bridge）、`resolved_by`、`archive_deleted_by` … |
| `integer` | 3 | `reviewer_id`×2、`unpublishable_by` |
| `varchar(100)` | 2 | `actor`（authorization）、`authorized_by` |
| `varchar(32)` | 2 | `managed_by`×2 |
| `varchar(255)` | 1 | `created_by`（log_collect_task） |
| `bigint` | 1 | `review_owner_id`（**唯一带外键**） |

这直接违反项目规则「数据库表设计必须使用唯一调用链」：无法跨表回答「谁做了什么」。

### 3.2 【P1】`kbd_entry.unpublishable_by` 是**结构性无法写入**的预留字段

三重证据：
- 数据：staging **0 行非空**
- 写入：`_set_unpublishable_one()`（`admin.py:2884`）的 `UPDATE ... SET` 中**不含该列**，只写 `status` / `unpublishable_at` / `unpublishable_reason`
- 读取：仅 `admin.py:1167`（SELECT）与 `admin.py:1222`（API 响应）；前端 `KbdReviewView.vue:111` 只声明了类型，从未渲染

**为何无法写入（第一性原理）**：该接口的鉴权函数 `_check_auth()`（`admin.py:731`）只比对内部服务 Token，**整条链路不存在任何用户身份来源**；处理器收到的 `request_context` 仅承载 `reason`。因此在「操作人身份」体系统一之前，该列在结构上不可能被填充。

**历史溯源**：由 PR #1090（2026-09-24）以 `ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS unpublishable_by integer` 引入（无默认值 → 存量行均为 NULL），且 `git log -S` 显示该字段在全部历史中**从未出现在任何 UPDATE/INSERT 中**。

因此这是「功能未实现完整」，而不是「冗余字段」：同一功能组的 `unpublishable_at` / `unpublishable_reason` 都在写入，唯独缺少「谁」。处置见 §5 阶段一。

### 3.3 【P1】`support_id` 同名不同义（易误导关联）

| 表 | 语义 | 样本值 | 唯一值数 |
|---|---|---|---|
| `kbd_entry.support_id` | **KBD 条目编号** | `15414`、`15423` | 2806 |
| `kbd_package.support_id` | **知识包编号** | `34343`、`35349` | 432 |

两表按 `support_id` JOIN 能命中 432 行，但这只是**纯数字字符串的巧合**，业务上毫无关系。任何开发者（包括本次审查）都可能据此写出错误的外键或错误查询。

### 3.4 【P2】`case_id` 存在三种宽度

| 宽度 | 表 |
|---|---|
| varchar(20) | `case`、`conversation`、`message`、`session`、`diagnosis_session`、`environment`、`assistant_evaluation`、`tool_result`、`fact`、`claim_evidence_link`、`log_collect_task` |
| varchar(32) | `terminal_operation`、`bridge_execution_logs`、`bridge_execution_artifacts`、`vm_console_capture`、`vm_console_audit_event`、`effect_verification` |
| varchar(64) | `dynamic_resource_usage_audit` |

实测所有表 **0 行超过 20 字符**（`case` 的 CHECK 已限定 `^Q[0-9]{8}[0-9]{5,6}$`，最长 15 字符），因此收窄为 `varchar(20)` 在数据层面安全。

### 3.5 【P2】`exec_id` 存在两种宽度

- `authorization.exec_id` = varchar(36)
- `tool_result` / `bridge_execution_logs` / `bridge_execution_artifacts` / `vm_console_capture` / `dynamic_resource_usage_audit` = varchar(64)

同一工具调用 ID 在两处宽度不同，无法建立统一关联。

### 3.6 【P2】`status` 有 5 种宽度 + 2 个 ENUM（共 7 种类型）

varchar(16)×12、varchar(20)×5、varchar(24)×3、varchar(30)×1、varchar(32)×4，另有 `case_status`、`diagnosis_session_status` 两个 ENUM（均实测确认，另有 `message_role` 属 `role` 列，不计入）。

### 3.7 【P2】`tenant_id` 两种宽度

varchar(128)×14 表 vs **varchar(64)×3**（`effect_verification`、`vm_console_capture`、`vm_console_audit_event`）。

### 3.8 【P3】文档严重滞后

`docs/solution/数据库设计.md` §1.3 标题为「全量表总览（**27 张**）」，实际 **77 张**——50 张表未登记。

### 3.9 【P3】4 张零数据表待确认

`verification_asset`、`bundle_metadata`、`claim_evidence_link`、`operation_audit` 均为 0 行。其中前三张在代码中有引用（非死代码），属「已建未用」；`operation_audit` 已在文档登记为待实施。

`bundle_metadata` 最可疑：11 列、4 处代码引用、无 `trace_id`、`support_id` 无外键、且与 `kbd_package`/`package_snapshot` 概念重叠。

---

## 4. 经数据验证后**推翻**的假设（避免误改）

审查过程中有 3 个初判被真实数据否定，记录在此以防后人重复犯错。

### 4.1 ❌ 「`support_id` 应统一加外键到 `kbd_package`」

初判依据：`kbd_package.support_id` 是 UNIQUE，具备作外键目标的条件。

**证伪**：`kbd_entry.support_id` 有 **2374 行**在 `kbd_package` 中不存在。原因是两者语义不同（KBD 条目号 vs 知识包号）。若按初判加外键，迁移会直接失败，且会把无关数据错误关联。

**修正结论**：只有 `package_snapshot` / `verification_asset` / `bundle_metadata` 的 `support_id` 与 `kbd_package` 同维度（实测 0 孤儿），可加外键；`kbd_entry.support_id` 不可加。

### 4.2 ❌ 「`dynamic_resource_usage_audit.conversation_id varchar(64)` 是类型错误，应改 uuid」

初判依据：`conversation.conversation_id` 是 uuid，而该列是 varchar(64)，导致无法 JOIN 与建外键。

**证伪**：178 个非 UUID 值的真实内容是 `dry-run:0db25ada491a41058ec5973290b8cdcd`（40 字符）。代码显式写入：

```python
# backend/agent-service/app/routes/signal_dry_run.py（4 处）
conversation_id=f"dry-run:{trace_id}",
```

**修正结论**：该列是**有意设计的多态标识**，同时承载真实会话 UUID 与 dry-run 模拟会话标识。`varchar(64)` 正是为容纳 `dry-run:` 前缀。**改为 uuid 会破坏 dry-run 审计功能**，禁止改动。

### 4.3 ❌ 「ORM 字段在数据库不存在，是 schema-代码漂移 bug」

初判依据：`DynamicResourceUsageAudit` 模型定义了 `package_snapshot_digest` 等 4 个字段，而 `CREATE TABLE` 块中无这些列。

**证伪**：这些列由 PR #981 通过 `ALTER TABLE ... ADD COLUMN` 追加在 schema 文件末尾。是**解析器只读了 `CREATE TABLE` 块**，不是代码 bug。

**修正结论**：审查 `desired_schema.sql` 必须同时解析文件末尾的 `ALTER TABLE` 段。

---

## 5. 修复方案

> **统一前提**：`desired_schema.sql` 是 Atlas 声明式权威。任何加外键/加约束的改动，若目标环境存在违反约束的存量数据，会导致 `atlas schema apply` 失败，进而**部署失败**。
> 因此下文每项均标注「前置校验」，必须在所有环境通过后才可合入。

### 阶段一：本次已执行（纯文档，零 DDL）

#### 1. 修正文档表总览与主键描述

`docs/solution/数据库设计.md` §1.3 由 27 张补全为 77 张，并逐表对实库核对主键（修正 **12 处**错误，其中 2 处为原有内容：`fact`、`claim_evidence_link` 实为 `varchar(36)` 而非 uuid）。字段总数标注为 1268。

#### 2. 修正失实的字段说明

`docs/solution/agent/KBD审核责任人与无法发布功能说明.md` 原称 `unpublishable_by` 为「操作人 ID」，与实际（恒为 NULL、无写入路径）不符，已改为如实描述。

#### 3. 清理 10 个冗余索引

用 `pg_stat_user_indexes` 在 staging（统计累积 47 天、从未重置）核查，得到 **229 个 `idx_scan = 0`** 的索引。**但这一证据不足以判死**——决定性反例：`kbd_entry` 共 13 个索引，7 个在用（`kbd_entry_pkey` 19376 次、`idx_kbd_entry_ai_category` 7128 次等），6 个零扫描，而零扫描里包括向量索引 `idx_kbd_entry_embedding`——它 0 次扫描是因为 `embedding` 列当前全为 NULL（功能未启用），不是索引无用。同理 staging 流量不代表生产，管理员/月度功能可能从不在此触发。

改用**与负载无关**的判据：索引的语义是为某个访问路径提供有序结构；若两个索引的**列组合、列顺序、谓词三者完全相同**，则它们能服务的查询集合完全相等，多出的那个是纯机械冗余。

| 类别 | 数量 | 判据 |
|---|---|---|
| 与既有 UNIQUE 约束完全重合 | 9 | 同表、同列、同顺序、同谓词；唯一索引可完全替代同列普通索引 |
| 是另一索引的最左前缀 | 1 | `conversation(case_id)` ⊂ `conversation(case_id, started_at DESC)` |

**收益定性**：索引 354 → 344，释放约 450 KB。空间收益很小；真实价值是消除声明式 Schema 的自我重复（否则后人会误以为两个索引各有用）与减少写入时的索引维护。典型成因是「先为查询建普通索引，后为幂等/去重补上同列 UNIQUE，旧索引无人回收」。

**执行方式**：只删 `desired_schema.sql` 的声明行，由 atlas 自动 `DROP INDEX`（索引可重建、不动数据）；每处删除点保留一行说明，防止重复添加；`deploy/helm/*/init-configmap.yaml` 两份引导脚本同步删除 `idx_conversation_case_id`（该脚本本身也已声明覆盖索引，无窗口期）。`data-migrations/032` 中的历史声明不改（已执行过，改历史文件会破坏 checksum）。已同步在 `数据库设计.md` §8.1 固化「禁止重复索引」原则。

### 阶段一（原计划）：删除 `kbd_entry.unpublishable_by` —— 本次**不执行**

原判为「零风险直接删」，复核后**降级为「需产品决策」**，理由：

1. **删除是不可逆的生产 DDL**。`scripts/db-migrate.sh` Step 3 使用 `atlas schema apply --auto-approve`，且 `atlas.hcl` 未配置破坏性变更拦截 → 从 `desired_schema.sql` 移除该列会在**所有环境（含生产）自动执行 `DROP COLUMN`**。
2. **收益接近零**：该列恒为 NULL，保留不产生任何功能或性能成本，删除只减少一处认知负担。
3. **它承载着未完成的设计意图**：同功能组的 `unpublishable_at` / `unpublishable_reason` 均在写入，唯独缺少「谁」，说明是功能未实现完整。设计文档、前端类型都仍引用它。

**处置**：保留列，改为在文档中如实标注；待「统一操作人身份」（阶段三）落地时，一并改造为 `REFERENCES "user"(user_id)` 并补齐写入。

若后续确认要删，前置校验（须为 0）：

```sql
SELECT count(*) FROM kbd_entry WHERE unpublishable_by IS NOT NULL;
```

改动清单（5 处，缺一即报错）：`desired_schema.sql`（列定义 + `COMMENT ON COLUMN`）、`models/kbd_entry.py:228`、`admin.py:1167`、`admin.py:1222`、`frontend/admin/src/views/KbdReviewView.vue:111`。

### 阶段二：可安全加固（需三环境前置校验通过）

#### 3. 补 `message.case_id` / `tool_result.case_id` 外键

与已在 `fact` / `claim_evidence_link` 上完成的 P0-3/P0-4 修复对齐。

前置校验：

```sql
-- 两项均须返回 0
SELECT count(*) FROM message m LEFT JOIN "case" c ON m.case_id = c.case_id WHERE c.case_id IS NULL;
SELECT count(*) FROM tool_result t LEFT JOIN "case" c ON t.case_id = c.case_id
 WHERE t.case_id IS NOT NULL AND c.case_id IS NULL;
```

改动：

```sql
-- desired_schema.sql 中 message 表约束区追加
CONSTRAINT fk_message_case_id FOREIGN KEY (case_id)
    REFERENCES "case" (case_id) ON DELETE CASCADE,

-- tool_result 表约束区追加
CONSTRAINT fk_tool_result_case_id FOREIGN KEY (case_id)
    REFERENCES "case" (case_id) ON DELETE CASCADE,
```

影响评估：staging `message` 4040 行、`tool_result` 3677 行，加外键耗时极短。需注意 `ON DELETE CASCADE` 会使删除工单时一并清理消息（与 `conversation` 级联链路一致，语义正确）。

#### 4. 补 `package_snapshot.support_id` 外键

前置校验（须返回 0）：

```sql
SELECT count(*) FROM package_snapshot p LEFT JOIN kbd_package k ON k.support_id = p.support_id
 WHERE k.package_id IS NULL;
```

改动：

```sql
CONSTRAINT fk_package_snapshot_support_id FOREIGN KEY (support_id)
    REFERENCES kbd_package (support_id) ON DELETE RESTRICT,
```

`verification_asset` / `bundle_metadata` 同为 0 孤儿且当前 0 行，可一并添加；但若 `bundle_metadata` 确认废弃，优先走阶段四。

### 阶段三：需产品决策

#### 5. 统一「操作人身份」

这是本次审查最根本的问题，但**不能纯技术修复**，因为它需要回答「审核人到底是谁」。

可选路径：

| 方案 | 做法 | 代价 |
|---|---|---|
| A | 所有 actor 统一 `uuid REFERENCES "user"(user_id)` | 需前端/调用方开始传真实用户 ID；管理员需有账号 |
| B | 统一 `bigint REFERENCES kbd_review_owner(id)` | 仅覆盖审核域，非审核操作仍无身份 |
| C | 新增 `actor` 表，所有 actor 外键指向它 | 改动面最大，语义最干净 |

前置事实：`*_audit.actor_user_id`（uuid，已建好）当前**全部 0 行**，说明路径 A 的底座已就绪但尚未使用。

建议顺序：先确认 `reviewer_id` 的 5 个实际取值来源（前端？外部系统？），再决定。

同时需处理：`reviewer_id` 是 5 处 API 的**必填参数**（`Field(..., ge=1)`），删除或改类型会破坏接口契约。

#### 6. `support_id` 改名以消除歧义

建议其一改名：
- `kbd_entry.support_id` → `kbd_case_no`（它其实是案例编号）
- 或 `kbd_package.support_id` → `package_no`

属破坏性变更，需评估全部读写点与前端契约。

#### 7. `case_id` / `exec_id` / `status` / `tenant_id` 类型统一

均为收窄或统一，数据层面已验证安全（0 行超长），但涉及 20 余张表，建议与下一次大版本合并执行，不做零散改动。

### 阶段四：表级清理（需确认后执行）

| 表 | 建议 |
|---|---|
| `bundle_metadata` | 优先确认是否废弃：0 行、无 `trace_id`、`support_id` 无外键、与 `kbd_package`/`package_snapshot` 概念重叠。若确认废弃，与 `kbd_package`/`package_snapshot` 体系合并 |
| `verification_asset` | 0 行但代码有引用，属待启用，**保留** |
| `claim_evidence_link` | 0 行但代码有引用（`fact` 体系配套），**保留** |
| `operation_audit` | 文档已登记待实施，**保留** |

---

## 6. 明确不建议修改的项

| 项 | 理由 |
|---|---|
| `dynamic_resource_usage_audit.conversation_id varchar(64)` | dry-run 多态标识，见 4.2 |
| `dynamic_resource_usage_audit` 中的孤儿数据（178/208 行） | 审计表**有意保留**已删除实体的历史记录，用于合规追溯，不应清理也不应加外键 |
| `message.case_id` / `content_md` / `tsv` / `embedding` | 有意的物化冗余，设计原则已声明，且有索引/触发器支撑 |
| `kbd_batch_job.completed_count` | 虽有 CHECK 保证可由三项之和推导，但实测 0 不一致且查询高频，物化有收益 |
| JSONB 承载的扩展字段 | 设计原则明确「不确定结构用 JSONB」，且多为外部载荷 |
| `desired_extras.sql` 在 Step 2/4 执行两次 | 有意设计：Step 3 atlas apply 可能重建表致触发器丢失 |

---

## 7. 执行顺序与风险提示

1. 阶段一（文档修正）已完成，本次 PR 即为其交付物
2. 阶段一原计划的「删列」已降级，**不属于零风险操作**，不得在无产品决策时执行
3. 阶段二每项**必须在 dev / staging / prod 三个环境分别跑前置校验**，全部返回 0 才可合入
4. 阶段三需要产品/领域负责人决策，不宜由工程单方面推进
5. 阶段四需确认业务是否还需要该能力

**关键风险**：本项目 `desired_schema.sql` 由 Atlas 收敛执行，一旦声明了外键而目标库存在孤儿数据，`atlas schema apply` 会失败并**阻塞整个部署**。因此「前置校验」不是可选项，而是强制门禁。

建议后续增加 CI 检查：在 PR 阶段对 `desired_schema.sql` 新增的外键，自动在测试库校验孤儿数，fail-fast 于合并前而非部署时。
