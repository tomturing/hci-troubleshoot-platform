---
status: active
category: solution
audience: developer
last_updated: 2026-07-26
owner: team
---

# KBD 主动诊断信号调度与证据闭环算法设计

## 1. 决策摘要

S0 已确认故障分类后，S1 必须把该分类的版本化 KnowledgeSnapshot 作为候选全集，不能再用用户的模糊原话调用 `/api/kb/route`、embedding、FTS 或 top-K 筛掉候选。进入 CDD 后，系统不让模型生成命令、判断信号或撰写根因，而是执行以下确定性流水线：

```text
Category KnowledgeSnapshot
  -> SignalPlanCompiler
  -> Acquisition Graph
  -> Active Diagnostic Scheduler
  -> Constrained Executor
  -> Deterministic Signal Evaluator
  -> Candidate State Reducer
  -> Conclusion Gate
```

调度器只决定“下一项执行哪个已发布 acquisition”，不决定事实和结论。工具调用只能来自 KBD revision 内的 `signal_id/acquire`；同一份采集结果可以复用，但每篇 KBD 的 matcher 必须独立求值。只有结论门禁返回 `DEFINITIVE` 才允许写入 S4。

## 2. 第一性原理

诊断的目标不是生成一个听起来合理的答案，而是在有限成本和安全约束下区分一组可证伪假设。由此得到六条不可弱化的约束：

1. **候选完整性**：分类内已发布且可执行的 KBD 构成候选全集；排序不能改变成员资格。
2. **动作有来源**：任何现场动作都能追溯到 `snapshot_id + kbd_id + revision + signal_id`，Agent 没有自由命令通道。
3. **事实与解释分离**：工具产生 observation，确定性 matcher 产生 signal outcome，状态机产生候选结论；LLM 不参与结论关键路径。
4. **未知不等于否定**：超时、无权限、缺变量和 matcher 无法定值分别保持 `ERROR/BLOCKED/UNKNOWN`，不得转换为 `FAIL`。
5. **支持需要完备证据**：一篇 KBD 的决策表达式被 PASS 证据满足才是 `SUPPORTED`；候选少或相似度高不是证据。
6. **停止必须保持正确性**：只能在继续执行已不可能改变结论等级时停止，不能因“只剩一个候选”或“已经找到一个支持项”提前结束。

## 3. 旧算法为什么不成立

### 3.1 旧贪心覆盖率

旧算法按工具名或步骤覆盖候选数量选择命令，混淆了三个不同对象：

```text
tool type != acquisition != KBD signal
```

`qfk_system` 只是工具类型；`qfk_system + lsof + VM + HOST` 才是一项 acquisition；`KBD27123/rev10/sig_002` 是对采集结果的独立解释契约。只按工具名复用会把不同参数、目标和作用域的动作误判为同一证据，也无法表达同一采集结果被不同 matcher 得出不同结果。

纯覆盖率还偏爱“许多 KBD 都使用、但无法区分它们”的公共信号，忽略能快速排除候选的高判别信号。它优化的是步骤数量，不是后验不确定性。

### 3.2 候选数量 early-stop

“剩一个候选即停止”把排除其他候选误当成支持剩余候选。候选 KBD 仍必须完成自身 required signals。类似地，“已有一个命中即停止”会掩盖仍可能成立的并发根因，也会在其他候选因工具错误未完成时错误输出 definitive 根因。

### 3.3 LLM matcher

LLM 输出不具备同输入同输出、封闭状态和可形式验证性。它可能把工具错误解释成故障现象、补全输出中不存在的事实，或者受 KBD 根因文本诱导。因此 LLM 只能用于 S0 语言理解和非结论性摘要；关键 matcher 必须是发布时校验过的 keyword、regex、state、threshold、json_path、exists 或组合表达式。

## 4. 领域模型

### 4.1 SignalRef

```text
signal_ref_id = kbd_id / revision / signal_id
```

至少包含：

- `kbd_id/support_id/revision/signal_id`
- `required_for_support`
- `failure_effect`: `reject | no_support`
- `requires/produces`
- `matcher`
- `phase`: `diagnostic | context | solution`

兼容旧 KBD 的安全默认值：diagnostic signal 默认为 required 且 `failure_effect=reject`；context/optional 默认为非 required；solution/remediation 永不自动执行。

### 4.2 Acquisition

Acquisition 是一次真实世界采集，不是 matcher。其规范身份为：

```text
acquisition_key = hash(
  tool + canonical_resolved_args + target + scope
  + tool_version + policy_version
)
```

模板编译阶段先生成稳定的 template key；变量就绪后解析参数并生成运行期 key。只有运行期 key 完全相同才允许共享一次执行。执行有唯一 `exec_id`，每个关联 signal 另有唯一 `evaluation_id`，从而避免“复用了结果却伪造多次真实执行”。

### 4.3 SignalPlan

SignalPlan 是快照上的不可变执行计划，包含：

- `plan_id/snapshot_id/category_id`
- 全部候选及 revision
- acquisition 节点和 signal ref 边
- `requires -> produces` 变量依赖图
- 工具风险、成本、延迟和策略版本
- 编译告警（缺工具、循环依赖、非法 matcher、写操作）

计划编译失败的 KBD 不能静默丢弃，必须进入 `NOT_EXECUTABLE` 并保留原因。

## 5. 状态机

### 5.1 信号状态

```text
NOT_RUN -> PASS | FAIL | UNKNOWN | ERROR | BLOCKED
```

- `PASS`：确定性 matcher 满足。
- `FAIL`：采集成功且确定性 matcher 明确不满足。
- `UNKNOWN`：有输出但 matcher 无法定值，或缺少合法 matcher。
- `ERROR`：采集通道、工具或协议失败。
- `BLOCKED`：缺变量、权限、安全确认或策略禁止。

只有 `PASS/FAIL` 是事实判定；后三者均不得确认或排除根因。

### 5.2 候选状态

```text
CANDIDATE
  -> SUPPORTED
  -> REJECTED
  -> INCONCLUSIVE
  -> NOT_EXECUTABLE
```

- `SUPPORTED`：决策表达式由 required PASS 完整满足。
- `REJECTED`：至少一个具有 `failure_effect=reject` 的 required signal 明确 FAIL，或决策表达式明确为假。
- `INCONCLUSIVE`：执行结束时仍有 required signal 为 UNKNOWN/ERROR/BLOCKED/NOT_RUN。
- `NOT_EXECUTABLE`：计划本身无合法诊断路径或全部路径被安全策略永久阻断。

首期对旧数据采用 required signals 的 AND 语义；后续 schema 支持显式布尔表达式 `A AND (B OR C)`，但仍由确定性表达式求值器执行。

### 5.3 结论等级

| 等级 | 条件 | 是否进入 S4 |
|---|---|---|
| `DEFINITIVE` | 至少一篇 SUPPORTED，且其余均为 SUPPORTED/REJECTED | 是 |
| `PARTIAL` | 至少一篇 SUPPORTED，但仍有 INCONCLUSIVE/NOT_EXECUTABLE | 否 |
| `INCONCLUSIVE` | 无 SUPPORTED，且存在未决或不可执行候选 | 否 |
| `NO_MATCH` | 所有可执行候选均被明确 REJECTED | 否 |

多篇 KBD 可以同时 SUPPORTED；这代表并发根因或同一事实支持多个知识条目，不应被算法强行压成一个。

## 6. 主动信号调度算法

### 6.1 可执行集

每轮仅考虑：

- 至少关联一篇 `CANDIDATE`；
- 依赖变量已由环境或 producer 提供；
- 非 solution/remediation；
- 未因风险策略或人工确认阻断；
- 尚未执行，或已有可复用 observation 但仍有未求值 signal。

候选一旦 `REJECTED`，其独占 acquisition 立即取消；若 acquisition 仍被其他活动候选需要，则保留。这是基于证明的短路，不是基于候选数量的 early-stop。

### 6.2 优先级效用函数

```text
utility(a) =
    wd * discrimination(a)
  + wc * required_coverage(a)
  + wu * unlock_value(a)
  + wr * reuse_value(a)
  - cc * normalized_cost(a)
  - cl * normalized_latency(a)
  - cr * risk_penalty(a)
```

- `discrimination`：同一 observation 上不同 matcher 能把多少活动候选划分为不同结果；matcher 越多样、涉及候选越多，值越高。
- `required_coverage`：本次可求值的 required signal 数量。
- `unlock_value`：produces 的变量能解锁多少尚未可执行的下游 acquisition。
- `reuse_value`：一次采集可供多少独立 signal 求值。
- `cost/latency/risk`：来自发布契约；缺省使用保守静态值，写操作风险为禁止而不是高成本。

默认权重首先保证判别力和解锁能力，再优化复用与成本。权重只能改变合法执行顺序，不能改变 matcher outcome、候选归约规则或最终结论。相同效用使用稳定排序：

```text
risk ASC, cost ASC, latency ASC, acquisition_template_key ASC
```

因此候选输入顺序不会影响计划、执行顺序和结论。

### 6.3 正确停止条件

满足任一条件才可停止：

1. 全部候选达到终态；
2. 至少一篇 SUPPORTED，且其他候选均为 SUPPORTED 或 REJECTED；
3. 不再存在可执行 acquisition，剩余候选归约为 INCONCLUSIVE/NOT_EXECUTABLE；
4. 达到显式预算、安全或用户边界，未完成候选归约为 INCONCLUSIVE。

不允许的停止条件：只剩一个候选、达到 top-K、相似度超过阈值、LLM 表示“足够确定”、已有第一篇支持项。

## 7. 执行契约与防幻觉边界

Agent 只能提交以下选择：

```json
{
  "plan_id": "...",
  "acquisition_template_key": "..."
}
```

执行器从已签名计划读取工具和参数模板，解析变量，重新计算 runtime acquisition key，并校验 revision、工具白名单、只读策略和目标作用域。请求中出现自由 `command`、未知 signal_id、revision 漂移或未解析占位符时直接 BLOCKED。

工具原始输出以 hash、时间、目标和 `exec_id` 存证；matcher 只读取该 observation。报告生成器只能引用：

- Conclusion Gate 输出的 KBD ID；
- KBD 原始 root_cause/solution；
- 对应 `evaluation_id -> exec_id -> observation` 证据链。

它不能生成新命令、新输出、新根因或把错误消息改写成现场事实。

## 8. 27123 多 KBD 示例

S0 确认 `虚拟机-003 虚拟机开机失败` 后，S1 加载分类全部 KBD。案例 27123 的图为：

```text
sig_001/qkv_task
  produces VM, HOST
       |             \
       v              v
sig_002/qfk_system   sig_003/qfk_system
lsof {{VM}}          ps
matcher vm-disk      matcher ClwDRDBClient
```

已知运行事实为 VM `4359974862144`、HOST `SVR_aCloud_668`。调度器会因 `sig_001` 的 unlock value 优先执行 producer，随后解析出两项 KBD 原生采集：

```text
acli system lsof 4359974862144
acli system ps
```

若两条 required matcher 均 PASS，且分类内其他 KBD 已 SUPPORTED/REJECTED，则 27123 可形成 DEFINITIVE；若 terminal bridge 超时，则相应信号是 ERROR、27123 是 INCONCLUSIVE，不能输出其根因。若另一篇 KBD 尚因权限 BLOCKED，即使 27123 已支持也只能输出 PARTIAL，并明确展示未决候选。

## 9. 可观测性和审计字段

每次诊断至少记录：

- `snapshot_id/plan_id/policy_version/category_id`
- 每轮 scheduler 的候选集合、可执行集、各效用分量和选择理由
- `acquisition_template_key/runtime_key/exec_id`
- `signal_ref_id/evaluation_id/outcome/matcher_version`
- 候选状态迁移及触发证据
- conclusion level、未决候选和停止原因

核心指标：分类 inventory 完整率、计划编译失败率、采集复用率、required signal 完成率、ERROR/BLOCKED 比例、PARTIAL 比例、人工升级成功率、错误 S4 写入数（目标必须为 0）。

## 10. 实施分层

### P0：正确性内核

- 引入 SignalPlan、Acquisition、SignalEvaluation、CandidateAssessment、ConclusionDecision。
- 按 runtime acquisition key 去重真实执行，拆分 exec_id/evaluation_id。
- 引入候选状态机、FAIL 短路和四级 Conclusion Gate。
- 仅 DEFINITIVE 写入 S4；PARTIAL/INCONCLUSIVE 显式升级人工。

### P1：主动调度与解释

- 实现 discrimination/coverage/unlock/reuse/cost/risk 效用函数。
- 稳定 tie-break，输出每轮选择理由。
- 加入共享 acquisition 保留和被拒候选独占任务取消。

### P2：知识契约增强

- 发布时强制 signal_id、matcher、requires/produces、风险和成本校验。
- 支持布尔 decision expression 及静态可满足性检查。
- SignalPlan 持久化、签名和回放。

## 11. 验收不变量

必须以自动化测试证明：

1. producer 总在依赖它的 consumer 之前执行，且高 unlock value 优先。
2. 高判别 acquisition 优先于仅覆盖多但 matcher 同质的 acquisition。
3. 完全相同 acquisition 只执行一次，关联 signal 独立求值且 evaluation_id 不同。
4. required FAIL 立即 REJECTED 并取消该 KBD 独占后续动作；共享动作仍为其他候选保留。
5. ERROR/UNKNOWN/BLOCKED 既不能支持也不能排除候选。
6. optional signal 不阻止支持。
7. SUPPORTED + 未决候选只能得到 PARTIAL，不能写 S4。
8. SUPPORTED + 其他全 REJECTED 才得到 DEFINITIVE。
9. 候选输入顺序不改变 schedule 和 conclusion。
10. 成本/风险权重可改变顺序，但不能改变相同 observation 下的最终结论。
11. 27123 golden case 仍产生三次 signal evaluation，且两个 QFK 命令只能来自 sig_002/sig_003。
12. terminal bridge 错误使 27123 保持 INCONCLUSIVE，并展示案例链接与错误证据。

## 12. 最终边界

该算法不是用另一个启发式替代旧贪心，而是把“优化执行顺序”和“证明诊断结论”彻底分离：调度层允许启发式优化，正确性内核不允许启发式或 LLM 参与。即使调度权重配置不佳，最坏结果也只能是多执行一些合法信号或更晚得到结论，不能产生无证据根因。
