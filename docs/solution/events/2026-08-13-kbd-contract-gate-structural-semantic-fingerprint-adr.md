---
status: active
category: solution
audience: backend, architect, ai-agent
last_updated: 2026-08-13
owner: team
related_pr: 待创建（治本实现阶段）
---

# ADR: KBD 自动执行契约门禁——结构/语义指纹分级判定

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

## 二、决策：结构/语义指纹分级门禁

### 2.1 核心思想

将"能否执行"的判定，从**单一字节哈希全等**，拆分为两个正交维度：

| 维度 | 决定什么 | 旧 KBD 违反的后果 |
|---|---|---|
| **结构约束** | 字段存在性、`type`、`required`、`enum`/`const` 取值、`additionalProperties` 开关 | 校验失败 → 真 breaking，必须重发 |
| **语义约束** | `description` 变化、默认值变化、`enum` 成员增删、聚合含义变化 | 结构仍合法，结果可能漂移 → 兼容但需警觉 |

### 2.2 指纹算法（精确可落地）

#### 结构指纹 `structural_fingerprint`

对每个 schema 文件，提取与"能否解析"强相关的子结构，归一化后哈希：

```python
def _structural_repr(schema: dict) -> dict:
    return {
        "required": sorted(schema.get("required", [])),
        "properties": {
            k: {
                "type": _norm_type(v.get("type")),        # 类型（归一化 oneOf/anyOf 为集合）
                "enum": sorted(v.get("enum", [])),         # 枚举取值（强约束）
                "const": v.get("const"),                  # 常量约束
                "additionalProperties": v.get("additionalProperties"),
            }
            for k, v in (schema.get("properties") or {}).items()
        },
        # if/then 内的 required 变更同样是结构破坏性（如 match.type=threshold 要求 value/operator）
        "conditional_required": _extract_if_then_required(schema),
    }
```

> **关键**：`_structural_repr` 必须**递归**进入所有 `$ref` / `allOf` / `oneOf` / `if-then` 子树，
> 不可只看顶层 `properties`。

#### 语义指纹 `semantic_fingerprint`

在结构指纹基础上，额外纳入不改变解析但影响语义的项：

```python
def _semantic_repr(schema: dict) -> dict:
    return {
        **_structural_repr(schema),
        "descriptions": _collect_descriptions(schema),   # 所有 description 文本
        "defaults": _collect_defaults(schema),           # 所有 default 值
        # enum 成员的增删作为语义漂移信号（见 2.4 规则）
    }
```

#### 版本自动推断（在 `gen-schemas.py` 生成期完成）

`gen-schemas.py` 是单一来源、幂等生成器（固定顺序 + `ensure_ascii=False` + `indent=2`），
适合在生成时对比上一次提交的 schema 自动推断版本号：

```python
def infer_contract_version(old: dict, new: dict) -> tuple[int, int]:
    if _structural_repr(old) != _structural_repr(new):
        return (old_major + 1, 0)          # breaking：结构破坏性
    if _semantic_repr(old) != _semantic_repr(new):
        return (old_major, old_minor + 1)  # compatible：仅语义漂移
    return (old_major, old_minor)
```

版本写入 `signal.v2.schema.json` 的 `$comment` 或独立 `contract_version.json`，
供 `current_contract_version()` 暴露（保留原 `current_tool_contract_revision` 作为"是否变动"探测用）。

### 2.3 门禁改造（`_execution_issues`）

```python
cur_struct = current_structural_fingerprint()      # lru_cache 快路径
cur_semantic = current_semantic_fingerprint()
pv = publish_validation or {}
stored_struct = pv.get("structural_revision")
stored_semantic = pv.get("semantic_revision")

if pv and pv.get("status") == "passed":
    if stored_struct != cur_struct:
        # 结构变了：实跑当前 schema 校验旧 signals
        if not _validate_signals_against_current(signals):
            issues.append("契约结构破坏性变更，信号无法在新契约下执行，必须重新发布")  # 真阻断
        # 校验通过 => 兼容演进，放行
    elif stored_semantic != cur_semantic:
        emit_soft_stale(support_id, category)        # 放行 + 可观测，不阻断
```

**对 #745 的推演**：23821 缺 `metric` 字段，但 `match` 为 `additionalProperties:False` 且 `metric`
非 required，用新 schema 校验通过 → `stored_struct == cur_struct` → 走 `elif` → `soft_stale` 告警 →
**`executable=True`，仅观测**。兼容变更不再误杀。

### 2.4 `enum` 变更规则（对抗性审查细化）

| 变更 | 分类 | 门禁行为 |
|---|---|---|
| `enum` 成员**只增不减** | 兼容（minor） | 放行 + `soft_stale` |
| `enum` 成员**删除** / `const` 改变 | 破坏性（major） | 结构指纹变 → 实跑校验，失败则阻断 |
| `not` / `if-then` 内枚举变更 | 破坏性（major） | 同上 |

### 2.5 算法版本化

指纹算法本身升级（如开始递归）会改变所有存量快照的 `structural_revision`，导致误判"全员 breaking"。
引入 `FP_ALGO_VERSION` 写入快照；当 `stored_fp_algo != cur_algo` 时，触发一次性全量重算
（重新生成 schema + 存量 KBD 批量刷新快照），而非误判过期。

### 2.6 可观测性（遵循全局可观测性规则）

新增 Prometheus 指标：

- `KBD_CONTRACT_SOFT_STALE_TOTAL{support_id, category}` —— 兼容演进但语义漂移；
- `KBD_CONTRACT_HARD_BREAK_TOTAL{support_id, category}` —— 真 breaking，阻断。

结构化日志携带 `trace_id`，链路追踪到分类查询入口。

### 2.7 存量止血（符合 024 数据迁移铁律）

024 迁移注释明确：契约 hash 过期**禁止在 SQL 写死 hash**，必须由 KBD 走 maintenance 工作稿重新发布刷新盖章。
治本 PR 合入后，对"结构校验通过"的 7 条 KBD 经 maintenance 工作稿批量刷新
`structural_revision` / `semantic_revision` 到当前值（仍走重新发布，非 SQL 直写语义值），消除 `soft_stale` 噪音。

---

## 三、为什么不选其他方案（决策依据）

### 方案 X1：每次 schema 改动后批量重发所有 KBD

- **拒绝理由**：仅把当前 hash 写回快照，下次任何 schema 改动又会 100% 过期。是"掩耳盗铃"，
  未触及"门禁用错判据"的根因。且依赖人工/批处理及时性，与 CI 持续合入节奏天然脱钩，必再发型故障。

### 方案 X2：直接移除契约门禁（完全不做版本校验）

- **拒绝理由**：契约门禁保护的是"旧 KBD 的 signals 在当前代码下能正确执行"。若真发生破坏性 schema 变更
  （如删除 `acquire.tool` 字段），无门禁会让旧 KBD 在运行时崩溃或产生错误结论，比拦截更危险。
  门禁应"精准"而非"取消"。

### 方案 X3：仅用整体 hash + 向后兼容豁免清单（白名单）

- **拒绝理由**：豁免清单需人工维护，且无法表达对"破坏性 vs 兼容"的自动区分；清单遗漏即失效，
  且与环境漂移耦合。不如从指纹语义层面自动区分。

### 方案 X4：本 ADR 方案（结构/语义指纹分级 + 版本自动推断 + 实跑校验兜底）

- **采纳理由**：
  1. 用"语义兼容性"替代"字节全等"，精准放行 #745 这类兼容变更；
  2. 对真 breaking 仍保留硬阻断 + 实跑校验兜底，安全性不降级；
  3. 版本号由 `gen-schemas.py` 自动推断，符合现有单一来源、幂等生成范式，零人工维护；
  4. 完整可观测（`soft_stale` / `hard_break` 指标 + `trace_id`），故障可定位；
  5. 符合 024 铁律（存量刷新走重新发布，不 SQL 写死）。

---

## 四、落地路径（分阶段，降低风险）

| 阶段 | 内容 | 涉及目录（触发文档门禁） |
|---|---|---|
| P0 | 拆结构/语义指纹 + `gen-schemas.py` 版本自动推断 + `_execution_issues` 分级门禁 + 新指标 | `backend/shared/schemas/`、`backend/kb-service/app/routes/`、`backend/scripts/` + `docs/` |
| P1 | 补单测（兼容放行 / breaking 阻断 / 语义漂移 soft_stale），接 `test_playbooks.py` 范式 | `backend/kb-service/tests/` |
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
