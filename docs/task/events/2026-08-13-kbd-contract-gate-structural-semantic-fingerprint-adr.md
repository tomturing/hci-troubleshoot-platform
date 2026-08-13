---
status: active
category: solution
audience: backend, architect, ai-agent
last_updated: 2026-08-13
owner: team
related_pr: PR #755（校验器驱动最优解，已落地）
---

# ADR: KBD 自动执行契约门禁——校验器驱动方案

> 本文档为**架构决策记录（ADR）**，记录"契约门禁全有或全无"根因的治本方案设计与决策依据。
> 事件完成后冻结，永久保留，不再修改。

---

## 一、背景与问题陈述

### 1.1 现象

2026-08-13 排查发现：虚拟机-015（虚拟机集群内热迁移失败（跨存储））分类下 KBD 23821 在诊断查询时报
"分类 ... 下的 KBD 均未通过自动执行契约，系统不会生成知识库外的命令或根因。已请求人工支持。"

进一步全库只读统计（本地 `hci_troubleshoot` 库）确认：

| 指标 | 值 |
|---|---|
| 已发布 KBD 总数 | 7 |
| 契约过期（≠ 当前运行时指纹） | 7（**100%**） |
| 无 `publish_validation` 字段 | 0 |

即 **全部 7 条已发布 KBD 的契约快照都滞后于代码**，分布在 6 个分类，并非 23821 单条问题。

### 1.2 根因（第一性原理，落到最底层事实）

门禁判定位于 `backend/kb-service/app/routes/playbooks.py:_execution_issues`：

```python
if pv.get("tool_contract_revision") != current_tool_contract_revision():
    issues.append("专家发布时使用的工具契约版本已过期，必须重新发布")
```

`current_tool_contract_revision()`（`backend/shared/schemas/signal_generation.py`）对
`backend/shared/schemas/signals/` 下**所有 `*.schema.json` 文件字节做 sha256 聚合**。

三个不可再分的事实：

1. **契约指纹 = 文件字节指纹**（非语义指纹）；
2. **判定 = 布尔全等**（无"近似/兼容/区间"中间态）；
3. **快照在发布时冻结，代码持续演进**（GitOps 持续合入）。

必然推论：任一 schema 文件字节变化 → 此前所有 KBD 冻结指纹立即不等 → 全部 `executable=False`。

### 1.3 本次触发的直接变更

commit `01580431`（#745）在 `signal.v2.schema.json` 的 `match.properties` 新增可选字段 `metric`
（`"type": "string"`，**非 required、不在 `additionalProperties` 禁区被旧数据违反**）。
该改动使聚合 hash 从 `cf416e14...` 推进到 `9612e3a9...`，导致 23821 等所有 #745 之前发布的 KBD 过期。

逆向复现（临时回退 #745 后重算 hash）确认：`cf416e14...` 正是 23821 快照记载的 `tool_contract_revision`。
**证明 #745 是唯一诱因，且其为向后兼容的非破坏性变更。**

### 1.4 架构级风险（对抗性审查结论）

门禁用"文件内容指纹"替代"语义兼容性判断"，带来系统性脆弱点：

- **无契约语义版本**：schema 演进无 major/minor 区分，无法区分"破坏性变更"与"兼容性变更"；
- **无降级通道**：hash 不等时无条件拒绝，而非"用新 schema 校验旧信号，通过则放行"；
- **发布与代码脱钩**：KBD 盖章是人工/批处理，代码由 CI 持续合入，两者无自动同步；
- **后果**：每次 signals schema 改动，所有存量诊断能力瞬间归零，直到人工批量重发。

---

## 二、决策：校验器驱动方案（最优解）

### 2.1 第一性原理推导

```
executable 的唯一物理定义 = validate(old_signals, current_schema) 的 pass/fail
```

一切判定都应从这个单一事实出发，不需要任何中间代理层。

### 2.2 方案演进：为何放弃两级指纹

PR #755 初版采用"结构/语义指纹分级判定"：`structural_fingerprint` + `semantic_fingerprint`。

对抗性审查发现两个致命缺陷：

**REDUNDANCY-1（双重校验）**：`_execution_issues` 顶层无条件跑 `validate_publishable_signals_json`（第1次），`_evaluate_contract_revision` 内当 `stored_struct != cur_struct` 时再跑一次（第2次）。Breaking 场景产生两条重复错误信息，且性能无优化（因为顶层就已无条件跑）。

**BUG-2（soft_stale 指标过计数）**：`soft_stale.inc()` 不受外部 `issues` 是否为空的约束，已有其他阻断问题的 KBD 仍被误计入 `soft_stale`，指标语义失真。

**两级指纹的设计意图**（用廉价指纹判断是否需要跑校验器）在顶层无条件校验的架构下归零：校验器已跑，指纹比较仅是额外的 CPU + 复杂 $ref 递归展开，带来 ~220 行代码、3 个已知缺陷、零性能收益。

### 2.3 最优解：三层职责分离

```
层1 - Gating（唯一真相源，决定 executable）
      schema_valid = validate(signals, current_schema)  → 一次调用，结果复用

层2 - Change Detection（变化感知）
      stored_tool_contract_revision != current          → 字节哈希，"有东西变了"

层3 - Observability（基于前两层结果分发）
      变了 + schema_valid + 无其他 issues → soft_stale（兼容漂移）
      变了 + !schema_valid               → hard_break（破坏性变更，issues 已有原因）
```

### 2.4 实现（`_execution_issues`）

```python
# Layer 1：唯一真相源
schema_valid = True
try:
    validate_publishable_signals_json(validation_document)
except Exception as exc:
    schema_valid = False
    schema_issue = str(getattr(exc, "message", exc))

# ... signal 级别诊断（不变）...

# Layer 2 + 3：变化感知 + 可观测性分发
if isinstance(publish_validation, dict) and publish_validation:
    if publish_validation.get("status") != "passed":
        issues.append("专家发布校验状态无效，必须重新发布")
    else:
        stored_rev = publish_validation.get("tool_contract_revision")
        if stored_rev != current_tool_contract_revision():
            if not schema_valid:
                KBD_CONTRACT_HARD_BREAK_TOTAL.labels(...).inc()  # 仅埋点，不重复报错
            elif not issues:
                KBD_CONTRACT_SOFT_STALE_TOTAL.labels(...).inc()  # 无其他问题时才计数
```

**对 #745 的推演**：23821 缺 `metric` 字段，但 `metric` 非 required，用新 schema 校验通过
→ `schema_valid = True` + `stored_rev != current_rev` + `not issues`
→ `soft_stale.inc()` + `executable = True`。兼容变更不再误杀。

### 2.5 与 PR #755 初版的对比

| 维度 | PR #755 初版（两级指纹） | 最优解（校验器驱动） |
|---|---|---|
| 新增代码量 | ~220 行（指纹计算全套） | ~10 行差量 |
| 校验器调用次数 | 2 次（REDUNDANCY-1） | **1 次** |
| EDGE-1（关键词盲区） | 存在（被顶层兜底，结构模糊） | **不存在**（校验器完备覆盖） |
| BUG-2（soft_stale 过计数） | 存在 | **不存在** |
| 错误信息重复 | 存在（breaking 场景两条） | **不存在** |
| 递归 $ref 解析 | 需要 | **不需要** |
| FP_ALGO_VERSION | 需要 | **不需要** |
| 旧快照向下兼容 | ✅ | ✅（tool_contract_revision 原本就在） |
| 可观测性完备性 | 关键词不完备 | 校验器完备（包含 minimum/pattern/format） |

### 2.6 可观测性（遵循全局可观测性规则）

Prometheus 指标（保留）：

- `KBD_CONTRACT_SOFT_STALE_TOTAL{support_id, category}` —— 兼容演进但字节哈希变化（仅在无其他 issues 时计数）；
- `KBD_CONTRACT_HARD_BREAK_TOTAL{support_id, category}` —— 真 breaking，旧信号校验失败。

结构化日志携带 `trace_id`，链路追踪到分类查询入口。

### 2.7 存量止血（符合 024 数据迁移铁律）

024 迁移注释明确：契约 hash 过期**禁止在 SQL 写死 hash**，必须由 KBD 走 maintenance 工作稿重新发布刷新盖章。
治本 PR 合入后，存量 7 条 KBD 经 maintenance 工作稿批量重新发布，刷新 `tool_contract_revision` 到当前值。
由于这些 KBD 均为向后兼容变更（#745 新增可选字段），重发期间暂时走 `soft_stale` 路径（`executable=True`，不阻断）。

---

## 三、为什么不选其他方案（决策依据）

### 方案 X1：每次 schema 改动后批量重发所有 KBD

- **拒绝理由**：仅把当前 hash 写回快照，下次任何 schema 改动又会 100% 过期。是"掩耳盗铃"，
  未触及"门禁用错判据"的根因。

### 方案 X2：直接移除契约门禁（完全不做版本校验）

- **拒绝理由**：若真发生破坏性 schema 变更（如删除 `acquire.tool` 字段），无门禁会让旧 KBD
  在运行时崩溃或产生错误结论。门禁应"精准"而非"取消"。

### 方案 X3：仅用整体 hash + 向后兼容豁免清单（白名单）

- **拒绝理由**：豁免清单需人工维护，且无法表达对"破坏性 vs 兼容"的自动区分；清单遗漏即失效。

### 方案 X4：结构/语义指纹分级判定（PR #755 初版）

- **拒绝理由**：在顶层无条件校验的架构下，指纹比较退化为纯开销（REDUNDANCY-1）。
  且关键词手工枚举（不含 minimum/maximum/pattern/format）存在覆盖盲区（EDGE-1），
  尽管被顶层兜底，但形成了语义模糊的两套判断路径。

### 方案 X5：本 ADR 方案（校验器驱动 + tool_contract_revision 变化探测）

- **采纳理由**：
  1. `validate_publishable_signals_json` 是已有的完备校验器，覆盖所有约束关键词；
  2. 单次调用结果全程复用，消除双重校验（REDUNDANCY-1）；
  3. `soft_stale` 仅在无其他问题时计数，指标语义精确，消除 BUG-2；
  4. 删除 220 行递归指纹代码，用 10 行差量替代，代码量减半；
  5. 保留 `tool_contract_revision` 向后兼容，存量 KBD 无需迁移快照格式；
  6. 完整可观测（`soft_stale` / `hard_break` 指标 + `trace_id`），故障可定位；
  7. 符合 024 铁律（存量刷新走重新发布，不 SQL 写死）。

---

## 四、落地路径

| 阶段 | 内容 | 涉及目录（触发文档门禁） |
|---|---|---|
| P0 | 校验器驱动门禁 + 移除两级指纹代码 + `certify` 不再写入结构/语义指纹字段 + 新指标（PR #755） | `backend/shared/schemas/`、`backend/kb-service/app/routes/` + `docs/` |
| P1 | 补单测（兼容放行 / breaking 阻断 / soft_stale 精确计数），接 `test_playbooks.py` 范式 | `backend/kb-service/tests/` |
| P2 | 存量 7 条 KBD 经 maintenance 工作稿批量刷新快照 | 数据迁移 + `docs/` |
| P3 | 同步更新契约演进规范与 `AGENTS.md` | `docs/` |

> 文档门禁：P0 改动 `backend/`、`scripts/` 时，须在同一 commit/PR 同步 `docs/` 至少一项（本 ADR 即满足）。

---

## 五、关联

- 事件文档：`2026-08-13-kbd23821-仿真delta-matcher阻塞修复与fixture补齐.md`（23821 旧 stale 修复）
- 数据迁移铁律：`database/data-migrations/024_fix_kbd_23821_scope_and_matcher.sql` 注释
- 诊断门禁代码：`backend/kb-service/app/routes/playbooks.py:_execution_issues`
- 契约指纹代码：`backend/shared/schemas/signal_generation.py:current_tool_contract_revision`
- 单一来源生成器：`backend/scripts/gen-schemas.py`
