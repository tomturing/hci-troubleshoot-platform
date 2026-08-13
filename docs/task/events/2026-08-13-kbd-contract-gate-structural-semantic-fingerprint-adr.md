---
status: active
category: task
audience: backend, ai-agent
last_updated: 2026-08-13
owner: team
related_pr: 待创建（治本实现阶段）
---

# 任务事件：KBD 自动执行契约门禁——结构/语义指纹分级判定（实施计划）

> 本文件为 `solution/events/2026-08-13-kbd-contract-gate-structural-semantic-fingerprint-adr.md` 的
> 任务镜像，描述 HOW（执行清单与完成状态）。事件完成后冻结，永久保留。

---

## 一、目标

将 KBD 自动执行契约门禁从"字节哈希全等硬门槛"改造为"结构/语义指纹分级判定"，
使**向后兼容的 schema 演进（如 #745 新增可选 `metric`）不再误杀存量 KBD**，同时保留对**真破坏性变更**的硬阻断。

---

## 二、实施清单（P0 → P3）

### P0：核心改造

- [ ] **`backend/shared/schemas/signal_generation.py`**
  - [ ] 新增 `_structural_repr(schema)`：递归提取 `required` / `type` / `enum` / `const` / `additionalProperties` / `if-then` 的 required
  - [ ] 新增 `_semantic_repr(schema)`：`_structural_repr` + `descriptions` + `defaults`
  - [ ] 新增 `current_structural_fingerprint()`（递归汇总所有 `*.schema.json`，`lru_cache`）
  - [ ] 新增 `current_semantic_fingerprint()`
  - [ ] 新增 `FP_ALGO_VERSION` 常量并纳入指纹
  - [ ] 保留 `current_tool_contract_revision()` 作为"是否变动"探测（向后兼容）

- [ ] **`backend/scripts/gen-schemas.py`**
  - [ ] 生成时对比上一次提交 schema，调用 `infer_contract_version(old, new)` 自动推断 `(major, minor)`
  - [ ] 将版本写入 `signal.v2.schema.json` 的 `$comment` 或独立 `contract_version.json`
  - [ ] 保持幂等（固定顺序 + `ensure_ascii=False` + `indent=2`）

- [ ] **`backend/kb-service/app/routes/playbooks.py:_execution_issues`**
  - [ ] 用 `stored_struct != cur_struct` 替代原 `tool_contract_revision` 全等判定
  - [ ] 结构不等时实跑 `_validate_signals_against_current(signals)`（当前 schema 校验旧信号）
  - [ ] 校验失败 → 真阻断 issue；通过 → 放行
  - [ ] 结构等但语义不等 → 调用 `emit_soft_stale(support_id, category)`（放行 + 观测，不阻断）
  - [ ] `FP_ALGO_VERSION` 不等 → 走一次性全量重算路径

- [ ] **`backend/shared/observability/metrics.py`**
  - [ ] 新增 `KBD_CONTRACT_SOFT_STALE_TOTAL{support_id, category}`
  - [ ] 新增 `KBD_CONTRACT_HARD_BREAK_TOTAL{support_id, category}`

- [ ] 可观测性埋点：结构化日志携带 `trace_id`，链路追踪到分类查询入口

### P1：单测（接 `test_playbooks.py` 范式）

- [ ] 兼容变更（仅新增可选属性）→ `executable=True` 且 `soft_stale` 埋点
- [ ] 破坏性变更（删 required 字段 / 删 enum 成员）→ 校验失败 → `executable=False` + `hard_break` 埋点
- [ ] 语义漂移（description/defaults 变化，结构不变）→ `executable=True` + `soft_stale` 埋点
- [ ] `FP_ALGO_VERSION` 变更 → 触发全量重算，不误判过期

### P2：存量止血（符合 024 铁律）

- [ ] 经 maintenance 工作稿对 7 条"结构校验通过"的 KBD 批量刷新
      `structural_revision` / `semantic_revision` 到当前值（**走重新发布，禁 SQL 写死 hash**）

### P3：文档同步（文档门禁）

- [ ] 更新契约演进规范（`docs/contracts/` 或 `docs/solution/` 相关主干）
- [ ] 更新 `AGENTS.md`（若涉及维护约定）

---

## 三、完成标准

1. P0 合入后，本地库 7 条 KBD 在 #745 现状下**不再因契约过期被整体判 `executable=False`**；
2. 真破坏性 schema 变更仍能被门禁硬阻断并触发 `KBD_CONTRACT_HARD_BREAK_TOTAL`；
3. 兼容/语义漂移变更触发 `KBD_CONTRACT_SOFT_STALE_TOTAL`，可观测、不阻断；
4. 单测全绿；
5. 文档门禁通过（P0 同一 PR 含本 ADR + P3 规范更新）。

---

## 四、风险与对策

| 风险 | 对策 |
|---|---|
| 指纹算法升级导致存量误判全员 breaking | `FP_ALGO_VERSION` 版本化，触发一次性全量重算 |
| 实跑 jsonschema 校验拖慢热路径 | 仅 `stored_struct != cur_struct` 触发；结果按 `(schema_version, signals_hash)` 缓存 |
| 兼容判定放行引入静默语义错误 | 仅结构兼容放行；语义漂移走 `soft_stale` 告警；真 breaking 仍硬阻断 |
| 存量 7 条仍显示 stale 噪音 | P2 批量刷新快照（走重新发布，符合 024 铁律） |

---

## 五、关联

- 方案 ADR：`solution/events/2026-08-13-kbd-contract-gate-structural-semantic-fingerprint-adr.md`
- 诊断门禁代码：`backend/kb-service/app/routes/playbooks.py:_execution_issues`
- 契约指纹代码：`backend/shared/schemas/signal_generation.py`
- 单一来源生成器：`backend/scripts/gen-schemas.py`
- 数据迁移铁律：`database/data-migrations/024_fix_kbd_23821_scope_and_matcher.sql`
