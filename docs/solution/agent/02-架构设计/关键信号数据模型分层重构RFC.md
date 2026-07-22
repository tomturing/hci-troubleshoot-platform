---
status: draft
category: solution
audience: developer
last_updated: 2026-07-22
owner: team
---

# 关键信号（signals_json）数据模型分层重构 RFC

> 本文档是**独立于缺陷修复的后续重构 RFC**，与事件文档
> `docs/solution/events/2026-07-22-QKV-QFK关键信号关键词字段失同步分析与修复方案.md`
> 互补：事件文档解决"当下事故"（增量删死字段），本文档解决"数据结构债"（按职责嵌套分层）。
> **当前状态：RFC draft，待评审，未实施。**

---

## 0. 摘要（TL;DR）

- **问题**：`kbd_entry.signals_json` 中每条信号是一个**扁平大表**，把"采集契约 / 触发契约 / 编排依赖 / 来源质量 / 审核工作流"5 类有界上下文压进同一个 dict，导致缺单一事实来源（SSOT）、死字段（`keyword` 顶层、`status`）、异义同名（`acquirer_args.keyword` 在 qfk 实为资源/主题）、以及"UI 写顶层、agent 读嵌套"的读写模型错位。这直接造成了 KBD 27123 "改了关键词不生效"的线上事故。
- **主张**：按第一性原理，将存储态信号**重构为按职责嵌套的分层模型** `acquire / match / orchestrate / provenance / review`，每个概念只存在一次，每个职责一段。
- **范式依据**：DDD 值对象/实体、CQRS 读写模型对齐、配置即数据的 Schema 版本化（expand-contract 并行变更）、以及监控探针领域的定型分离范式（Prometheus `expr+for+labels`、Datadog `query+threshold+aggr`、K8s probe）。
- **与现有架构的一致性**：本 RFC **不推翻** `关键信号架构演进` 确立的"信号=原子单元（取数+参数+判定捆在一起）"与"变量池黑板"原则——它仍是一条信号记录，只是**内部**按职责分段；其 `match` 段直接复用已落地的 `Matcher` 定型求值器，`acquire`/`requires` 仍走变量池渲染。
- **迁移**：采用 expand-contract（加新读新→双写→迁移存量→删旧），引入 `schema_version`，避免一次性大爆炸迁移。

---

## 1. 背景与问题陈述

### 1.1 事故溯源（来自事件文档）

KBD 27123 用户在 UI 把 qkv_task 关键词从 `启动虚拟机失败` 改为 `启动虚拟机`，但 agent 回采仍执行 `acli ... task get -k '启动虚拟机失败'`，且无 `-s failed`。根因是：

1. 前端编辑只写**顶层 `keyword`**；
2. 后端 agent 路径（`_signal_to_qkv`）只读 **`acquirer_args.keyword`**；
3. `status:"failed"` 写了但 agent 只读 **`is_failed`**（缺位即 False）→ `-s failed` 永不拼出。

这暴露的不是"某个字段配错"，而是**数据模型层面缺 SSOT + 扁平混排**的结构性缺陷。

### 1.2 当前存储态（真实库内样例，已去除敏感信息）

**QKV（qkv_task）**

```json
{
  "id": "s1", "risk": 1, "phase": "diagnostic", "action": "status",
  "source": "steps_text", "target": {}, "keyword": "启动虚拟机",        // ❌ 死字段（agent 不读）
  "matcher": null, "timeout": 10, "acquirer": "qkv_task", "expected": true,
  "produces": [{"name":"VM","path":"vm"}, ...], "requires": [],
  "container": "asv-con", "confidence": 0.95, "match_mode": "or",
  "description": "...", "instruction": "...", "needs_review": false,
  "acquirer_args": { "limit": 100, "status": "failed",                  // ❌ status 死字段
                      "keyword": "启动虚拟机", "is_failed": true },
  "source_section": "steps_text", "signal_category": "frontend",
  "extraction_method": "llm_field_level_v1", "require_human_confirm": false
}
```

**QFK（qfk_log）**

```json
{
  "id": "s1", "risk": 1, "phase": "diagnostic", "source": "steps_text",
  "keyword": "绑定vgpu命令失败",                                       // ❌ 死字段（= matcher.pattern 镜像）
  "matcher": { "mode": "any", "type": "keyword",
               "pattern": "绑定vgpu命令失败", "expected": true },      // ✅ agent 读这里
  "acquirer": "qfk_log", "produces": [], "requires": ["HOST"],
  "confidence": 0.85, "description": "...", "needs_review": false,
  "acquirer_args": { "keyword": "vgpu", "resource": "{{HOST}}" },      // ⚠️ 异义同名：是资源/主题，非匹配关键词
  "source_section": "steps_text", "signal_category": "backend",
  "extraction_method": "llm_field_level_v1", "require_human_confirm": false
}
```

### 1.3 字段膨胀事实（基于代码读取证据）

- 顶层 `keyword`：仅前端展示/编辑与 `from_dict` 备用路径读取；**运行时 agent 路径不读**（生产无 `s->>'keyword'` 查询）→ 纯死字段。
- `acquirer_args.status`：parser 写、agent 不读 → 死字段。
- qfk `acquirer_args.keyword`（值 `"vgpu"`）：是日志资源/主题过滤，**与匹配关键词异义同名**。
- `confidence` / `source_section` / `signal_category`：被抽取评分与前后端分流读取（属"来源与质量"上下文）。
- `risk` / `extraction_method` / `expected` / `match_mode` / `needs_review` / `timeout`：后端未检索到执行路径读取（疑似未消费或仅 UI 用）。

---

## 2. 第一性原理分析

### 2.1 单一事实来源（SSOT）

一个概念（"查询关键词"）只该有一个权威存储位置。当前同一概念有三个副本：`顶层 keyword`、`acquirer_args.keyword`、`matcher.pattern`（qfk）。写方（前端）与读方（agent）对"哪个是真相"理解冲突，且**读方内部还自相矛盾**（QKV 路径读嵌套、另一条 `qkv_load`/`from_dict` 路径读顶层）。无机制保证一致 → 漂移是必然。

### 2.2 单一职责 / 有界上下文

一条信号被 5 类消费者使用，本应各有字段边界，却被摊平进同一 dict：

| # | 有界上下文 | 字段（当前扁平） | 是否进执行路径 |
|---|---|---|---|
| ① | 采集契约 | `acquirer` + `acquirer_args` | ✅ |
| ② | 触发契约 | `matcher`（qfk） | ✅ |
| ③ | 编排/依赖 | `produces` `requires` `phase` `action` `target` `container` `timeout` `expected` `match_mode` | ✅ |
| ④ | 来源与质量 | `signal_category` `confidence` `source_section` `extraction_method` `risk` `needs_review` | ❌ 仅评分/路由/UI |
| ⑤ | 审核/工作流 | `require_human_confirm` | ❌ 仅闸门 |

**根因**：5 类职责无嵌套隔离，"执行该信什么"和"谁抽的/质量多高/要不要人确认"混在一起，任一消费者加字段都直接污染全局。

### 2.3 命名即契约

把冗余展示字段也命名 `keyword`，等于用名字伪装成权威源，制造"改了顶层却没生效"的陷阱。qfk 把"日志资源/主题"也命名 `acquirer_args.keyword`，与"匹配关键词"同名，加剧混乱。**命名过载是第一性原理层面的契约缺失。**

### 2.4 缺少 Schema 演进治理（演化债）

`关键信号架构迁移指南.md` 仅定义"命名映射 + from_dict 自动判别"，**未定义 signals_json 的版本号、迁移脚本或向后兼容策略**（其 Q1 以"PR 未合入、迁移成本为零"为由不做兼容）。一旦信号已落库并被人编辑，再无版本锚点，任何结构改动都面临"全量数据迁移 vs 永久双读"的两难。这是本次必须补上的治理缺口。

### 2.5 读写模型错位（CQRS 视角）

前端编辑器的**写模型**是"顶层 `keyword` 可改"，agent 的**读模型**是"`acquirer_args.keyword` / `matcher.pattern`"。两者没有共享的权威源，等价于 CQRS 中读写模型从未对齐。嵌套分层 + 单一权威源，正是把读写模型重新统一到同一份数据。

### 2.6 与现有架构原则的关系（重要一致性声明）

- `关键信号架构演进-从两方案批判到分层修正.md` 主张"**一条信号 = 原子单元：用哪个工具取数 + 取什么参数 + 怎么判定，三者在一条信号里捆在一起**"。本 RFC 的嵌套模型**不违背**该原则：它仍是**一条信号记录**，只是**内部**按职责分段。原子性（一条记录）与内部分层（字段分组）是正交的两件事。
- 该文档已将 `expected_pattern` 升级为**定型 `Matcher` 求值器**（`type/mode/expected`），并确立"变量池黑板"（producer 写、consumer 读）。本 RFC 的 `match` 段直接承接 `Matcher`，`acquire`/`requires` 仍走变量池渲染——**完全复用既有设计，只是把扁平字段归位到各自段落**。
- 统一占位符 `{{VAR}}`（演进决策②）在嵌套模型下仍然成立（`acquire.args` 与 `orchestrate.requires` 共用同一套渲染）。

### 2.7 字段消费实情审计：expected / match_mode / risk / extraction_method

> 本节用"设计意图 vs 运行时实情"对照，澄清这 4 个被初步标记为"疑似未消费"的字段。结论修正：**`expected`/`match_mode` 在 QFK 链路里是核心被消费字段，问题不在"未被消费"而在"producer 写 `matcher.*`、consumer 读顶层 `match_mode/expected` 的命名错位"；而 `risk`/`extraction_method` 才是真正的只写不读（write-only provenance）。**

#### 2.7.1 expected / match_mode（QFK：被消费，但命名错位）

- **设计意图**：`expected`（布尔，期望匹配成立与否）与 `match_mode`（组合逻辑 `or`/`and`）是"信号触发判定的定型参数"，随 `matcher` 一起由 LLM 抽取，描述"这条信号在什么情况下算命中"。
- **运行时实情（被消费）**：
  - 持久化形态：`matcher: { type, pattern, mode, expected }`（qfk 信号）。
  - 转换链路：`kbd_differential._signal_to_qfk`（`kbd_differential.py:1057-1058`）读 `matcher.get("mode","or")` → `match_mode`、`matcher.get("expected",True)` → `expected`，填充 `BackendSignal.match_mode`/`expected`；`qfk/handlers.py` 与 `engine.py` 据此做命中判定与门控。
  - 因此 **`expected`/`match_mode` 在 QFK 是执行路径字段，非死字段**。
- **真正的病灶（命名错位 / 读写模型错位）**：
  - producer 写 `matcher.mode`/`matcher.expected`，consumer 运行时模型却是顶层 `match_mode`/`expected`（`qfk/signal.py:50-51`，`handlers.py:5-7` 注释"顶层共有字段：keyword, timeout, expected, match_mode"）。**同一语义、两套字段名**，靠 `_signal_to_qfk` 手动搬移。
  - QKV 信号顶层也有 `expected`/`match_mode`（`expected:true`、`match_mode:"or"`），但 QKV **没有关键词匹配语义**，agent 拼 `acli task get` 时根本不读它们 → **QKV 顶层副本才是真死字段**。
- **重构处置**：把 `type/pattern/mode/expected` 统一收口到 `match` 段；consumer 直接从 `match` 读，消除"matcher.* → 顶层 match_mode/expected"的手动搬移（详见 §4.4）。QKV 顶层的 `expected`/`match_mode` 在归位时**不迁移**（其无语义），避免复活死字段。

#### 2.7.2 risk（只写不读：被 require_human_confirm 取代的冗余闸门）

- **设计意图**：`extract_signals.py:211` 注释明确——`risk: 风险等级 1/2/3（供执行层门禁与分类器兜底）`。它意图复刻 acli 工具的风险分级（见 `tool_registry.py`/`react_engine.py`/`executor.py`/`classifier.py` 里的 acli command risk=1/2/3），作为"该信号是否需人工确认"的**数值闸门**。
- **运行时实情（只写不读）**：
  - 写入：`extract_signals.py:232` `enriched["risk"] = _write_op_risk(...)`。
  - 读取：**没有任何消费方读取信号 JSON 的 `risk` 来决策**。真正的门禁走 `require_human_confirm`（布尔，`extract_signals.py:226-232` 正是用 `risk` 推导出 `require_human_confirm` 后写入）。
  - 旁证：`kbd_differential.py:340`/`:496` 确实读 `risk`，但读的是 `rep_signal.get("risk",2)` / `kbd.get_signal(tool)` 返回的**KBD 工具定义**的风险（acli 命令风险），**不是信号 JSON 的 `risk`**——两类 `risk` 同名不同源，容易误判。
- **为何消亡**：存在两套重叠的"是否需确认"设计——数值型 `risk`（1/2/3）与布尔型 `require_human_confirm`。最终**布尔闸门胜出**成为实际消费字段，数值 `risk` 退化为"仅用于推导 `require_human_confirm` 的中间量"，推导完即被丢弃，再无人读。这是典型的"双闸门设计收敛后，失败一方沦为死字段"。
- **重构处置**：`risk` 归位到 `provenance.risk` 作为质量标注保留（可用于 UI 排序/审计），但**明文标注"不参与运行时门禁，门禁以 `review.require_human_confirm` 为准"**，杜绝再次误读。

#### 2.7.3 extraction_method（只写不读：期待未来而未至的 provenance）

- **设计意图**：`extract_signals.py:95-96` 注释——`字段级溯源：抽取方法标识（写入每条信号的 extraction_method）`，常量 `EXTRACTION_METHOD = "llm_field_level_v1"`。意图是为每条信号打"由何种抽取方法产生"的溯源戳，以便未来按方法分支解析、或审计/回放。
- **运行时实情（只写不读）**：全代码库搜索 `extraction_method`，**仅有 `extract_signals.py:96/207/215` 的写入/常量定义，无任何读取方**。
- **为何消亡**：系统从始至终只有一种抽取方法（`llm_field_level_v1`），没有第二分支需要据此切换逻辑，也没有报表/审计查询它。纯前瞻型元数据（schema anticipating a future that never came）。
- **重构处置**：保留于 `provenance.method`（溯源有价值），但**不进入任何运行时分支**；待将来确需多方法时再激活消费，届时配套"按 method 选择解析器"的逻辑，避免现在就造用不上的分支。

#### 2.7.4 审计小结

| 字段 | 设计意图 | 运行时实情 | 真实分类 | 重构处置 |
|---|---|---|---|---|
| `expected` | 命中判定的定型参数 | QFK 经 `matcher`→`BackendSignal`→`handlers`/`engine` 被消费 | **被消费（命名错位）** | 收口 `match.expected`，consumer 直读 |
| `match_mode` | 组合逻辑 `or`/`and` | 同上 | **被消费（命名错位）** | 收口 `match.mode`，consumer 直读 |
| `risk` | 数值闸门（1/2/3） | 仅用于推导 `require_human_confirm`，推导后无人读；同名 KBD 工具风险易误判 | **只写不读（冗余闸门）** | 归位 `provenance.risk`，标注不参与门禁 |
| `extraction_method` | 抽取方法溯源戳 | 仅写入，无读取方 | **只写不读（前瞻元数据）** | 归位 `provenance.method`，不进运行时分支 |

> 这 4 字段的"疑似未消费"恰好印证 §2.1 的 SSOT 缺失：当字段无单一权威、无消费契约时，要么被另一字段取代（`risk`→`require_human_confirm`），要么因前瞻设计永远不被查询（`extraction_method`），要么靠手动搬移在 producer/consumer 间错位流动（`expected`/`match_mode`）。**统一 `acquire.args` 契约（§4.4）正是从根上消除这类错位。**

---

## 3. 业界最佳实践与范式对照

### 3.1 DDD：实体与值对象

信号是**实体**（由稳定 `id` 标识，支持人工编辑/去重）；`acquire` `match` `orchestrate` `provenance` `review` 是**值对象**（由内容定义、不可变、可整体替换）。把值对象提升为显式嵌套结构，是 DDD 对"大泥球（Big Ball of Mud）"对象的标准解药。

### 3.2 CQRS / 读写模型统一

命令侧（UI 保存）与查询侧（agent 执行）必须基于**同一权威源**。当前错位应通过"删冗余副本 + 唯一可写源"消除，而非各读各的。

### 3.3 配置即数据（12-Factor / Schema 版本化）

`signals_json` 本质是可被 LLM 写、人工改、agent 读的**声明式配置**。业界对声明式配置的演进共识：

- **Add-only / expand-contract（并行变更，Martin Fowler）**：先加新结构并双写，再让读方切新，再迁移存量，最后删旧。禁止"改字段即一次性迁移全量"。
- **Schema 版本锚点**：`signals_json` **数组级**带 `schema_version`（整批同版本，见 §7/§10.2），读取方按版本路由（如 `from_dict` 已具备按 `signal_category` 路由的能力，可推广为按 `schema_version` 路由）。
- **JSON Schema 契约**：为嵌套模型定义 `$schema`/`schema_version`，用 `required`/`additionalProperties:false` 在保存时强制不变量（杜绝再冒出顶层 `keyword` 之类幽灵字段）。

### 3.4 监控/探针领域的定型分离范式

`关键信号架构演进 §6` 已引用：Prometheus（`expr + for + labels`）、Datadog（`query + threshold + aggr`）、K8s probe 都是"2~3 个**正交定型**字段"的声明式探针，而非图灵完备语言。本 RFC 的 `acquire`（定型采集）+ `match`（定型求值器）+ `orchestrate`（依赖图）正是对该范式的结构化落地：采集、判定、编排三者正交、各自可 admin 编辑、可审计。

### 3.5 数据迁移模式

- **Expand-Contract**：避免大爆炸。本 RFC 第 7 节据此设计四阶段。
- **Blue-Green 双读**：迁移期新旧结构并存，按 `schema_version` 选择解析路径，出错可即时回滚到旧路径。

---

## 4. 目标数据模型（嵌套分层）

### 4.1 统一结构

```jsonc
// signals_json 整体形态：数组级 schema_version（整批同版本，见 §7 / §10.2）
{
  "schema_version": 2,                       // 数组级版本锚点（一次迁移整批同版本）
  "signals": [
    {
      "id": "s1",
      "acquire": {                            // ① 采集契约（producer/consumer 同构权威源）
        "tool": "qkv_task",                   // 取自 ACQUIRER_CATALOG（封闭词表，SSOT）
        "args": {
          // ── 公共参数（common_args，全局定义一次，所有 acquirer 共享，禁止各工具另造）──
          "timeout": 10,                       // 采集超时（秒）；QKV/QFK 通用
          // ── qkv_task 专属参数（ACQUIRER_ARGS_SCHEMA["qkv_task"] 注册，带注释）──
          "keyword": "启动虚拟机",              // 采集关键词（acli task get -k）；qkv 无独立 match，关键词即查询过滤
          "limit": 100,                        // 翻页
          "is_failed": true                    // 失败标志（由 status 派生），控制 -s failed
        }
      },
      "match": {                              // ② 触发契约（qfk/consumer；qkv 可缺）
        "type": "keyword",
        "pattern": "绑定vgpu命令失败",           // 匹配关键词唯一权威源（= 原 matcher.pattern）
        "mode": "any",                         // = 原 match_mode（consumer 直读，不再经 matcher.* 搬移）
        "expected": true                      // 期望命中（consumer 直读，见 §2.7.1）
      },
      "orchestrate": {                        // ③ 编排/依赖（执行路径）
        "phase": "diagnostic", "action": "status",
        "source": "steps_text", "target": {}, "container": "asv-con",
        "produces": [{"name":"VM","path":"vm"}], "requires": ["HOST"]
        // 变量池渲染走 acquire.args 的 {{VAR}}
      },
      "provenance": {                         // ④ 来源与质量（不进执行路径）
        "category": "frontend",               // = 原 signal_category
        "method": "llm_field_level_v1",       // = 原 extraction_method（溯源戳，不进运行时分支，见 §2.7.3）
        "source_section": "steps_text",
        "confidence": 0.95,
        "risk": 1,                            // 质量标注，不参与门禁（见 §2.7.2）
        "needs_review": false
      },
      "review": {                             // ⑤ 审核/工作流（不进执行路径）
        "require_human_confirm": false        // 唯一运行时门禁（见 §2.7.2）
      }
    }
  ]
}
// 另例（qfk_log）acquire.args：注意 resource_keyword ≠ match.pattern（语义消歧见 §4.4.4）
//   { "tool": "qfk_log", "args": { "timeout": 10, "resource_keyword": "vgpu", "resource": "{{HOST}}", "file": "/var/log/x.log", "end": "now" } }
```

### 4.2 字段映射表（扁平 → 嵌套）

| 原扁平字段 | 目标段落 | 处置 |
|---|---|---|
| `acquirer` | `acquire.tool` | 迁移 |
| `acquirer_args.*` | `acquire.args.*` | 迁移（统一结构，见 §4.4） |
| `acquirer_args.keyword`（qkv） | `acquire.args.keyword` | 迁移（唯一权威，公共/专属见 §4.4.3） |
| `acquirer_args.keyword`（qfk="vgpu"） | `acquire.args.resource_keyword` | **重命名**（异义消除，见 §4.4.4） |
| `acquirer_args.status` | — | **删除**（由 `is_failed` 派生） |
| `acquirer_args.is_failed` | `acquire.args.is_failed` | 迁移 |
| `acquirer_args.timeout` | `acquire.args.timeout` | 迁移（归入 **common_args**，所有 acquirer 共享） |
| `matcher` | `match` | 迁移（仅 qfk/consumer；`expected`/`mode` 经 §4.4.5 直读，不再搬移为顶层） |
| 顶层 `keyword`（qkv/qfk） | — | **删除**（死字段 / 副本，见 §2.7.1） |
| 顶层 `expected` / `match_mode`（qkv） | — | **不迁移**（QKV 无匹配语义，属死副本，见 §2.7.1） |
| `produces` `requires` `phase` `action` `source` `target` `container` | `orchestrate.*` | 迁移 |
| `signal_category` | `provenance.category` | 迁移 |
| `extraction_method` | `provenance.method` | 迁移（溯源戳，**不进运行时分支**，见 §2.7.3） |
| `source_section` `confidence` `needs_review` | `provenance.*` | 迁移 |
| `risk` | `provenance.risk` | 迁移（质量标注，**不参与门禁**，见 §2.7.2） |
| `require_human_confirm` | `review.require_human_confirm` | 迁移（唯一运行时门禁） |
| `signals_json` 包裹 | `signals_json = { schema_version, signals:[...] }` | **数组级版本锚点**（见 §7 / §10.2） |

### 4.3 与 `KeySignal` 基类的关系

现有 `KeySignal`/`FrontendSignal`/`BackendSignal`（字段级分别抽取 §2）已是**分层类模型**；问题出在"类模型分层，但落库 JSON 仍扁平"。本 RFC 是**存储态的结构化**，不改类继承体系，只让 `from_dict` 在反序列化时按 `schema_version` 把扁平/嵌套两种形态都映射到既有类。即：**类模型不变，持久化形态升级。**

### 4.4 统一 `acquire.args` 契约（producer/consumer 同构）

> 用户决议：**"要重构就重构彻底"**——统一 producer（LLM 抽取写库）与 consumer（agent 执行读取）的 `acquire.args` 结构，做到通用、统一、解耦。核心原则（第一性原理）：**公共字段全局只定义一次，特有字段各工具单独注册并注释区分。**

#### 4.4.1 第一性原理：为什么必须 producer/consumer 同构

- **CQRS 同构契约（§2.5）**：命令侧（UI/抽取写）与查询侧（agent 读）必须基于同一份 `acquire.args` 形态。当前 `expected`/`match_mode` 的"matcher.* 写、顶层 match_mode/expected 读"错位（§2.7.1），正是读写形态未对齐的产物。统一后，无论谁写、谁读，键名、嵌套、类型完全一致，不再需要 `_signal_to_qfk` 之类手动搬移。
- **单一事实来源（SSOT，§2.1）**：当前"关键词"有 3 个候选源（顶层、`acquirer_args.keyword`、`matcher.pattern`），"失败标志"有 2 个（`is_failed`/`status`）。统一契约强制：每个语义只有一个键（匹配关键词唯一为 `match.pattern`，采集关键词唯一为 `acquire.args.keyword`），从结构上消灭幽灵副本。
- **正交职责（§3.4 定型分离）**：`acquire`（怎么取数）与 `match`（怎么判定）语义正交，必须分属不同段，绝不能把 `match` 的 `expected`/`mode` 塞进 `acquire.args` 或 `orchestrate`。

#### 4.4.2 工业范式：工具参数 Schema 注册表（registry）

对齐业界"工具调用参数契约"范式：

- **OpenAI function-calling / MCP `input_schema`**：每个工具暴露单一 `parameters` JSON Schema；工具目录即 registry。→ 本设计用 `ACQUIRER_ARGS_SCHEMA[tool]` 注册表，键为 `acquire.tool`（取自 `ACQUIRER_CATALOG` 封闭词表）。
- **OpenAPI `components/schemas` + `$ref`**：公共字段定义一次、被所有工具 schema 引用，杜绝重复声明。→ 本设计用 `COMMON_ARGS`（`$defs`）承载 `timeout` 等跨工具字段，各工具 schema `required`/`additionalProperties:false` 引用之。
- **Kubernetes CRD structural schema / `additionalProperties:false`**：在准入（admission）时强校验，禁止未知字段。→ 本设计在**保存时**按 `ACQUIRER_ARGS_SCHEMA[tool]` 校验 `acquire.args`，未知字段报错。

#### 4.4.3 结构设计：公共参数一次定义 + 各工具单独注册

```jsonc
// 公共参数：$defs/common_args 全局定义一次，所有 acquirer 共享
// 规则：任何适用于 >1 个 acquirer 的字段，必须进 common_args，禁止各工具另造同名
COMMON_ARGS = {
  "timeout": {"type": "integer", "minimum": 1, "default": 10}   // QKV/QFK 通用采集超时
}

// 各工具 args 注册表（与 ACQUIRER_CATALOG 一一对应）；additionalProperties:false 防幽灵字段
ACQUIRER_ARGS_SCHEMA = {
  "qkv_task": {
    "type": "object", "additionalProperties": false,
    "properties": {
      "timeout": COMMON_ARGS["timeout"],        // 公共：引用而非重定义
      "keyword":  {"type": "string"},            // 专属：采集关键词（acli task get -k）；qkv 无独立 match，关键词即查询过滤
      "limit":    {"type": "integer", "default": 100},
      "is_failed":{"type": "boolean", "default": false}  // 失败标志（由 status 派生），控制 -s failed
    },
    "required": ["keyword"]
  },
  "qfk_log": {
    "type": "object", "additionalProperties": false,
    "properties": {
      "timeout": COMMON_ARGS["timeout"],        // 公共
      "resource_keyword": {"type": "string", "description": "资源/主题选择器（acli log get <topic>）；原 acquirer_args.keyword，改名消歧，非匹配关键词"},
      "resource": {"type": "string", "description": "目标资源定位（日志/服务名），支持 {{VAR}} 变量池"},
      "file":     {"type": "string", "description": "日志文件路径（qfk_log 专属）"},
      "end":      {"type": "string", "description": "时间窗（qfk_log 专属）"}
    },
    "required": ["resource_keyword", "resource"]
  },
  "qfk_system": {
    "type": "object", "additionalProperties": false,
    "properties": {
      "timeout": COMMON_ARGS["timeout"],
      "sub_command": {"type": "string", "description": "acli system get <sub_command>（如 lsof/ps auxf）"},
      "container":   {"type": "string", "description": "执行容器（qfk_system 专属）"}
    },
    "required": ["sub_command"]
  }
  // qfk_service / qfk_vm / qfk_network / qfk_storage / qfk_hardware / qfk_platform 各注册一段，
  // 均引用 COMMON_ARGS["timeout"]，专属字段各自注释区分
}
```

**落库形态（producer 写、consumer 读，同构）**：

```jsonc
// qkv_task 信号
"acquire": { "tool": "qkv_task", "args": { "timeout": 10, "keyword": "启动虚拟机", "limit": 100, "is_failed": true } }

// qfk_log 信号（注意：resource_keyword ≠ match.pattern）
"acquire": { "tool": "qfk_log", "args": { "timeout": 10, "resource_keyword": "vgpu", "resource": "{{HOST}}", "file": "/var/log/x.log", "end": "now" } }
```

#### 4.4.4 语义消歧：彻底解决"同名重载"

| 旧字段 | 旧语义（混乱） | 新字段 | 新语义（清晰） |
|---|---|---|---|
| 顶层 `keyword`（qkv） | 展示副本，agent 不读 | `acquire.args.keyword` | qkv 采集关键词（acli -k），唯一权威 |
| `acquirer_args.keyword`（qfk="vgpu"） | 与"匹配关键词"同名，实为资源/主题 | `acquire.args.resource_keyword` | 资源/主题选择器，注释标明"非匹配关键词" |
| `matcher.pattern`（qfk） | 匹配关键词，藏在 matcher | `match.pattern` | 匹配关键词唯一权威源 |
| `matcher.mode`/`matcher.expected` | consumer 读成顶层 match_mode/expected | `match.mode`/`match.expected` | consumer 直读，不再搬移 |

#### 4.4.5 consumer 同构：BackendSignal 由 `acquire.args` + `match` 重建

消费者运行时模型（`BackendSignal`）**只**由 `acquire.args` + `match` 重建，键名一一对应，彻底消除 producer/consumer 命名错位：

```python
# 旧：手动搬移 matcher.* → 顶层 match_mode/expected（错位根源）
# 新：直接映射，键名一致
bs.timeout    = args.get("timeout", 10)              # acquire.args.timeout（公共）
bs.keyword    = match.get("pattern")                # match.pattern（匹配关键词）
bs.match_mode = match.get("mode", "or")             # match.mode
bs.expected   = bool(match.get("expected", True))   # match.expected
bs.resource   = args.get("resource")                # acquire.args.resource（qfk_log）
bs.file       = args.get("file")                    # acquire.args.file
bs.end        = args.get("end")                      # acquire.args.end
bs.command    = args.get("sub_command")             # acquire.args.sub_command（qfk_system）
bs.container  = args.get("container")               # acquire.args.container
```

> **实现策略说明（已按用户决议"直接切 v2 列形态"落地）**：consumer 端采用**边界归一**而非全量改写——在 `kbd_from_dict` / `admin` GET 这一读取边界，用 `to_legacy_signal()` 把 v2 还原为既有扁平信号，`kbd_differential` 等下游**零改动**；模型 `from_dict` 同时原生容错 v2（双重保险）。这样存储层是干净的 v2（producer 写 v2 + `acquire.args` 校验），而运行时风险最小。后续可平滑演进为"consumer 直读 v2 段"（去掉边界归一），无需改存储契约。

#### 4.4.6 收益（第一性原理 + 范式）

- **简洁、无误解**：公共字段（如 `timeout`）全局一处定义，新增工具零重复；异义同名（qfk 的 `keyword`）被 `resource_keyword` 显式消歧并注释，杜绝"改了顶层却没生效"。
- **解耦**：新增/修改某个 acquirer 的参数 = 在 `ACQUIRER_ARGS_SCHEMA` 注册一段，producer 校验与 consumer 读取自动遵循；无需改动抽取或执行的核心逻辑。
- **防回归**：`additionalProperties:false` + 保存时按 schema 校验，任何幽灵字段（如再次冒出顶层 `keyword`）在落库即被拒。
- **支持特殊性**：各 acquirer 的专属参数字段各自维护、带注释区分，既统一了契约外壳，又保留工具间差异。

#### 4.4.7 代码落地（已搭建骨架）

§4.4 的设计已落地为可导入的共享模块（producer/consumer 同构、单一事实来源），无需等待 Phase 2 全量改造即可并入主干：

- **`shared/schemas/acquirer_args.py`**：`COMMON_ARGS` + `ACQUIRER_ARGS_SCHEMA`（11 个 acquirer 全量注册，与 `ACQUIRER_CATALOG` 一一对应）+ `validate_acquire_args(tool, args)`（纯 Python 校验，无需 `jsonschema` 依赖即可运行，语义对齐 §6.1 的 JSON Schema）。
- **`shared/schemas/signal_migration.py`**：`migrate_signal_document(raw)` / `migrate_signal_document` 纯函数，将扁平 v1 list → 嵌套 v2 `{schema_version, signals:[...]}`，**幂等**且**无损**（未识别的 v1 字段收进 `_v1_legacy`）。
- **`scripts/migrate_signals_v1_to_v2.py`**：DB/文件双模式迁移运行器（`--dsn` 生产模式、`--input/--output` 文件模式、`--dry-run` 干跑），Phase 2 执行。
- **producer 校验接入点**：`extract_signals.py:_validate_signal` 已挂接 `validate_acquire_args`——**仅当信号采用 v2 `acquire` 段时强制**，旧扁平格式在 Phase 0/1 双写期零破坏；Phase 1 抽取改为写 `acquire` 段后自动成为机器强制门禁（§6.1）。

> 已用线上真实样本验证：qfk_log 迁移后 `acquire.args={resource_keyword, resource}` 通过校验，二次迁移幂等，`additionalProperties:false` 正确拦截幽灵字段。

---

## 5. 影响范围与消费者全景（基于架构文档调研）

| 消费者 | 文件/函数 | 当前读取字段 | 重构改造点 |
|---|---|---|---|
| 信号组装（producer/consumer 拆分） | `kbd_differential.py` `_build_signal_set` / Phase A·B | `signal_category`, `produces`, `requires`, `acquirer_args`, `matcher` | 改为读 `acquire`/`match`/`orchestrate` 段 |
| QKV 命令构建 | `_signal_to_qkv` | `acquirer_args.keyword`, `is_failed`, `limit` | 经边界归一后读 `acquire.args.*`（同构契约，§4.4）；`_signal_to_qkv` 自身零改动 |
| QFK 命令/判定 | `_signal_to_qfk` | `matcher.pattern`, `acquirer_args` | 经 `kbd_from_dict` 边界归一为扁平后再读（与现状一致）；`expected`/`match_mode` 同源消除搬移（§2.7.1/§4.4.5） |
| 变量池渲染 | `variable_pool` / `_resolve_args` | `acquirer_args` 的 `{{VAR}}` | 渲染源统一为 `acquire.args`（边界归一后透传） |
| 去重 key | `kbd_differential` | `json.dumps(acquirer_args)` | 边界归一后 `json.dumps(acquire.args)` 等价 |
| 评分/分流 | `extract_signals.py` | `signal_category`, `confidence`, `source_section`, `produces`, `requires` | 读 `provenance`/`orchestrate`（`risk`/`method` 仅溯源，不进门禁，§2.7） |
| 备用加载路径 | `engine.qkv_load` → `FrontendSignal.from_dict` | 顶层 `keyword`（旧） | 模型 `from_dict` 已原生容错 v2（§4.4.5） |
| `acquire.args` 契约校验（**新增**） | `ACQUIRER_ARGS_SCHEMA` + `admin.py` 保存 + `_persist_signals` | — | producer 落库前 + admin 保存时按 `ACQUIRER_ARGS_SCHEMA[tool]` 校验（additionalProperties:false 禁幽灵字段，§4.4.2/§4.4.6） |
| 落库（producer） | `extract_signals.py` `_persist_signals` | 整块 `signals_json`（扁平 list） | **直接切 v2**：落库为 `{schema_version, signals:[...]}`，逐条校验 `acquire.args`（§7 实际策略） |
| 落库（admin 保存） | `admin.py` `update_kbd_entry` | 整块 `signals_json` | 入参接受 v2 对象或 v1 list，保存时 `migrate_signal_document` 归约为 v2 + 校验 |
| admin 审核门 | `admin.py` 审核接口 | `isinstance(signals_json, list)` 判 backend 信号 | 解包 v2 对象后按 `acquire.tool`/provenance 判定 backend（§2.7 修正） |
| 前端展示 | `KbdReviewView.vue` | v2 `acquire.tool`/`acquire.args`/`match`/`orchestrate`/`_v1_legacy` | **原生 v2 对象化（2026-07-22）**：admin GET 直出 `{schema_version, signals}`，前端**直接基于 v2 结构渲染**（删除 `KeySignal`/`toLegacyFromV2` 适配层，用 `sigTool/sigArgs/sigLeg/sigMatch/sigOrch` 直接从 v2 各段取值），零适配、零信息损失 |
| 前端编辑绑定 | `KbdReviewView.vue` `signalEditDraft` | `acquire.tool`/`acquire.args`/… | 同上：编辑草稿即 v2 单条信号，模板 v-model 直接绑定 `acquire.args.*`/`match.*`/`_v1_legacy.*`；保存回发完整 v2 文档 `{schema_version, signals}`，admin `update_kbd_entry` 幂等归约 |
| 编辑器组件 | `MatcherEditor.vue` `ProducesEditor.vue` | `matcher` / `produces` | 同上：直接绑定 v2 `match`/`orchestrate.produces`，无需适配层 |
| LLM 抽取写入 | `extract_signals.py` `_persist_signals` | 写顶层 `keyword`+`acquirer_args`+`matcher` | 内部仍用扁平 v1 处理，落库前经 `_signals_to_v2` 归约 v2（统一 `acquire.args` 形态，§4.4） |

> 注：以上读取点来自运行代码与架构文档调研；实施前应以 `grep` 全量复核一遍，确保无遗漏读取方（尤其生产 SQL 与报表查询）。

---

## 6. 不变量与契约（保存时强制）

1. **无顶层 `keyword`**：`additionalProperties:false` + 禁止 `keyword` 顶层键（QKV 改为 `acquire.args.keyword`，QFK 匹配词在 `match.pattern`，资源选择器在 `acquire.args.resource_keyword`）。
2. **QKV 关键词唯一源**：`acquire.args.keyword` 存在且为字符串；`match` 段对 producer 可缺。
3. **QFK 关键词唯一源**：`match.pattern` 存在（= 原 `matcher.pattern`）；`acquire.args.resource_keyword` 与匹配关键词语义隔离（不得再叫 `keyword`）。
4. **`expected`/`match_mode` 仅存于 `match` 段**：不得出现在 `acquire`/`orchestrate`/顶层（`expected`/`match_mode` 在 QFK 是执行路径字段，consumer 直读 `match.*`，见 §2.7.1/§4.4.5）。
5. **失败标志同向**：`acquire.args.is_failed == true` ⟺ `acquire.args.status == "failed"`（不一致报错）。
6. **`schema_version` 数组级必填**：`signals_json` 顶层为 `{schema_version, signals:[...]}`；缺省按 v1（扁平）兼容解析，新写必为 v2（见 §7/§10.2）。
7. **`acquire.args` 契约校验**：`acquire.args` 必须由 `ACQUIRER_ARGS_SCHEMA[tool]` 校验——公共字段仅 `common_args` 定义（如 `timeout`），各工具专属字段各自注册；`additionalProperties:false` 杜绝幽灵字段（§4.4.2/§4.4.6）。
8. **占位符统一**：`acquire.args` 与 `orchestrate.requires` 仅用 `{{VAR}}`（沿用演进决策②）。
9. **`provenance.risk` / `provenance.method` 不进执行分支**：仅作溯源/质量标注；运行时门禁以 `review.require_human_confirm` 为准（§2.7.2/§2.7.3）。

### 6.1 JSON Schema 契约与 CI 校验（开放问题③：已决议"是"）

- **生成契约文件**：为嵌套模型（及每个 `ACQUIRER_ARGS_SCHEMA[tool]`）生成 JSON Schema（`$schema: draft-07`，`additionalProperties:false`），随代码入库于 `schemas/signals/`：
  - `signal.v2.schema.json`：整体 `{schema_version, signals:[...]}` 结构，含 5 段 `required` 约束与字段类型。
  - `acquirer_args/{tool}.schema.json`：每个 acquirer 的 args 契约（由 `ACQUIRER_ARGS_SCHEMA` 直接导出，`common_args` 以 `$ref` 复用）。
- **保存时校验**：`admin.py update_kbd_entry` 落库前用 `jsonschema.validate` 校验整段 `signals_json` 与逐条 `acquire.args`（按 `tool` 选 schema）。校验失败返回 422，杜绝幽灵字段/缺必填。
- **CI 防回归**：CI 中加入 job——(a) 对 `schemas/` 跑 `check-jsonschema`；(b) 对单测 fixtures 跑 validate，确保示例信号始终合法；(c) 当 `ACQUIRER_ARGS_SCHEMA` 变更时，提示/自动重新导出 schema（提供 `make gen-schemas` 脚本同步代码与契约文件）。
- **收益**：把 §6 的不变量从"文档约定"升级为"机器强制"，任何回归（如再次冒出顶层 `keyword`、漏写 `resource_keyword`）在 PR 阶段即被拦截。

**状态：✅ 已实现（2026-07-22）**
- **生成脚本**：`backend/scripts/gen-schemas.py`——从 `ACQUIRER_ARGS_SCHEMA` 单一来源导出；`common_args.timeout` 以 `$ref` 复用（杜绝各工具重复声明）；幂等（重复运行字节一致）。
- **契约文件入库**：`backend/shared/schemas/signals/`
  - `signal.v2.schema.json`：整体结构（draft-07，`$id` 绝对 URI，跨文件 `$ref` 经 `referencing.Registry` 解析）。
  - `acquirer_args/common_args.schema.json` + `acquirer_args/{tool}.schema.json`（11 个）。
- **加载与校验**：`backend/shared/schemas/signal_schema.py::validate_signals_json`（基于 `jsonschema` + `referencing`）；`admin.py update_kbd_entry` 落库前整段校验（含逐条 `acquire.args` 按 `tool` 选 schema），失败返回 **422**。
- **CI 防回归**：`ci.yml` 新增 `schema-contract` job（仅后端/CI 变更触发），调用 `scripts/ci/check_signal_schemas.py` 做三件事：①schema 自身合法；②fixtures 校验（合法样本必须通过、3 类非法样本——顶层 `keyword`/缺必填/幽灵字段——必须被拒）；③**漂移检测**（重新导出与入库文件逐一对齐，不一致则失败并提示 `make gen-schemas`）。
- **本地命令**：`make gen-schemas`（导出）、`make schema-check`（校验）。
- **运行时依赖**：`pyproject.toml` 增加 `jsonschema>=4.21.0`（与纯 Python `validate_acquire_args` 双保险，语义对齐）。

---

## 7. 迁移策略（直接切 v2 列形态 + 边界归一，已决议）

> 用户决议（2026-07-22）：**持久化直接切 v2 列形态**（非双写），且 producer/consumer 按推荐优先级一次性推进。据此采用**直接切 + 边界归一**策略，而非原 Expand-Contract 四阶段双写。

### 核心思路
- **存储直接切 v2**：`signals_json` 落库即 `{schema_version:2, signals:[...]}`（producer `_persist_signals` 经 `_signals_to_v2`；admin `update_kbd_entry` 经 `migrate_signal_document` 归一）。
- **读取边界归一（agent）/ 直出（前端）**：`kbd_from_dict`（agent）在读取时把 v2 还原为扁平信号（"`to_legacy_signal`"），下游 `kbd_differential` **零改动**；模型 `from_dict` 同时原生容错 v2。admin GET 自 2026-07-22 起**直出标准化 v2 文档**（不再归一）；前端 `KbdReviewView.vue` 进一步**原生 v2 对象化**——删除 `KeySignal`/`toLegacyFromV2` 适配层，直接基于 v2 渲染/编辑，回写发完整 v2 文档（admin `update_kbd_entry` 幂等归约）。数据契约彻底原生 v2，无任何归一/适配层。
- 因此"直接切"不会在部署窗口内破坏运行时——旧 pod 读到 v2 也能被边界归一消化（前提是先部署新代码再跑迁移）。

### 部署与迁移顺序（关键）
1. **先部署新代码**（kb-service + agent-service）：producer 写 v2、边界归一生效、校验生效。
2. **再跑存量迁移脚本** `scripts/migrate_signals_v1_to_v2.py --dsn <db>`（先 `--dry-run` 核对行数）。
3. 回采验证：对含信号的 KBD 发起诊断，断言命令含新关键词与 `-s failed`（复用事件文档验收标准）。
4. 如 v2 异常：回滚脚本不可用（列形态已切），依赖 DB 备份恢复；因边界归一保留 v1 解析能力，亦可临时回退 producer 写 v1。

### 落地状态（截至 2026-07-22）
- ✅ `shared/schemas/acquirer_args.py`：`COMMON_ARGS` + `ACQUIRER_ARGS_SCHEMA`（11 acquirer）+ `validate_acquire_args`。
- ✅ `shared/schemas/signal_migration.py`：`migrate_signal_document` / `to_legacy_signal` / `unwrap_signals`。
- ✅ producer（`extract_signals._persist_signals`）写 v2 + 校验。
- ✅ agent 边界（`kbd_model.kbd_from_dict`）归一 v2→legacy；模型 `from_dict` v2 容错。
- ✅ admin（`update_kbd_entry` 幂等归约+双重校验；GET 直出 v2 文档；前端原生 v2 对象化、回写 v2 文档；审核门解包 v2）。**后端契约完善**：`acquirer_args` 注册表为各 tool 增 `target`/`description` 可选字段、清空 QFK `required`；`signal.v2` JSON Schema 修正 `orchestrate.produces` 为对象数组、`match` 允许 `null`、放行 `_v1_legacy`——使历史真实数据（`acquire.args.target`/`_v1_legacy.description`/`produces` 数组等）能合法往返，不再 422。
- ✅ 迁移脚本 `scripts/migrate_signals_v1_to_v2.py`（DB/文件双模式，dry-run）。
- ✅ **§6.1 JSON Schema 契约入 CI（保存时强制）**：`gen-schemas.py` 导出契约、`signal_schema.validate_signals_json`（`jsonschema`+`referencing`）整段校验、`admin.py update_kbd_entry` 落库前 422 拒非法、CI `schema-contract` job、`Makefile` `gen-schemas`/`schema-check` 目标（详见 §6.1）。
- ✅ **前端"原生读 v2"对象化（2026-07-22）**：删除 `KeySignal`/`toLegacyFromV2` 适配层，前端 `KbdReviewView.vue` 直接基于 v2 文档渲染（producer/consumer 分组按 `acquire.tool`/provenance）、编辑（草稿即 v2 单条信号，v-model 直绑 `acquire.args.*`/`match.*`/`orchestrate.*`/`_v1_legacy.*`）、回写（发完整 v2 文档 `{schema_version, signals}`，admin 幂等归约）。零适配层、零信息损失，数据契约彻底原生 v2。
- ⬜ 模型本地单测受运行环境 Python 版本限制未跑（运行时 3.11+ 正常），但共享层 round-trip 与契约校验已用真实样本验证。

---

## 8. 风险与权衡

| 风险 | 缓解 |
|---|---|
| 全量迁移数据出错 | Phase 2 前保留 v1 字段双写；迁移脚本 dry-run + 逐条校验；DB 备份 |
| 读取方遗漏导致运行时取空 | 实施前全量 `grep` 读取点；单测覆盖每条字段映射；Phase 2 回采验证 |
| `from_dict` 旧路径（顶层 `keyword`）与新路径冲突 | Phase 0 即明确 v1/v2 路由，旧路径在 v2 下不读顶层 |
| 前端编辑器改造成本 | `MatcherEditor`/`ProducesEditor` 已按段编辑，绑定 `match`/`orchestrate` 即可；顶层 `keyword` 输入改为绑 `acquire.args.keyword` |
| 与"增量缺陷修复"重复劳动 | **不重复**：先落地事件文档的增量修复（删死字段），本 RFC 在其基础上做嵌套归位，字段更少、映射更清晰（见 §9） |

---

## 9. 与"增量缺陷修复"的关系（至关重要）

- **事件文档的增量修复**（待评审实施）：删顶层 `keyword`、删 `status`、qfk `acquirer_args.keyword`→`resource_keyword`、统一读取点、`is_failed` 多源。**它直接消除线上事故，且零全量迁移。**
- **本 RFC（重构）**：在增量修复之后，把剩余字段**按职责嵌套**。由于死字段已被增量修复删除，本 RFC 的迁移只需"归位剩余字段 + 加 `schema_version`"，工作量更小、风险更低。
- **结论**：两者是**先后接力**而非二选一。建议：先合入增量修复止血 → 再排期本 RFC 做结构性还债。本 RFC 不阻塞事故修复。

---

## 10. 开放问题（待评审决议）

1. `expected` / `match_mode` / `risk` / `extraction_method` 等"疑似未消费"字段如何处理？**已决议**：见 §2.7 审计——`expected`/`match_mode` 在 QFK 是被消费的，归位进 `match` 段并让 consumer 直读（消除 matcher.*→顶层 的搬移错位）；`risk`/`extraction_method` 确为只写不读，归位 `provenance` 并明确标注不进运行时分支，不做无谓删除（保留溯源价值）。
2. `schema_version` 放在每条信号元素还是整个 `signals_json` 数组级？**已决议：数组级**——`signals_json = { schema_version, signals:[...] }`，整批同版本迁移，降低逐元素版本判断复杂度（§7/§10.2）。
3. 是否同步为嵌套模型生成 JSON Schema 文件纳入 CI（保存时校验）？**已决议：是**——详见 §6.1 与 §11，作为"防回归"的长期契约；`ACQUIRER_ARGS_SCHEMA` 一并纳入校验。
4. 是否借重构机会统一 producer/consumer 的 `acquire.args` 结构？**已决议：是（彻底重构）**——统一 `acquire.args` 契约（producer/consumer 同构、公共字段 `common_args` 一处定义、各工具 `ACQUIRER_ARGS_SCHEMA` 单独注册并注释区分），见 §4.4。

---

## 11. 实施里程碑（建议）

| 里程碑 | 内容 | 依赖 |
|---|---|---|
| M0 | 事件文档增量修复合入（止血） | — |
| M1 | RFC 评审通过 + 开放问题决议 | M0 |
| M2 | Phase 0 锚点（`schema_version` + 路由桩） | M1 |
| M3 | Phase 1 双写（抽取+前端写 v2） | M2 |
| M4 | Phase 2 读切 v2 + 存量迁移 + 回采验证 | M3 |
| M5 | Phase 3 删旧 + `shared/schemas/acquirer_args.py` 注册 + JSON Schema 契约入 CI（保存时校验） + `scripts/migrate_signals_v1_to_v2.py` 收尾 | M4 | ✅ 已实现（2026-07-22） |

---

## 12. 参考文档

- `docs/solution/events/2026-07-22-QKV-QFK关键信号关键词字段失同步分析与修复方案.md`（事故分析 + 增量修复）
- `docs/solution/agent/02-架构设计/关键信号架构演进-从两方案批判到分层修正.md`（原子单元、Matcher 定型求值器、变量池黑板、统一占位符）
- `docs/solution/agent/02-架构设计/关键信号字段级分别抽取.md`（`KeySignal` 基类模型）
- `docs/solution/agent/02-架构设计/关键信号架构迁移指南.md`（`from_dict` 路由、命名映射）
- `docs/solution/agent/02-架构设计/关键信号抽取问题分析与修复方案.md`
- `docs/solution/agent/02-架构设计/QKV_QFK信号配置操作指南.md` / `QKV_QFK扩展性与配置易用性评估.md`（admin-ui 编辑器 `MatcherEditor`/`ProducesEditor`）
- 业界范式：Fowler *Expand-Contract (Parallel Change)*；12-Factor *Config*；Prometheus `expr+for+labels` / Datadog `query+threshold+aggr` / K8s probe（定型分离）；DDD 实体/值对象。
