# Bundle 工厂生成 Draft 幽灵退役状态重激活与列表同步修复方案

## 1. 背景与问题描述

在运维排障平台（`admin-ui`）的 **Bundle 工厂**（`BundleFactoryView.vue`）中，用户在顶部输入框输入 KBD `support_id`（如 `41446`）并点击“+ 生成 Draft”。
系统提示 Toast `Draft 已生成`，并在网络面板成功返回：
- `POST /api/hci-sim/v1/control-plane/bundles` (201 Created)
- `GET /api/hci-sim/v1/control-plane/bundles?support_id=41446` (200 OK)

但在下方的左侧列表中，新生成的 Draft 完全未出现，右侧详情区也无法自动选中它。

---

## 2. 第一性原理分析 (First Principles Analysis)

### 2.1 物理事实与状态机拆解
1. **工作台列表过滤契约**：
   在 `MemoryRegistry.List` 与 PostgreSQL `BundleRegistry.List` 中，为了防止历史被废弃/已归档的 Bundle 污染工作台视图，SQL 查询过滤条件显式包含 `b.status <> 'retired'`。
2. **Compile 幂等命中盲区**：
   当用户之前曾为该 KBD 生成过基础 Draft（`draft_revision = 0`），后因多次修订生成了新版 Draft 并将旧的基础 Draft 置为 `retired` 状态时，数据库 `fixture.bundle` 中保留了该 `input_fingerprint` 对应的 `status = 'retired'` 记录。
   当用户再次在 Bundle 工厂点击“+ 生成 Draft”时，服务端的 `Compile()` 计算相同的 `input_fingerprint`，在数据库中命中了该记录，并直接原样返回了处于 `retired` 状态的 Bundle（HTTP 201）。
3. **级联失效闭环**：
   - 服务端返回了 `status = 'retired'` 的 Bundle；
   - 前端收到 201 触发 `loadBundles(bundle.digest)`；
   - 后端 `List()` 过滤排除 `retired` 记录，返回的列表不包含此 digest；
   - 前端无法在列表中定位与选中，造成“提示生成成功但实际看不见”的幽灵退役现象。

---

## 3. 对抗性审查 (Adversarial Review)

### 3.1 攻击场景与审查结论
- **审查点 1：已归档 Bundle 被重新 Compile 时是否应报错 409 还是恢复？**
  - **红队视角**：已归档的 Bundle 是否应该不可变？如果重新编译，是否会破坏审计追踪？
  - **第一性原理审查**：Bundle 的对象内容（Manifest、Object、Dependencies、Checksum）完全保持不可变。归档（Retire）仅是工作流状态（Status）的生命周期标记。当用户明确请求为该 KBD 输入编译 Draft 时，其意图是获取活跃工作副本。将其状态原子重置为 `draft`，既没有篡改对象内容，又满足了幂等重建需求。
- **审查点 2：内存注册表与数据库注册表一致性**
  - **审查**：`MemoryRegistry` 和 `PostgresRegistry` 必须在遇到已归档记录时执行完全相同的状态恢复逻辑：
    - `status = 'draft'`
    - `stale_reason = NULL`
    - `updated_at = now()`
- **审查点 3：前端幂等键唯一性**
  - **审查**：前端 `createDraft()` 增加时间戳后缀 `bundle-factory-${supportId}-${Date.now()}`，防止短时间连续点击触发不可预期的重放。

---

## 4. 修复落地详情

### 4.1 控制面 Go 运行时 (`hci_sim`)
1. **`hci_sim/internal/controlplane/controlplane.go`**：
   在 `MemoryRegistry.Compile` 中，当命中既有 `input_fingerprint` 或 `digest` 且其状态为 `BundleRetired` 时，自动将其重置为 `BundleDraft` 并清空 `StaleReason`。
2. **`hci_sim/internal/database/bundle_registry.go`**：
   在 PostgreSQL `BundleRegistry.Compile` 事务中，当命中既有指纹且 `status == 'retired'` 时，执行 `UPDATE fixture.bundle SET status = 'draft', stale_reason = NULL, version = version + 1, updated_at = $2 WHERE digest = $1`。
3. **测试覆盖**：
   - 在 `controlplane_test.go` 中添加 `TestRecompileRetiredBundleReactivatesDraft` 单测。
   - 在 `controlplane_api_test.go` 中添加 API 级重激活集成断言。

### 4.2 前端工程 (`frontend/admin`)
- 在 `BundleFactoryView.vue` 中优化 `createDraft()` 的 `Idempotency-Key`，增加时间戳唯一后缀。

---

## 5. 验证结果

1. **Go 运行时测试**：
   `ok hci_sim/cmd/hci-sim 0.174s`
   `ok hci_sim/internal/controlplane 0.148s`
   `ok hci_sim/internal/database 0.009s`
   全部测试套件通过。
2. **前端测试与打包**：
   `11 passed (11 test files), 105 passed (105 tests)`
   `vue-tsc -b && vite build` 生产构建成功。
