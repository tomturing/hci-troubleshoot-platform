---
status: accepted
category: solution
audience: product, architect, developer, qa, sre
last_updated: 2026-08-17
owner: team
---

# hci-sim 能力缺口对抗性审查与仿真环境构造流程全景

> **编者按（归档说明，2026-08-17 新增）**
> - 本文档归档自 2026-08-16 的「hci-sim 能力缺口对抗性审查」结论与同日的「仿真测试环境完整构造流程」图示梳理，对话原文一字未减，仅追加归档说明、勘误与证据索引。
> - 证据基线：仓库代码与文档状态以 2026-08-16 核验为准（KBD-27123 纵向样板、C1 全量能力报告、2026-08-11 P0-P1 全链路验证等）；代码演进后请以最新源码为准。
> - 配套浏览器版流程图（Mermaid 渲染，含全景流程图 / 组件图 / 构建时序图三张，颜色区分 自动 / 人工 / 未实现，需联网加载 Mermaid CDN）：本文档同目录 [`sim-env-construction-flow.html`](sim-env-construction-flow.html)。
> - 勘误预告：原文图 2 中 conversation-service 回取权威 context 的接口路径存在一处笔误，归档时已按源码更正，详见[附录 B · 勘误](#附录-b-勘误新增)。

---

# 第一部分 · 能力缺口对抗性审查（2026-08-16）

# 结论

**两个问题的严谨答案都是“否”，但否定的方式不同：**

1. **“支持指定 KBD 的 ID”--接口签名上支持，语义上不支持。** 所有入口都接受 `kbd_id` 参数，但运行时的真实语义是“校验你指定的 ID 是否等于部署时烧死在镜像里的那一份 manifest 的 KBD”（当前 dev 是 27123）。指定任何其他 ID，得到的是结构化的 `capability_gap` 拒绝，而不是为该 KBD 构建环境。
2. **“自动化生成构造仿真环境的一切数据”--目前为假，且按系统设计永远不该为真。** 已自动化的是“从一条已发布 KBD 出发的后半段链路”（capability 解析 -> synthetic Bundle 编译 -> run/lease/case/conversation/context 持久化）；未自动化的是前半段权威输入的生产（KBD 人工审核发布、真实 Artifact 双角色审批、手工维护的 git manifest）。而“真实数据口径”的生成（positive-realistic Bundle）整条管线尚未落地。另有一部分数据（审批判断、TestRun 标题描述）是治理设计上刻意保留的人工输入，不属于该自动化的范畴。

以下按第一性原理展开，所有断言附代码/文档证据。

---

## 一、第一性原理：构造一个仿真测试环境，数据上到底需要什么

把“仿真测试环境”拆到不可再分，需要四层数据，缺一层环境就不成立：

| 层 | 数据 | 本质问题 |
|---|---|---|
| ① 权威输入 | 已发布 KBD + 通过 publish_validation 的 Signal v2 文档 + active 的 Tool Contract | “仿真谁”--没有已冻结的事实，一切输出都是编造 |
| ② 数据面 | Fixture Bundle（每条采集命令 argv -> 确定性 stdout/exit_code/fault） | “命令进来回什么”--synthetic（模板生成）还是 positive-realistic（源自真实脱敏 Artifact） |
| ③ 控制面 | scenario / bundle / run / lease / outbox 记录 | “可审计、可幂等、可追溯地跑一次” |
| ④ 平台上下文 | 真实 Case + Conversation + agent 的 sim-ssh 权威 context + bridge 连接 | “Agent 像处理真工单一样处理它” |

`hci_sim` 的架构决定了一个硬约束：**Runtime 只加载一份不可变 manifest**（`HCI_SIM_FIXTURE_MANIFEST`，`hci_sim/cmd/hci-sim/main.go:181`；Helm 里 `deploy/helm/hci-sim/values.yaml:47` 固定指向 `kbd-27123-fixture-manifest.json`）。这份 manifest 的 `KBD.SupportID` 就是该部署实例的全部 KBD 世界--“指定 KBD ID”的一切行为都绕不开这个单点。

## 二、Q1：指定 KBD ID 的三个入口，逐个对抗性审查

**入口 1：`bootstrap --kbd-id`（`bootstrap.go:80`）--真支持，但只产出 synthetic。** 它从 C1 capability API（kb-service `hci_sim_resolver.py`）拉取该 KBD 的权威快照，编译出标记 `SYNTHETIC=true` 的 Bundle。前提是该 KBD 已达到 `ready_for_artifact_binding`（`bootstrap.go:107`），未 ready 一律 fail-closed 拒绝。产出的 Bundle 只能配合本机 aCLI 适配器和 C3 人工验收场景，**不能**作为正式 build 的输入。

**入口 2：`GET /v1/simulations/capabilities/{kbd_id}`（`main.go:245`）--这是个预检接口，不是加载接口。** 接受任意 ID，但对非加载 KBD 返回 `kbd_not_loaded` gap、`buildable: false`（`main.go:261`）。它的价值是让 Admin UI 在构建前看到“为什么不行”，仅此而已。

**入口 3：`POST /v1/simulations/build`（`main.go:293-299`）--三道闸门，全都要过。**
- 闸门一：请求的 KBD 必须等于已加载 manifest 的 KBD，否则 409 `requested KBD is not the loaded immutable fixture`；
- 闸门二：`HCI_SIM_AUTHORITY_SCOPE` 不得为默认值 `runtime_fixture` 且 manifest 非 synthetic（`main.go:297`）。**对抗性发现：Helm 默认值恰恰是 `runtime_fixture`（`values.yaml:24`），即按默认部署 build 永远 409。** dev 环境能跑通 KBD-27123 纵向样板，靠的是运维把该变量手工覆盖为 `dev_golden`（见验证记录 `docs/verify/hci-sim/events/2026-08-11-hci-sim-P0-P1-27123全链路验证.md:58`）--这道“安全闸门”目前靠运维不乱改 env 来维持，不是工程化保证；
- 闸门三：revision 匹配。`HCI_SIM_ACTIVE_REVISION` 与 manifest revision 不一致报 `kbd_revision_mismatch`--P0-P1 验证记录里真实出现过 requested=24 vs runtime=1 的漂移现场（同文件 :58），还出现过 GitOps digest 滞后导致 capabilities 404（同文件 §0）。

**结论：** “指定任意 KBD ID 自动出环境” = 不支持。当前等价于“换一个 KBD 需要重走一次应用发布”（手写 manifest JSON 进 git + 改 Helm values + Argo 更新 image digest）--生产化差距审查文档的原话就是“新 KBD 仍接近一次应用发布……人工步骤过多且无法原子化”（`docs/solution/hci-sim/events/2026-08-11-…生产化差距审查与Bundle工厂化重构基线.md:70`）。另有细节约束：`support_id` 数据库上限 varchar(20)，capabilities 接口拒绝含 `/?#` 的 ID。

## 三、Q2：逐层数据的“自动生成”真实状态

**已自动化（给定一条 published + ready 的 KBD 之后）：**
- C1 capability 解析：只读 `dynamic_resource_active` 不可变快照，产出 synthetic_routes 与结构化 gap 码，绝不推进编辑态（`hci_sim_resolver.py:218-307`）；
- synthetic Bundle 编译：从 resolved synthetic_routes 生成全部路由，支持 positive/negative/missing-evidence/command-failed/timeout/version-incompatible 七种变体（`bootstrap.go:266-365`）；
- scenario/bundle/run 持久化：输入指纹确定性 UUID、幂等键、同事务 upsert（`hci_sim/internal/database/run_repository.go:104-149`）；
- lease 签发 + connection 数据、environment_context 生成（`main.go:337-362, 540-566`）；
- 真实 Case 创建与重试复用（`backend/api-gateway/app/routes/simulations.py:88-129`）、Conversation 创建（前端 `SimulationConversation.vue`）、sim-ssh 权威 context 回取（conversation-service `simulation_context_client.py`）；
- 5 篇 SAMPLE-SIG-* 样例 KBD 的 seed 种入与 CI 契约校验（`database/seeds/04_kbd_diagnosis_samples.sql` + `scripts/hci-sim/diagnosis-lab.py` 的 contract-smoke）。

**未自动化（缺口本体）：**
1. **KBD 自身的生产**：seed 明确“只创建 draft KBD，不自动审核、不自动发布”（seed 文件头）。实测漏斗：dev 126 条 KBD，仅 6 条 published、**2 条 ready_for_artifact_binding**、4 条 TOOL_CONTRACT_STALE、120 条未发布（`docs/verify/hci-sim/events/2026-08-06-…C1权威KBD解析与全量能力验证报告.md:22-32`）--上游供给是空转的。
2. **真实口径的 Bundle 数据（最大缺口）**：positive-realistic 要求绑定获批 Artifact（采集->脱敏->secret/PII/license/schema 四重扫描->expert+security 双角色审批）。C2 的生产 Registry、OCI/S3/KMS、PostgreSQL Repository **全部未落地**（C2 任务文档“后续阻断项”全未勾）。
3. **837 行控制面内核是死代码**：`internal/controlplane` 的 Bundle 状态机、双角色审批、TestRun、差分/mutation 内核，`main.go` 的 import 里根本没有它，全仓无非测试引用--生产链路用的是 main.go 手写 handler + 简化版判定（`simulationBuildable`，`main.go:568`）。“看起来有审批系统”与“有审批系统”是两回事。
4. **样例输出来自人工**：每信号的正/负输出是人工编写入库的画像 JSON（`hci_sim/testdata/sample-suites/diagnosis-signal-matrix-v1.json`），不是生成的。
5. **TestRun 的 title/description 是唯一必填人工字段**（三 PR 闭环方案 :59）；conversation 消息由真实交互产生，无 seed 脚本（这在验收语义下是特性，但字面上就是“不是一切数据都自动生成”）。
6. **E 级证据全 pending**：real/sim 差分、mutation、稳定性、容量--没有它们，synthetic 输出的“代表性”无法量化，这是 synthetic 路径自身的合法性欠账。

## 四、Q3：能力缺口与优先级

官方文档自评：A/B/C/C1/C3/D 均 `in_progress`（代码级完成、生产化未完成）、C2 `in_progress`（阻断项全未做）、E `planned`；总结论“KBD 27123 纵向样板 PASS / 面向终端用户的生产交付 **BLOCKED**”（`docs/task/hci-sim/hci-sim任务.md:18-28`，Bundle 工厂化 5 项任务全未勾）。我按“解锁下游能力的杠杆 × 阻断程度”重排如下：

**P0 -- 直接阻断“指定任意 KBD 自动出环境”这一主张，不做则一切无从谈起：**
1. **真实 Bundle 生产管线（C2 落地）**：把 Artifact 状态机/审批从内存参考实现接到 PostgreSQL + 对象存储 + KMS + 真实身份源。这是唯一能让非 synthetic Bundle 合法存在的路径；没有它，build 的 authority 闸门永远只能靠 `dev_golden` 这类手工覆盖绕过--闸门形同虚设。
2. **多 KBD Runtime 能力**：Bundle Registry（多 KBD 存储、按 kbd_id 路由、热载或 per-KBD 部署）。这是“指定 KBD ID”从校验语义变为真语义的关键，也是打破“单 pod 单 KBD 单 manifest、replicaCount=1”单点的前提。
3. **KBD 发布漏斗治理**：126->6->2 的漏斗说明瓶颈根本不在 hci_sim 而在上游；含 Tool Contract stale 的自动重抽。工具再好，无米下锅。

**P1 -- 把“一次应用发布”降为“一次审批动作”：**
4. **Bundle 工厂化**（任务文档第 4 节，5 项全未勾）：manifest 编译入库、审批流、Argo/digest 更新自动化，消除手工 git 维护的 27123/23821 模式。
5. **authority_scope 正式化**：给 `dev_golden` 类覆盖以审批语义和配置追溯，堵住“运维改一个 env 就放开 build”的对抗面。
6. **controlplane 包的接线或删除决策**：837 行死代码要么接成真正的控制面服务，要么显式定位为参考实现--现状是最差的一种（维护成本照付、能力却是幻觉）。

**P2 -- 规模化与可信度证据：**
7. 阶段 D 收尾（持久 Scheduler、Headless Runner）；8. 阶段 E 全套（real/sim 差分、mutation、稳定性、容量/SLO）--这是回答“synthetic 数据到底像不像真的”的唯一手段；9. 画像/正负输出的半自动生成；10. 多副本与容量。

**最后一个对抗性备注**：即使 P0–P2 全部完成，“自动化生成一切数据”也不该成为目标--KBD 终审发布、Artifact 双角色审批、TestRun 的人为描述，是这个系统 fail-closed 设计里刻意保留的人类判断节点。正确的目标函数是：**人只做判断类输入，事实与派生数据全自动、可审计、可幂等重现**。当前系统离这个目标函数的距离，就是上面 P0 的三项。

---

# 第二部分 · 仿真测试环境完整构造流程（图示）

图已生成，两种载体：

- **本回复内的 ASCII 流程图**（终端直接可读）
- **浏览器版** `/aihci/sim-env-construction-flow.html`（Mermaid 渲染，含全景流程图、组件图、构建时序图三张，颜色区分 自动/人工/未实现，需联网加载 CDN）〔归档新增注：该 HTML 已随本文档归档至同目录 [`sim-env-construction-flow.html`](sim-env-construction-flow.html)，其中接口路径已按附录 B 勘误修正〕

图例：`●` 自动已实现 ｜ `◐` 人工闸门/手工步骤 ｜ `✖` 未实现（P0 缺口）

## 图 1 · 全景主干：从 KBD 生产到仿真测试环境可用（环节 0–7）

```text
【环节 0 · 上游权威输入：KBD 生产】
 ① KBD 原始文档 (data-pipeline fetcher) ................................. ●
        │
        ▼
 ② LLM 信号抽取 extract_signals -> Signal v2 草案 .......................... ●
        │            （Tool Registry active 修订逐 Signal 校验）
        ▼
 ③ 人工审核 + 发布：draft -> published ............................. ◐ 人工闸门
        │            （seed 只建 draft 不自动发布；dev 126 条中 120 条未发布）
        ├─ 滞留 draft ──► 流程到此为止（无权威输入）
        ▼ published
 dynamic_resource_active 不可变快照 (revision N, checksum) ............... ●
        │
        ▼
【环节 1 · C1 Capability 解析】kb-service HciSimKbdResolver .............. ●
        │            只读 active 快照、绝不推进编辑态、缺任一事实即 fail-closed
        ├── 缺事实 ──► 结构化 capability_gap（KBD_NOT_PUBLISHED /
        │               SIGNALS_MISSING / TOOL_ACTIVE_SNAPSHOT_MISSING / …）-> 终止
        ▼ ready_for_artifact_binding（dev 现状仅 2 条）
【环节 2 · Fixture Bundle 生产（分叉）】
        ├── 路径 A · synthetic【已实现】 .................................. ●
        │     bootstrap --kbd-id -> 编译 SYNTHETIC=true manifest（7 种变体）
        │     仅供 C3 本地验收 / aCLI 适配器，不能作为正式 build 输入
        │
        └── 路径 B · positive-realistic【P0 缺口】 ......................... ✖
              真实 Artifact->脱敏->四重扫描->Expert+Security 双角色审批
              ->Compiler->Bundle Registry（controlplane 837 行为内存参考实现，未接线）
              │
              └─ 现实替代 · 手工路径 ................................... ◐ 人工
                    手写 kbd-*.json 入 deploy/helm/hci-sim/files/
                    + values.yaml manifestFile + Argo 更新 image digest
                    （=「新 KBD ≈ 一次应用发布」，人工步骤多且无法原子化）
        │
        ▼
【环节 3 · Runtime 部署与加载】hci-sim-dev 单副本 pod .................... ●
 HCI_SIM_FIXTURE_MANIFEST -> fixture.Parse：
 digest / 未知字段 / RouteKey 歧义校验 -> Router（单 KBD 世界）
 Lease HMAC key ≥32B · host key Secret · 独立库缺失即 fail-closed
        │
        ▼
【环节 4 · 环境构建 build】Admin UI「仿真测试」面板
 4a 用户输入 KBD ID -> GET capabilities/{kbd_id}
     （api-gateway 注入控制面 token）-> buildable + capability_gap[] ....... ●
        │
        ▼
 4b POST /v1/simulations/build {kbd_id}，过三道闸门（见图 3）:
     ① kbd_id == 已加载 manifest 的 KBD？ ──否──► 409 kbd_not_loaded
     ② authority_scope ≠ runtime_fixture 且非 synthetic？
        （Helm 默认值恰为 runtime_fixture，dev 靠手工覆盖 dev_golden 放行）
     ③ active revision == manifest revision？
        │ 全过
        ▼
 同事务持久化 + 签约 .................................................... ●
     scenario（UUID=SHA1(input_fingerprint)，确定性/幂等）
     + fixture.bundle + control_plane.run（Idempotency-Key）
     + lease.Sign：htp2 token，15min，绑定 Run/Bundle/KBD/node/container
        │
        ▼
 响应：test_run_id + connection{host, port, username=sim, lease token}
       + environment_context（execution_mode=sim-ssh 等） ................ ●
        │
        ▼
【环节 5 · TestRun 绑定平台上下文】
 5a 用户填 title / description（唯一必填人工字段） .................. ◐ 人工
        │
        ▼
 5b api-gateway 先创建真实 Case（Q…；重试带 case_id 复用原工单） ......... ●
     -> Runtime 校验 sim-ssh 显式绑定 -> BindCase（不可变绑定）
     -> status CAS：requested -> preparing + test_run.created 事件
        │
        ▼
 5c 前端建 Conversation（?case_id=…）-> conversation-service
     回取 Runtime 权威 context -> Agent 获得 sim-ssh 权威上下文 ........... ●
        │
        ▼
【环节 6 · 执行（真实闭环）】
 前端 connectBridge -> WebSocket /terminal-bridge -> SSH :2222
 （auth_type=lease, execution_mode=sim-ssh） ............................. ●
        │
        ▼
 Agent 诊断 -> recommended_command 命令卡片:
   risk=1 自动执行 ｜ risk=2 人工点「允许执行」 ｜ risk=3 自动阻止 ... ◐ 部分
        │
        ▼
 每条命令（Runtime 内部）:
   lease 复验（签名/时效/配额）-> Lex（禁 shell 操作符）-> NormalizeArgv
   -> RouteKey{tool,acquisition_key,argv,node,container}+variant 精确匹配
     （未命中 -> fixture_not_found，fail-closed）
   -> 确定性 Result + fault（none/nonzero_exit/permission/timeout/
     disconnect/truncate）-> run_event 落库（trace_id） ................. ●
        │
        ▼
 命令卡片 passed/failed；会话级 agentOutcome（exit_code≠0 -> failed）..... ●
        │
        ▼
【环节 7 · 结果与证据】
 POST result（api-gateway 规范化 report_summary 并计算 report_digest）
 -> run_result（CAS 幂等）-> run_outbox -> reconciler 投递 webhook ........... ●
 证据落点：control_plane.run / run_event / run_result
          + capability-matrix.sh CI 门禁
```

## 图 2 · 服务组件与数据流（构建后的运行时拓扑）

```text
 用户/操作层
   Admin UI（SimulationConversation.vue）        审核人员（KBD/Artifact 审批）◐
        │ /api/hci-sim/*（api-gateway 注入控制面 token）
 ───────┼────────────────── 应用层 ──────────────────────────────────────────
        ▼
   api-gateway ─── /v1/simulations/{capabilities,build,test-runs,result} ──┐
        ├── POST /api/cases ──► case-service（真实工单 Q…，重试复用）        │
        └── POST /api/conversations ──► conversation-service                │
                 │ GET /v1/simulations/test-runs/{test_run_id}/context（回取权威 context）│
                 ▼                                                          ▼
              Agent（sim-ssh 权威 context）                    hci-sim Runtime
                 │                                              HTTP 控制面
                 ▼                                                     │
   terminal_bridge（K3s，不解析 lease 只透传）                           │
        WebSocket /terminal-bridge ──► SSH :2222（password=lease）        │
                                                    │                    │
 ───────────────────────────────────── 仿真层 ──────┼────────────────────┤
                                                    ▼                    ▼
                            hci-sim Runtime SSH 数据面：Router 精确路由 + Lease 复验
                            reconciler：run_outbox -> webhook
                                                    │
 ───────────────────────────────────── 数据层 ──────┼─────────────────────
                                                    ▼
              PostgreSQL 独立库 hci_sim（16 表 4 schema，无跨库外键）
              主库（KBD / Signal / Tool Registry / Case / Conversation）
```

## 图 3 · build 三道闸门（“指定 KBD ID”的真实语义）

```text
 POST /v1/simulations/build {kbd_id}
        │
        ▼
 ① kbd_id == 已加载 manifest 的 KBD ？ ──否──► 409「requested KBD is not
        │                                     the loaded immutable fixture」
        │ 是
        ▼
 ② scope ≠ runtime_fixture 且非 synthetic ？ ──否──► 409「Runtime fixture
        │            （Helm 默认值=runtime_fixture，                lacks authority」
        │             dev 手工覆盖为 dev_golden 才放行）
        │ 是
        ▼
 ③ active revision == manifest revision ？ ──否──► gap: kbd_revision_mismatch
        │ 是                                            buildable=false
        ▼
 构建：scenario + bundle + run 同事务 -> htp2 Lease -> connection + context
```

三张图合起来读：**环节 0–2 决定“有没有米”（当前瓶颈：人工发布漏斗 + 真实 Bundle 管线缺失），环节 3 的单 manifest 决定“指定 KBD ID”只是校验语义，环节 4–7 是已经全自动跑通的部分**（KBD-27123 纵向样板即证据）。浏览器版中三张图可缩放查看，节点颜色即上述图例。

---

# 附录 A · 配套文件（新增）

| 文件 | 说明 |
|---|---|
| [`sim-env-construction-flow.html`](sim-env-construction-flow.html) | 浏览器版流程图（Mermaid 渲染）：图 1 全景流程（环节 0–7，节点按 自动/人工/未实现/数据落点 着色）、图 2 服务组件与数据流、图 3 环境构建时序图（build -> TestRun -> 执行 -> result 的参与者交互）。需联网加载 Mermaid CDN。 |

# 附录 B · 勘误（新增）

1. **图 2 / HTML 时序图中的 context 回取路径**：对话原稿误写为 `GET /test-runs/glm-5.3_common/context`，正确路径为 `GET /v1/simulations/test-runs/{test_run_id}/context?case_id=…`。证据：Runtime 侧 `hci_sim/cmd/hci-sim/main.go`（`/v1/simulations/test-runs/` + `/context` 后缀的 GET handler）；调用方 `backend/conversation-service/app/services/simulation_context_client.py:41`（`url = f"{self.base_url}/v1/simulations/test-runs/{quote(test_run_id, safe='')}/context"`）。本档正文图 2 与 HTML 均已按此更正。
2. 其余内容（结论、缺口清单、优先级排序、图 1/图 3）与 2026-08-16 对话原文一致，未做删改。

# 附录 C · 证据索引（新增）

| 主题 | 证据位置 |
|---|---|
| Runtime 单 manifest 加载 | `hci_sim/cmd/hci-sim/main.go:181`；`deploy/helm/hci-sim/values.yaml:47` |
| build 三道闸门 | `hci_sim/cmd/hci-sim/main.go:293-299`；`values.yaml:24`（authorityScope 默认 runtime_fixture） |
| capability 预检接口 | `hci_sim/cmd/hci-sim/main.go:245-279` |
| bootstrap synthetic 编译 | `hci_sim/cmd/hci-sim/bootstrap.go:78-194, 266-365` |
| C1 权威解析与 gap 码 | `backend/kb-service/app/services/hci_sim_resolver.py:218-387` |
| scenario/run 持久化 | `hci_sim/internal/database/run_repository.go:104-149`；`database/hci-sim-migrations/000001_control_plane.sql:12-45` |
| Case 创建与重试复用 | `backend/api-gateway/app/routes/simulations.py:78-129` |
| 权威 context 回取 | `backend/conversation-service/app/services/simulation_context_client.py:33-41` |
| controlplane 死代码结论 | `hci_sim/internal/controlplane/controlplane.go`（837 行，无生产接线）；`main.go:22-28` import 列表 |
| KBD 漏斗基线（126/6/2） | `docs/verify/hci-sim/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证报告.md:22-32` |
| 27123 纵向样板现场证据 | `docs/verify/hci-sim/events/2026-08-11-hci-sim-P0-P1-27123全链路验证.md`（§0 digest 滞后、§5 最终证据、§6 证据边界） |
| 「新 KBD ≈ 一次应用发布」 | `docs/solution/hci-sim/events/2026-08-11-hci-sim生产化差距审查与Bundle工厂化重构基线.md:70` |
| 阶段状态与 BLOCKED 结论 | `docs/task/hci-sim/hci-sim任务.md:18-28` |
