# Agent 对 KB 与信号高依赖的局限性分析与改进方向

> 文档版本：V1.1
> 创建日期：2026-08-20
> **V1.1 变更（2026-08-20）**：新增第五章「信号生产成本分析与降本方向」——针对"写信号像写伪代码、生产成本太高"的一线痛点，给出成本分解与 P0/P1/P2 降本方向；原附录顺延为第六章
> 文档性质：**架构分析 / 讨论稿**（改进方向为提案，未经评审，不代表已采纳的设计决策）
> 基于代码基线：main 分支（2026-08-20 快照，所有结论均经代码核实）
> 关联文档：
> - [在线与离线诊断模式-KBD信号利用路径对比](../在线与离线诊断模式-KBD信号利用路径对比.md)
> - [关键信号架构落地设计](../关键信号架构落地设计.md)
> - [虚拟机控制台视觉生产者信号设计与需求](../虚拟机控制台视觉生产者信号设计与需求.md)
> - [离线诊断模式业务设计_V3.1](../离线诊断模式业务设计_V3.1.md)
> - [KBD不可绕过执行门禁与Fail-Closed兜底方案](../events/2026-08-04-KBD不可绕过执行门禁与Fail-Closed兜底方案.md)
> - [QKV_QFK扩展性与配置易用性评估](QKV_QFK扩展性与配置易用性评估.md)（信号模板提案否决依据，见 §5.4）
> - [关键信号统一解析运行时与Resolver分层方案](../events/2026-08-07-关键信号统一解析运行时与Resolver分层方案.md)（SignalIntent 降本解药，见 §5.3）

---

## 摘要

本文回答一个架构性质疑：**平台的在线与离线诊断模式是否过度依赖 KB（KBD/SOP）与 QKV/QFK 信号的质量，以至于能力被局限？**

结论分三层：

1. **事实成立**：经代码核实，当前 agent 的能力上限基本等于 KB 与信号体系的覆盖上限。KB 未命中即升级人工，信号缺失即 UNKNOWN/Insufficient，且"机制推理"自由降级路径已成死代码——比文档呈现的还要封闭。
2. **一半是 deliberate trade-off（有意的权衡）**：在客户生产 HCI 集群上排障，fail-closed + KB 锚定换取确定性、可审计性与防幻觉，这个选择本身有充分理由，不应推翻。
3. **真正的局限不在"依赖 KB"，而在三件事**：KB 未命中场景零辅助且零沉淀（升级人工 = 纯损耗）；观察空间（QKV 白名单）扩展成本高，跟不上故障模式演化；KB 质量抖动无运行时度量，直接传导为诊断能力抖动。

改进方向的优先级排序：**知识回流闭环 > 未命中率度量 > advisory 探索层 > 轻量观察通道 > 离线证据放宽**。核心主张：不放开执行自由度，而是把"转人工"这个终点改造成知识回流的起点。

**V1.1 补充**：KB 覆盖缺口收敛慢还有另一面——**信号生产成本太高**（"写信号像写伪代码"：单条 40~100 行、20+ 字段、无跨 KBD 复用、无自助 dry-run）。回流慢（3.1）+ 生产贵（第五章）是同一问题的两面，降本方向见第五章。

---

## 一、调研结论：代码实际行为

### 1.1 在线诊断：KB 未命中时没有探索路径，直接升级人工

主链路（`agent-service/app/adapters/agents/htp/investigation_agent.py::process()`）以 S0 确认的 `category_id` 为权威边界，经 `kb_client.get_category_playbooks()` 加载版本固定快照，本地判定 `track = sop | kbd | human_escalation`（T-AGT-23 甚至用 `sop_resume_context` 绕过重路由防漂移）。`kb-service` 的 `GET /api/kb/route` 三轨路由（SOP → KBD → `human_escalation`）只是兜底查询入口。

Fallback 行为：

| 场景 | 实际行为 |
|------|---------|
| 分类下无 SOP 且无 KBD（或 KBD 均未通过自动执行契约） | 输出"系统不会生成知识库外的命令或根因"，yield `AgentEscalation` 直接转人工 |
| KB 服务不可达（inventory=None） | 同样升级人工 |
| "机制推理"自由降级（`_process_fallback_mode` / `_build_fallback_prompt`，prompt 模板 `s4_fallback_v1`） | **死代码**——commit `4e1d2474` 改为 S0 分类驱动后废弃，无任何调用方 |

即：**当前不存在假设驱动的开放探索路径**。唯一的"假设生成"机制是 KBD 候选集本身（每个 KBD 作为 S2 hypothesis 插入 `diagnostic_item`）。

### 1.2 KBD/SOP 命中后：门禁卡在变量来源与工具风险，而非逐步锁死

两条轨道的刚性程度不同：

**SOP 轨道（ReactEngine 模式）——结构化但不僵死**。`react_engine.py` + `sop_tools.py::SopToolExecutor` 下，LLM 同一轮可同时调 SOP 导航工具（`get_sop_node` / `sop_advance` / `sop_request_variable`）与诊断工具（acli_exec / bash_exec，prompt 明示"可同时使用诊断工具收集证据"）。三重约束：

- **变量来源门禁**（`SopToolExecutor._check_variable_source_gate`）：SOP 声明的 `user_input/user_confirm/env_*` 变量未就绪前，bash_exec/acli_exec 调用被 block（`sop_variable_gate_blocked`），LLM 不得用命令替代声明来源；
- **工具白名单**：`tool_definition` 表为 SSOT（`tool_registry.load_tool_registry`，risk 1/2/3 → auto/confirm/block）；bash/acli 命令再由 `acli/classifier.py` 的 `classify_acli/classify_bash` 动态定级，**未知命令 fail-closed** 到确认/阻断；
- 写操作工具幂等跳过（T-AGT-23），risk ≥ 2 需用户确认。

**KBD 轨道（CDD 模式）——严格结构化**。`kbd_differential.py::KBDDiagnostic` 编译信号 DAG（`shared/cdd/plan_compiler.py`），`_tool_contract_checker` 在线校验可执行性后执行，matcher 判定，结论由 `shared/cdd/conclusion_gate.py::decide_conclusion`（DEFINITIVE / PARTIAL / INCONCLUSIVE / NO_MATCH）门控；**非 DEFINITIVE 照样 `AgentEscalation`**。设计文档明确"不让 LLM 在运行时临时发明替代命令"。

**变量获取策略的置信序**（`shared/models/information.py`）：`tool_exec > env_inject > kb_search > user_input > llm_inference`，其中 `llm_inference` 默认置信 0.5、结论须标"待验证"——系统已经把"LLM 自由推断"放到了最低信任级。

### 1.3 离线诊断：信号缺失降级为 UNKNOWN + 一次定向补采

**采集端**（Go，`diagnosis-service/offline-collector/collect.go`）：单项失败记 `executionRow{Status:"failed"}` 后继续执行，整包带 failed 状态进证据包并提示"必要时请补采"，不中断。

**分析端**（`diagnosis-service/app/services/offline_analysis_service.py`）：

- `_evaluate_signal`：无可用证据 → 信号态 **UNKNOWN**（附原因："未配置 Offline Signal Mapping"或"所需离线证据缺失、失败或不可读"），不报错；UNKNOWN 不作反向证据；
- `_calculate_assessment`：产出 completeness_score 与 `missing_evidence` 清单；mandatory 缺失则 `non_diagnosable_scope=["confirmed_root_cause"]`、`ready_for_diagnosis=False`；
- `_conclusion`：未就绪 → `Insufficient`（confidence ≤ 0.39）；复用在线 CDD 门禁，只有 DEFINITIVE + mandatory 全齐 + 唯一 SUPPORTED 才给 `Confirmed`；
- **替代路径仅两条**：`offline_signal_collector_mapping`（在线工具→离线 collector 的已审批映射）和 `_create_supplement` 生成的**一次**定向补采计划。没有自由发挥的证据采集。

设计原则（《离线诊断模式业务设计_V3.1》）："离线数据不足时明确缺失证据并转工程师人工补充，不输出确定性根因"。

### 1.4 不存在运行时自学习闭环

- `case-service` 的 `close_case` 仅改工单状态，**工单关闭不触发任何知识写入**；
- 实际的知识生产是**离线半自动流水线**（`data-pipeline/`）：Support Portal 案例 → fetch → import → vision → classify → extract-signals → review-signals → **Admin 专家复核/修改/验证** → publish → Agent 消费。README 明确"专家复核是当前质量兜底，不被伪装成自动阶段"；agent-service 只消费已发布知识，不生成或静默修复 Proposal；
- 运行时反馈仅限**命中计数**（`increment_sop_hit` / `increment_kbd_hit` / `decrement_kbd_hit`，`kb-service/app/routes/hits.py`），是使用热度，不是知识沉淀；
- `fact` 表 / `ClaimVerification` 的角色是**运行时防幻觉，不是学习**：`FactStore`（`agent-service/app/services/fact_store.py`）按来源置信度存工单级事实并做多源冲突检测；`ClaimVerification`（`shared/models/reliability.py`）强制 LLM 把 S4 结论逐条标为 supported / contradicted / insufficient_evidence，未达标记 `agent_unsupported_claim_total` 指标。

### 1.5 QKV 信号体系：封闭白名单，扩展成本高

- 全部 acquirer 共 **11 个**（`ACQUIRER_CATALOG`，`kb-service/app/routes/extract_signals.py`）：生产者 `FRONTEND_TOOLS = {qkv_alert, qkv_task, qkv_dialog}` + 条件生产者 `CONDITIONAL_PRODUCERS = {qkv_vm_console}` + 7 个 `qfk_*` 消费者（`shared/schemas/acquirer_args.py`，`additionalProperties:false` 禁幽灵字段）；
- **发布门禁**（`signal_schema.py::validate_kbd_publishable_signals_json`）：每个 KBD 必须至少 1 条生产者信号、signal id 稳定唯一、变量依赖 DAG 校验，再经专家盖章 `certify_publishable_signals_json`；流水线 Stage 6 全量 Signal Review 输出 PASS / NEEDS_REVIEW / BLOCKED；
- **新增一个生产者的成本**可参照 `qkv_vm_console` 落地实例：需同时改共享 Schema + JSON Schema、Resolution Runtime resolver、Tool Registry、agent-service 专用适配器、terminal_bridge 固定操作、离线 Go collector 执行器、diagnosis-service、制品服务、kb-service 校验、Admin UI 共约 10 个组件，另加策略开关与离线 collector mapping。不是插件式注册，而是**跨服务契约变更 + 多层静态门禁 + 专家审核**。

### 1.6 一句话总结能力边界

该系统是典型的 **"KB-first + fail-closed"架构**：在线 SOP/KBD 均未命中时直接升级人工，明确拒绝生成知识库外的命令与根因；KBD 轨道全程受信号 DAG、matcher 与结论门禁约束；离线模式信号缺失降级为 UNKNOWN + 缺失清单 + 一次定向补采；无运行时自学习，知识回流依赖离线流水线的人工复核。**agent 的能力上限 ≈ KB 与信号体系的覆盖上限，未覆盖场景的设计意图是"安全地承认不知道"，而非自主探索。**

---

## 二、为什么这个权衡是合理的（不应轻易推翻的部分）

在客户生产环境的 HCI 集群上排障，"重依赖 KB"有充分的领域理由：

1. **错误代价不对称**。漏诊（转人工）的成本远低于误诊（在客户集群执行错误命令）。fail-closed 方向正确。
2. **离线模式天然没有人在环**。不可能做开放探索，只能"安全地承认不知道" + 给出可行动的缺失清单。
3. **可审计性是 To B 硬需求**。每个结论都能追溯到 KBD 条目 + 证据链（fact/claim_evidence_link/Conclusion Gate），交付与复盘都依赖这一点。
4. **项目自身的经验教训**（见 AGENTS.md「Skill 调用失效根因分析」）：*"当前架构是 Trust-based 而非 Enforce-based，任何只靠 Prompt 建立的行为规范，都会在模型版本切换、上下文压缩、存在低阻力替代路径时失效"*。把行为边界做进代码门禁而非依赖 LLM"自由但谨慎"，是被实际故障验证过的选择。
5. SOP 轨道实际保留了节点内的自由度（可自由使用诊断工具收集证据），门禁卡在**变量来源**与**工具风险等级**上——并非逐步锁死，设计粒度是合理的。

**因此本文的立场不是"放开执行自由度"，而是在保留 fail-closed 执行门禁的前提下，补齐覆盖缺口与回流机制。**

---

## 三、真正的局限

### 3.1 升级人工 = 纯损耗，没有沉淀（最大缺口）

KB 未命中 → 转人工 → 人工解决 → 工单关闭，**不产生任何知识回流**。每一次 KB 未命中本应是最高价值的知识候选（它精确标记了覆盖缺口 + 附带了真实故障现象 + 有工程师的解决过程），现在全部白白丢掉。KB 的边界因此是**静态的、由离线流水线单方面决定的**，覆盖缺口的收敛速度完全取决于 Support Portal 存量案例的导入进度。

### 3.2 观察空间扩展太慢

QKV 白名单 + 新增生产者约 10 组件契约改造的成本，意味着 agent 的"眼睛"跟不上故障模式的演化速度。`qkv_vm_console` 补"Guest OS 内部现象（黑屏/蓝屏/Panic）"这一个观察洞就花了 P0 双线的工程量。告警/任务/弹框/控制台之外仍存在大量观察盲区（如性能曲线、拓扑变更史、补丁历史），每补一个都是同等量级的工程。

### 3.3 KB 质量抖动无运行时度量，直接传导为能力抖动

KBD 解析/信号抽取历史上修过大量 bug（贪婪解析、截图丢失、嵌套列表丢弃、SQL 拼接……见 AGENTS.md 多条 PR 记录）。专家复核是唯一兜底，但：

- 已发布 KBD 的质量退化（如 HCI 版本升级导致命令输出格式变化 → matcher 大面积 UNSUPPORTED）**系统自身感知不到**；
- 现有 hit 计数只反映热度，不反映"命中但诊断失败/降级"的质量信号；
- 诊断成功率按 KBD 维度的归因度量尚未建立。

### 3.4 长尾故障零辅助

即使是"只读命令、不执行、纯建议"的低风险辅助推理也不存在。工程师面对未知故障时得不到系统的任何假设建议，平台的价值在长尾场景归零——而长尾恰恰是排障工作量占比最高的部分。

---

## 四、改进方向（提案，按优先级）

> 以下均为讨论稿性质的提案，未做详细设计；落地前需各自走事件文档 + 评审流程。
> 共同约束：**不动 fail-closed 执行门禁、不动 KBD 结论门禁、不引入未经专家复核的知识直接消费。**

### P0-1 升级案例回流：把"转人工"改造成知识生产的入口

**思路**：人工解决并关闭的工单（尤其是 `human_escalation` 路径的）自动生成 KBD proposal 草稿，进入 data-pipeline 的 review 队列。素材天然齐备：故障现象描述、S0 分类、对话历史、工程师执行的命令序列（terminal_bridge 有完整记录）、最终根因与解法。

**要点**：
- 草稿只进 review 队列，**仍走专家复核后才发布**，不破坏"专家复核是唯一质量兜底"原则；
- 与现有"Bad Case 回流测评"机制（03-测评与GitOps）天然衔接：升级案例同时生成测评用例候选；
- 预期收益：KB 覆盖缺口的收敛从"被动导入存量案例"变为"主动消化在线增量案例"，直接针对 3.1。

**风险**：低。不改变任何运行时行为，只增加离线生产流水线的输入源。

### P0-2 KB 未命中率与诊断质量度量

**思路**：将以下指标提升为一级运营指标：
- 按 S0 分类的 `human_escalation` 率（KB 覆盖缺口的直接度量）；
- 按 KBD 维度的结论分布（DEFINITIVE / PARTIAL / INCONCLUSIVE / NO_MATCH），识别"命中但低质"的 KBD；
- matcher 大面积 UNSUPPORTED 的告警（HCI 版本升级导致输出格式漂移的早期信号）。

**要点**：现有 `agent_unsupported_claim_total`、hit 计数、Conclusion Gate 已具备数据基础，主要是聚合与告警层的工作。

**风险**：无。纯观测面建设。

### P1-1 恢复探索层，但降级为 advisory 模式

**思路**：将死代码的"机制推理"（`s4_fallback_v1`）复活为**纯建议模式**：KB 未命中时，在转人工的同时产出——基于 LLM 通用知识的假设清单（标注置信度）+ **只读**验证命令建议（经 `acli/classifier.py` 判定为只读/低风险才呈现），供工程师参考。

**硬边界**：
- 不进入自动执行链路，不产生 SOP/KBD 轨道的任何事件；
- 所有输出显式标注"知识库外推理，仅供参考"；
- 离线模式不适用（无人在环）。

**要点**：针对 3.4。实现上可复用 `_build_fallback_prompt` 的存量资产；advisory 输出本身也是 P0-1 知识回流的素材（工程师采纳/拒绝的假设是天然的标注数据）。

**风险**：中。需严格防止 advisory 建议被客户误当作系统诊断结论执行（前端呈现层必须强区分），以及防止 advisory 通道被逐步"转正"侵蚀执行门禁。

### P1-2 轻量观察通道：读命令白名单 + 低置信观察

**思路**：在 11 个 QKV 白名单之外，增设一档"通用只读采集"通道：维护只读命令模板白名单，执行输出喂给 Vision/LLM 做**低置信观察**（参照 `llm_inference` 的置信定位：默认 0.5、须标"待验证"），不参与 DEFINITIVE 结论判定，只作为辅助证据呈现。

**硬边界**：
- 只读分类器 fail-closed（未知命令不放行）；
- 观察结果不得进入 Conclusion Gate 的 SUPPORTED 证据集（或仅允许在 PARTIAL 及以下等级使用）；
- 门禁刻意比新生产者信号轻一档（无跨 10 组件契约改造），但保留审计。

**要点**：针对 3.2，用"置信分层"换取"观察空间扩展速度"。与 `qkv_vm_console` 的"重契约、高置信"路线互补。

**风险**：中。需要证明低置信观察不会污染高置信判定路径；建议先在离线模式试点（离线本就有 UNKNOWN + 补采机制可挂靠）。

### P2 离线证据放宽一档

**思路**：允许工程师在补采时附带自由格式证据（任意日志片段、截图、命令输出），LLM 以低置信参考读取（同 P1-2 的置信定位），而不是只有结构化 collector 一条路。原包零回写、派生 evidence_item 的机制已存在（见 `qkv_vm_console` 离线链路的"验包后派生 PNG + Vision 观察"），可复用。

**风险**：中。需要防注入与来源标注。

### 优先级总览

| 优先级 | 提案 | 针对局限 | 动执行门禁？ | 工程量级 |
|--------|------|---------|------------|---------|
| P0-1 | 升级案例回流 | 3.1 零沉淀 | 否 | 中 |
| P0-2 | 未命中率/质量度量 | 3.3 质量抖动无感知 | 否 | 小 |
| P1-1 | advisory 探索层 | 3.4 长尾零辅助 | 否（只读建议） | 中 |
| P1-2 | 轻量观察通道 | 3.2 观察扩展慢 | 否（低置信隔离） | 中～大 |
| P2 | 离线证据放宽 | 离线替代路径单一 | 否 | 中 |

---

## 五、信号生产成本分析与降本方向（V1.1 新增）

> 本章缘起：一线痛点反馈——**"现在写 KB 信号就跟写伪代码一样，生产成本太高"**。
> 本章与第三章是同一问题的两面：KB 覆盖缺口收敛慢 = 回流慢（3.1）+ 生产贵（本章）。
> 本章结论于 2026-08-20 经 schema、种子数据、golden 用例、pipeline/kb-service 路由与 Admin UI 源码交叉核实。

### 5.1 痛点字面成立：signals_json 是一段声明式诊断程序

一条可用信号的真实书写量：

| 维度 | 实际情况 |
|------|---------|
| 单条规模 | 裸信号约 33~45 行、14~23 个叶子字段；带完整 provenance/review 元数据约 47~106 行（带 extract/columns 的 qfk_system 可到 100+ 行）；另需文档级 `verification_contract`（约 20 行） |
| 作者必须手写的"逻辑" | `acquire.args`（命令/日志文件/时间窗/request_id）、`match`（type/pattern/mode/expected + `extract` 的 rows/columns/cardinality/value_key/ai_extract.instruction）、`orchestrate.produces`（变量名/类型/path）、`requires` 依赖、`verification_contract.evidence_policy`（must/should/exclude 分配） |
| 机械/可自动注入 | `id`、`role`、`provenance`（抽取时服务端注入）、`review.needs_review`、`orchestrate.phase` 默认值、`{{HOST}}` 占位符、发布盖章 |
| 跨 KBD 复用 | **无**。每个 KBD 的 signals_json 独立全量声明；种子数据中相似的 `qkv_alert`（仅 keyword 不同）逐条重复。信号模板管理页提案曾被**明确否决**（《QKV_QFK扩展性与配置易用性评估》§3.3，理由：省 7 天开发、可视化编辑器已够） |

这组"逻辑字段"的合集就是一段声明式诊断程序，而不是领域专家能自然书写的知识标注——这是"像写伪代码"的直接成因。

### 5.2 成本分解：四处花钱

1. **全量声明 + 无复用**：每个 KBD 约 20 个字段从头写一遍，高频模式（告警存在性检查、失败任务检查）反复重写。
2. **LLM 初稿后，最难的活仍落在人身上**：extract-signals 阶段由 LLM 直接从 KBD 章节 + 截图 Evidence IR 产出 v2 Candidate，但低置信信号的 match.pattern 会被清空并打 `needs_review`，由专家**手工补精确匹配串**；rejected candidates（写操作、catalog 缺命令、校验失败）需专家手工转成合法信号。
3. **校验靠打回**：三级门禁（草稿门禁 → review-signals 静态审查 → 发布门禁），错误码包括 `KBD_PRODUCER_SIGNAL_MISSING`、`QFK_OUTPUT_MODE_CONFLICT`、`SIGNAL_EXTRACT_REQUIRED`、`QFK_LOG_FILE_REQUIRED`、`QFK_LOG_PREDICATE_UNSUPPORTED`、`QFK_LOG_EXTRACT_UNBOUNDED`、`SIGNAL_VARIABLE_DEPENDENCY_INVALID`、`SIGNAL_REGEX_INVALID`、`SIGNAL_COMMAND_PIPELINE_UNSUPPORTED` 等；extract/matcher 与日志源 catalog 的兼容性目前也要作者自己保证。
4. **验证靠发布后**：作者**没有自助 dry-run**——KbdReviewView 只有 qfk_system 的命令字符串预览，无真实执行；replay/golden（positive/strong_negative/unknown/error 四态）/hci-sim fixture 设施齐全但全是工程侧工具，信号的真实效果基本在发布后靠现场消费验证。

### 5.3 一个现成但未落地的解药

2026-08-07《关键信号统一解析运行时与 Resolver 分层方案》（status: **proposed**）正是为此而生：作者只表达 **SignalIntent**（查哪个日志、跑哪个命令、判什么），路径解析、版本漂移、表示归一全部由领域 Resolver（Log/System/Domain/Service/Qkv/Variable）在运行时承担，"LLM 只产意图、不产可执行细节"。这是把"写伪代码"降回"写知识"的方案。Catalog 规则已半 Git 化（`backend/shared/resolution/catalogs/`），但方案整体未落地——当前作者仍需自行保证 extract/matcher 与日志源 catalog 兼容，否则被门禁打回。

### 5.4 降本方向（按性价比排序，均为提案，未采纳）

| 优先级 | 方向 | 思路 | 现有资产 | 风险 |
|--------|------|------|---------|------|
| **P0** | **自助 dry-run，验证左移** | 作者在 Admin 输入信号 → 立即对 hci-sim fixture / golden 样例回放（positive/strong_negative/unknown/error 四态），当场看 matcher 命中情况；把"发布后试错"变"书写时试错" | `shared.cdd.replay_evaluations`、golden 四态用例、hci-sim fixture 均已存在，缺的只是暴露给作者的产品层 | 低 |
| **P0** | **输出锚定的 pattern 生成** | 针对最难的手工活"补匹配串"：先在 sim/真实环境跑出命令真实输出 → LLM 从真实输出推导 match.pattern → 立即回匹配验证；把"盲写 pattern"变"对着答案写 pattern" | 命令预览、terminal_bridge、Vision 链路 | 低～中 |
| **P1** | **推进 Resolution Runtime 落地** | 5.3 的 SignalIntent 方案，治本，但工程量最大 | 方案文档已成稿（proposed），Catalog 半 Git 化 | 中（工程量大） |
| **P1** | **rejected candidates 自动修复循环** | 门禁打回原因已结构化（错误码 + 字段定位），让 LLM 读打回原因自动修一轮再进人审 | `humanize_signal_validation_error` 已把 Schema 错误翻译成字段级中文修复提示 | 低 |
| **P2** | **门禁分档** | 只读 qfk 消费者信号静态审查通过后抽检放行，专家精力集中到生产者信号与 evidence_policy 分配 | — | 中（需评审分档边界） |
| **P2** | **重新评估信号模板** | 不做管理页也可：把高频模式做成 LLM 抽取 prompt 的预设 + Admin 一键填充，作者只填 keyword/produces；当初的否决理由（省 7 天开发）应对照当前实际生产成本重估 | 《QKV_QFK扩展性与配置易用性评估》§3.3 | 低 |

**建议先埋度量**：每条信号的生产耗时 + 审核打回轮次。当前"成本高"仍是体感判断，有了数据才能确定先砍哪一刀。

---

## 六、附录：调研证据索引

> 本文所有结论于 2026-08-20 经代码核实，关键位置如下（路径相对仓库根）。

| 结论 | 代码位置 |
|------|---------|
| S0 分类驱动的主链路与 track 判定 | `backend/agent-service/app/adapters/agents/htp/investigation_agent.py::process()` |
| 三轨路由查询入口 | `backend/kb-service/app/routes/route.py`（`GET /api/kb/route`） |
| 未命中升级人工 | `investigation_agent.py` 的 `AgentEscalation` 分支；"系统不会生成知识库外的命令或根因"话术 |
| 机制推理死代码 | `investigation_agent.py::_process_fallback_mode` / `_build_fallback_prompt`；模板 `s4_fallback_v1`（`backend/shared/utils/prompt_loader.py`）；废弃于 commit `4e1d2474` |
| SOP 轨道变量门禁 | `backend/agent-service/.../sop_tools.py::SopToolExecutor._check_variable_source_gate` |
| 工具白名单与命令动态定级 | `tool_registry.load_tool_registry`；`backend/agent-service/.../acli/classifier.py::classify_acli/classify_bash` |
| KBD 轨道信号 DAG 与结论门禁 | `backend/agent-service/.../kbd_differential.py::KBDDiagnostic`；`backend/shared/cdd/plan_compiler.py`；`backend/shared/cdd/conclusion_gate.py::decide_conclusion` |
| 变量置信序 | `backend/shared/models/information.py` |
| 离线采集失败不中断 | `backend/diagnosis-service/offline-collector/collect.go` |
| 离线 UNKNOWN / Insufficient / 一次补采 | `backend/diagnosis-service/app/services/offline_analysis_service.py`（`_evaluate_signal` / `_calculate_assessment` / `_conclusion` / `_create_supplement`） |
| 工单关闭无知识写入 | `backend/case-service` 的 `close_case` |
| 知识生产流水线与专家复核定位 | `data-pipeline/README.md` |
| hit 计数 | `backend/kb-service/app/routes/hits.py` |
| FactStore 与 ClaimVerification | `backend/agent-service/app/services/fact_store.py`；`backend/shared/models/reliability.py` |
| QKV 白名单与 schema 封闭 | `backend/kb-service/app/routes/extract_signals.py::ACQUIRER_CATALOG`；`backend/shared/schemas/acquirer_args.py` |
| 发布门禁 | `signal_schema.py::validate_kbd_publishable_signals_json` / `certify_publishable_signals_json`；`data-pipeline/kbd/signal_review.py` |
| 新生产者成本参照 | `docs/solution/agent/虚拟机控制台视觉生产者信号设计与需求.md` §5.1 组件表 |
| 信号 schema 与单条规模 | `backend/shared/schemas/signals/signal.v2.schema.json`（约 1335 行，按 `acquire.tool` 分发到 `acquirer_args/*.schema.json`）；`backend/shared/schemas/signal_schema.py`；样例 `tests/golden/kbd_cases/cases/*.json`、`database/seeds/04_kbd_diagnosis_samples.sql` |
| LLM 信号抽取与 prompt | `data-pipeline/kbd/extract_signals.py`（Stage 5 调度）；`backend/kb-service/app/routes/extract_signals.py`；prompt `kbd_extract_signals_v2`（`database/seeds/02_system_prompts.sql`，StrictPromptLoader 热加载） |
| 信号复核 Admin UI | `frontend/admin/src/views/KbdReviewView.vue` + `components/editors/`（MatcherEditor / ProducesEditor / TextExtractEditor / ValueExtractEditor / QfkProcessingEditor） |
| 信号模板提案否决依据 | `docs/solution/agent/02-架构设计/QKV_QFK扩展性与配置易用性评估.md` §3.3 |
| replay/四态评估设施 | `tests/golden/kbd_cases/`（replay_scenarios 四态）；`backend/agent-service/tests/unit/test_kbd_golden_contracts.py`；`shared.cdd.replay_evaluations`；`hci_sim/testdata/kbd-27123-fixture-manifest.json` |
| 统一解析运行时提案（proposed） | `docs/solution/agent/events/2026-08-07-关键信号统一解析运行时与Resolver分层方案.md`；`backend/shared/resolution/catalogs/{resolution_catalog,acli_command_catalog}.json` |
