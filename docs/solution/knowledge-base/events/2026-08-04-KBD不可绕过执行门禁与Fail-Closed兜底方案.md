---
status: proposed
category: solution
audience: developer
last_updated: 2026-08-04
owner: team
---

# KBD 不可绕过执行门禁与 Fail-Closed 兜底方案

## 背景与需求

### 讨论来源

本工作循环从“已人工复核”标签的事实准确性开始，逐步暴露出三个不能混淆的问题：

1. `provenance.needs_review` 是 AI Proposal 的质量提示，`review.require_human_confirm`
   是运行时授权策略，Approved Expert Revision 才是人工审核事实；任一字段都不能冒充另一个字段。
2. Candidate、Signal、Rejected Candidate 是知识生产门禁；`write_signal`、`not_exists`、
   `run_failed` 是 Rejected Candidate 的稳定工程分类，不是专家是否审核的状态。
3. 专家可以修正知识语义、采集器映射、Catalog 缺口和 Matcher，但专家确认不能让不满足机器执行契约的内容绕过执行器。

人工复核标签的现场证据、直接根因、Revision 单一事实源和完整状态矩阵独立归档在
[KBD 人工复核标签事实模型纠偏方案](2026-08-04-KBD人工复核标签事实模型纠偏方案.md)。
本方案以该结论为前提，继续回答“专家完成知识复核后，机器还必须保留哪些不可绕过的执行底线”。

在此基础上形成的新问题是：如果专家审核前由门禁保守分流、专家审核后由专家对知识语义作最终裁决，机器还必须保留哪些不可绕过的执行底线；这些底线能否保证 KBD 正常运行，以及失败时如何兜底。

### 目标

建立一个简单、可审计、不能被 UI、专家操作、历史数据或运行时降级绕过的边界：

- 专家负责知识语义兜底；
- 机器负责执行安全、计划完整性和结论可信度兜底；
- 无法证明安全时不执行，无法取得有效证据时不下确定性结论；
- 不承诺所有现场都执行成功，但禁止把失败伪装成诊断成功。

### 非目标

- 不增加 Candidate 的第四种领域状态；
- 不改变 `write_signal`、`not_exists`、`run_failed` 三类稳定拒绝原因；
- 不允许专家通过单篇 KBD 白名单、强制发布按钮或数据库标记绕过硬门禁；
- 不让 LLM 在运行时临时发明替代命令；
- 不承诺 KBD 一定执行成功或一定找到根因。

## 问题分析过程

### 1. 第一性原理：什么叫 KBD 正常运行

KBD 的“正常运行”至少同时包含三种性质：

| 性质 | 必须满足的事实 | 违反后的风险 |
|---|---|---|
| 执行安全 | 不产生未授权副作用，不跨错目标执行 | 现场被修改、错误节点受影响 |
| 执行真实 | 能形成确定计划，目标现场具备相应能力，参数和依赖完整 | 编译通过但现场无法运行，或查错对象 |
| 结论可信 | 只有成功、完整、可判定的观测才能支撑 Signal 和结论 | 执行失败或空输出被误判成信号成立 |

“只读、可编译、依赖可达”是必要条件，但不是充分条件。至少还缺少：

- 执行精确 Revision 的完整性；
- 目标现场能力和版本的可验证性；
- 目标、变量和作用域的确定性；
- 运行结果、输出提取和 Matcher 的可判定性；
- 从 Signal Outcome 到最终根因的证据闭环。

因此目标判定式为：

```text
Executable KBD Signal
=
精确已发布 Revision
AND 编译后真实执行向量可证明只读
AND 目标版本能力已验证
AND 参数、目标和变量全部确定
AND 依赖 DAG 闭合、可达、无环且 producer 成功
AND 执行输出完整、提取成功、Matcher 可确定求值
AND 证据闭环满足结论门禁
```

### 2. Candidate 三态与执行门禁的关系

Candidate 三态回答的是“这条知识是否具备成为 Signal 的资格”：

```text
Candidate
  ├─ write_signal ─► Rejected Candidate
  ├─ not_exists  ──► Rejected Candidate
  ├─ run_failed  ──► Rejected Candidate
  └─ 静态门禁通过 ► Signal
```

执行门禁回答的是“这个精确 Signal 在这个精确现场和时点能否安全执行并形成证据”。
二者不能合并：

- Rejected Candidate 经专家修改后，必须作为修改后的 Candidate 重新进入静态门禁；
- 专家不能把原 Rejected Candidate 原样强制提升为 Signal；
- 已经成为 Signal 也不代表在所有现场都可运行，运行时仍必须验证能力、目标、变量和依赖；
- 运行失败是精确 Revision、现场、节点和时点下的运行事实，不应永久改写历史 Proposal，也不应新增 Candidate 状态。

### 3. 三类拒绝原因的准确语义

| 原因 | 准确含义 | 不能推导出的结论 |
|---|---|---|
| `write_signal` | 编译后的真实执行向量违反 KBD 只读边界，或仍属于 solution phase | 不能仅凭自然语言含“启动/删除”等词判定；QKV 历史查询不因此变成写操作 |
| `not_exists` | 当前平台随附的能力契约没有登记或无法证明该命令存在 | 不能声称目标现场已经确认不存在；静态 Catalog 不是现场事实 |
| `run_failed` | Candidate 无法形成满足当前结构、参数、依赖、采集和 Matcher 契约的合法运行计划 | 不能把所有真实现场执行失败都永久固化为 Candidate 质量错误 |

为了保持三类稳定原因不膨胀，具体失败事实通过现有 `reason`、编译错误和运行审计表达。
若后续需要机器聚合，可增加独立的细分原因字段，但不得改变三态或让细分字段成为绕过开关。

### 4. 当前实现事实与局限

当前代码已经具备部分纵深防御：

- KBD Candidate、专家保存/发布和 Agent 运行复用共享只读判定；
- 未解析变量、目标 HOST 无法解析、依赖环、producer 不可达会阻断或使 KBD 不可执行；
- QKV 非零退出码不会进入解析；
- QFK Bridge/SSH 失败、完整输出缺失、输出超限或提取失败不会进入 Matcher；
- Matcher 无法确定时返回 inconclusive；
- `UNKNOWN`、`ERROR`、`BLOCKED`、`NOT_APPLICABLE` 不满足证据闭环；
- 有未决或不可执行候选时，不允许输出确定性根因。

但当前实现不能支持“保证所有现场正常运行”的结论：

- aCLI Catalog 是随代码发布并缓存的静态 JSON，不是目标现场实时事实；
- namespace Handler 可用不等于某个具体命令、参数和输出格式可用；
- 现有命令最小参数契约覆盖有限；
- 只读判断仍依赖已知程序、子命令和参数集合，不能证明所有未知命令绝对无副作用；
- 发布前编译通过不能消除 SSH、网络、权限、版本漂移、瞬时故障和现场数据缺失；
- 专家审核只能提高知识语义质量，不能证明未来每次现场执行成功。

## 方案（WHAT）

### 1. 保持三态知识门禁，增加四层执行闭环

不新增复杂状态机，将不可绕过边界按职责分为四层：

```text
知识生产层
  Candidate ──静态门禁──► Signal / Rejected Candidate
                              │
发布完整性层                  │ 精确 Approved + Published Revision
                              ▼
运行计划层
  只读 + 能力 + 目标/变量 + DAG + 资源边界
                              │
                              ▼
证据与结论层
  命令成功 + 输出完整 + Matcher 可判定 + Evidence Closure
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              Definitive           Fail Closed
```

### 2. 七项不可绕过的硬门禁

| 编号 | 硬门禁 | 机器必须证明的事实 | 失败结果 | 专家能否直接绕过 |
|---|---|---|---|---|
| G1 | Revision 完整性 | Agent 消费精确的 Approved、Published、Active Revision，并校验 ID、内容摘要和 freshness | `NOT_EXECUTABLE` | 不能，只能重新审核发布 |
| G2 | 真实执行向量只读 | 最终 program、subcommand、argv、flags、target、privilege 和 phase 可证明无写副作用 | `BLOCKED` | 不能，只能修改知识或能力契约后重验 |
| G3 | 可编译 | Schema、采集器、参数、变量引用、Matcher 和执行阶段能形成确定的结构化计划 | `NOT_EXECUTABLE` | 不能，只能修正后重编译 |
| G4 | 目标能力已验证 | 目标现场与版本支持对应 Handler、命令、参数和必要输出契约 | `CAPABILITY_UNVERIFIED` / `NOT_EXECUTABLE` | 不能，可补齐或修正能力声明后重验 |
| G5 | 目标、参数和变量确定 | 没有未解析变量，主机/容器/集群/时间范围唯一且不隐式回退 | `BLOCKED` | 不能 |
| G6 | 依赖闭合并实际成功 | DAG 无环、producer 静态可达，且本次运行成功产生类型正确、作用域正确的值 | `BLOCKED` / `ERROR` / `NOT_APPLICABLE` | 不能 |
| G7 | 结果与证据可判定 | 命令成功、输出完整、提取成功、Matcher 可确定求值，支撑结论的证据闭环 | `UNKNOWN` / `ERROR` / `INCONCLUSIVE` | 不能直接宣布根因成立 |

G2、G3、G5、G6 同时包含结构化参数和命令注入防御；运行计划还必须声明 timeout、
输出大小、并发、取消和有界重试限制。资源边界属于执行契约，不能用自由 Shell 或 LLM
临时解释替代。

### 3. 能力验证采用证据分级，不把 Catalog 当现场事实

能力证据优先级固定为：

```text
目标现场、目标版本的实时能力探测
  > 有有效期和版本绑定的能力快照
  > 随代码发布的版本化 Catalog
  > 模型或人工自然语言判断
```

能力结果必须区分：

| 结果 | 含义 | 执行决策 |
|---|---|---|
| `CAPABILITY_VERIFIED` | 当前证据足以证明目标支持该执行契约 | 继续后续门禁 |
| `CAPABILITY_ABSENT` | 目标现场明确返回不支持或不存在 | 不执行 |
| `CAPABILITY_UNVERIFIED` | 只有静态 Catalog、版本不匹配或证据不足 | 不执行，进入人工/能力治理 |
| `CAPABILITY_PROBE_FAILED` | 探测因网络、权限、Bridge 等原因失败 | 不执行，作为运行故障处理 |

上述结果属于运行能力审计，不新增 Candidate 状态。静态 Catalog 仍可用于生成和发布前
编译，但不能单独充当目标现场自动执行授权。

### 4. 专家兜底的准确边界

专家可以：

- 判断“有启动虚拟机失败任务”是只读历史查询语义；
- 编辑 Rejected Candidate 的采集器、参数、Matcher、依赖和 Evidence；
- 确认 Catalog 是漏登记还是模型生成了不存在的命令；
- 提交新的版本化命令能力契约和通用回归样本；
- 形成新的 Expert Revision 并重新审核发布。

专家不能：

- 原样强制提升 Rejected Candidate；
- 豁免真实写操作、未解析变量、目标歧义、依赖环或编译错误；
- 用“已人工复核”代替目标现场能力验证；
- 在命令失败、输出缺失或 Matcher 不确定时直接宣布 Signal 成立；
- 为单篇 KBD 添加绕过开关。

统一路径为：

```text
专家修改 Candidate / Catalog / Collector / Matcher / Contract
  → 重新编译和验证
  → 生成新的 Expert Revision
  → 审核并发布
  → 运行时再次通过全部硬门禁
```

### 5. Fail-Closed 兜底语义

KBD 兜底不是“无论如何给出答案”，而是“不能证明时停止，不能取证时不下结论”：

```text
安全性无法证明
  → BLOCKED

能力存在性无法证明
  → CAPABILITY_UNVERIFIED / NOT_EXECUTABLE

变量、目标或作用域未解析
  → BLOCKED

命令、Bridge、网络或权限失败
  → ERROR

输出不足、截断、解析失败或 Matcher 无法确定
  → UNKNOWN / INCONCLUSIVE

证据闭环未完成
  → 禁止 Definitive 根因，转人工排查
```

只允许对明确瞬时、幂等、已证明只读的错误做有界重试。替代命令必须预先声明、
同语义并单独通过全部安全和能力契约验证；禁止运行时由 LLM 自由生成替代命令。

### 6. 可以保证与不能保证的边界

在门禁实现正确、底层执行器和审计存储可信的前提下，系统应保证：

- 已知或无法证明安全的写操作不会自动执行；
- 未解析变量和错误目标不会被原样下发或静默回退；
- 编译失败、依赖不可达或 producer 失败时，下游不会继续执行；
- 非零退出、Bridge 失败、输出缺失不会被 Matcher 当作“不匹配”；
- `UNKNOWN`、`ERROR`、`BLOCKED`、`NOT_APPLICABLE` 不会变成有效根因证据；
- 只有精确 Approved、Published、Active Revision 被消费；
- 所有阻断、失败、降级和 fallback 都有结构化审计。

系统不能保证：

- 现场主机、SSH、网络、权限和 Bridge 永远正常；
- Catalog 永远最新，所有版本参数和输出格式完全一致；
- 所有未知命令绝对没有副作用；
- 专家知识一定正确；
- 命令每次都成功；
- KBD 一定找到最终根因。

对外承诺应限定为：

> KBD 不保证在所有现场成功运行；它保证在无法安全、可靠运行或无法形成完整证据时
> 停止执行并诚实返回不可执行、阻断、错误或结论不足，不把失败伪装成有效诊断结论。

## 决策依据（WHY）

### 为什么选择七项硬门禁

1. Revision 完整性防止“审核 A、执行 B”，是所有后续门禁成立的前提。
2. 只读只解决副作用风险，不能解决命令是否存在、目标是否正确和证据是否有效。
3. 可编译只证明平台能构造计划，不证明现场版本可以执行。
4. 静态依赖可达不等于 producer 本次运行成功，必须同时验证动态输出。
5. 命令成功不等于证据可判定，Parser、Matcher 和 Conclusion Gate 必须独立 Fail Closed。
6. 将这些事实分别审计，才能用 Proposal→Expert Diff 反哺 Prompt、Catalog、Collector 和 Matcher，避免把所有失败都归咎于模型。

### 对抗性审查

| 攻击或反例 | 如果只保留三条底线 | 本方案的防护 |
|---|---|---|
| 审核 Revision 5，执行被后改的 Revision 6 | 三条都可能通过 | G1 固定 revision、hash 和 freshness |
| 未知命令未命中写操作黑名单 | 被误认为只读 | G2 要求正向证明只读；未知副作用 Fail Closed |
| Catalog 有命令，现场旧版本没有 | 编译通过后现场失败 | G4 要求目标版本能力证据 |
| Catalog 缺命令但现场实际存在 | 被永久标成模型错误 | `not_exists` 只表示当前契约未证实，专家可补 Catalog 后重验 |
| HOST 解析失败后回退当前节点 | 命令成功但证据来自错误节点 | G5 禁止隐式目标回退 |
| producer 在图上存在但执行返回空值 | 静态依赖可达，consumer 带空值运行 | G6 同时要求 producer 本次成功且输出有效 |
| 命令失败，`not` Matcher 对空输出返回 true | 生成假阳性 Signal | G7 在执行失败时禁止进入 Matcher |
| 输出被截断后仍命中局部关键字 | 证据看似成立但上下文不完整 | G7 要求完整输出或显式 UNKNOWN |
| 专家确认一条真实写命令 | 人工标签被当作执行豁免 | 专家只能修改后重验，不能绕过 G2 |
| LLM 临时生成“等价”替代命令 | 替代命令可能越权或语义漂移 | fallback 必须预先声明并独立验契约 |

### 为什么不选其他方案

- 不让专家审核直接覆盖所有门禁：人工审核不能证明机器运行计划、现场能力和本次输出事实。
- 不只保留“只读、可编译、依赖可达”三个布尔值：会遗漏 Revision、目标能力、作用域和证据门禁。
- 不把静态 Catalog 当作实时能力中心：它无法表达现场版本、节点状态和探测失败。
- 不把所有失败统一成 `run_failed`：Candidate 静态质量与运行时故障的生命周期不同，混合后无法准确反哺。
- 不增加更多 Candidate 状态：运行能力和 Outcome 已有独立阶段，增加状态会混淆知识与执行事实。
- 不追求“失败时也必须给根因”：这会鼓励系统用猜测替代证据，直接破坏 KBD 的可信度。

## 最终方案确认项

本事件文档只归档拟议方案，未授权修改业务代码。实施前需要产品/架构负责人明确确认以下事项：

1. 人工复核标签按独立三态方案处理：不需复核不显示，需要复核时按专家是否保存当前内容显示黄色或绿色；不参与执行门禁；
2. 是否确认七项硬门禁均不可通过专家按钮、单篇 KBD 配置或历史数据兼容逻辑绕过；
3. 是否确认静态 Catalog 只用于生成与编译证据，目标现场自动执行最终需要版本绑定的能力证据；
4. 是否确认专家只能通过“修改后重新编译、审核、发布”兜底知识语义，不能直接强制提升 Rejected Candidate；
5. 是否确认运行失败、能力未证实和证据不足分别进入运行审计，不扩充 Candidate 三态；
6. 是否确认对外承诺是“失败时安全停止且不伪造结论”，而不是“保证所有 KBD 在所有现场执行成功”；
7. 是否确认存量已发布 Revision 在新门禁启用后也必须在每次运行前重新验证，不享有永久豁免；
8. 是否确认 rollout 可以先做只读审计和差异报表，但一旦硬门禁进入 enforce 状态，就不保留 bypass 开关。

## 影响范围

确认并实施后，预计需要更新：

- Shared Signal 安全与编译契约；
- kb-service Candidate、专家保存、审核和发布门禁；
- agent-service Revision freshness、Capability Discovery、执行和 Conclusion Gate；
- aCLI Catalog 的版本化契约及现场能力证据；
- 运行审计原因码和管理端展示；
- KBD 专项单元、契约、集成、对抗性和真实环境回归；
- 现行全量文档 `solution/knowledge-base/知识库设计.md`、
  `solution/agent/02-架构设计/agent设计.md`、`task/knowledge-base/知识库任务.md`；
- 若新增或变更 REST API、数据库字段，再同步更新接口设计和数据库设计；
- 工作循环完成后更新 `docs/README.md` 第一屏。

当前阶段不更新上述现行全量文档，因为它们必须反映已实现事实，不能把待确认目标写成现状。

## 验收标准

### 方案验收

- 产品/架构负责人逐项确认“最终方案确认项”；
- 方案不再使用“保证正常运行”这一不可验证承诺；
- 专家权限、Candidate 三态、运行 Outcome 和 Conclusion Gate 的职责无重叠；
- 当前实现事实和目标方案有明确边界。

### 实施验收

- G1～G7 在抽取、专家保存、发布和 Agent 运行各入口没有旁路；
- 静态 Catalog 缺失、现场能力缺失、探测失败三种事实不会互相冒充；
- 未知副作用、未解析变量、错误目标、依赖失败、非零退出、输出截断、Parser 失败、Matcher inconclusive 均有反例测试；
- 命令失败或输出不足时，`not`、`expected=false` 等 Matcher 不能产生假阳性；
- 只有证据闭环的精确 Revision 可以进入 Definitive Conclusion；
- 存量已发布 Revision 运行前同样经过硬门禁；
- 失败审计能关联 KBD ID、Revision、hash、目标、能力证据、执行阶段、原因和 Outcome；
- 所有专项测试、契约测试、文档治理和 CI 通过后才允许合并。
