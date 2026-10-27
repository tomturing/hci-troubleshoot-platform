---
status: active
category: verify
audience: developer
last_updated: 2026-10-26
owner: team
---

# 通用排查原则

## 原则一：先确认进程/服务真正在跑

不要假设服务在运行，先 `ps aux | grep <name>` 或 `systemctl status` 确认。

## 原则二：先看日志末尾，再看全量

`journalctl -u <service> -n 50 --no-pager` 或 `kubectl logs --tail=50`，定位最近一次错误。

## 原则三：区分"历史错误"和"当前错误"

重启次数多（如 RESTARTS > 10）不代表当前异常，看"最后一次重启时间"和当前状态。

## 原则四：网络类问题先排除代理/防火墙干扰

容器/K8s 网络异常时，第一优先检查是否有 VPN/代理（如 Clash TUN）劫持路由，再查 NetworkPolicy、iptables 规则。

## 原则五：前端报"internal error"时的三步定位法

**第一步：先读避坑指南确认排查方向**（CLAUDE.md 强制要求，不可跳过）
- 读 debugging.md 原则一~四：先看日志/进程、区分历史错误
- 读 network-service-check.md：排除网络/Clash TUN 等基础设施故障，再聚焦代码逻辑层

**第二步：全仓库精确搜索错误文案，定位源头**
```bash
# 用报错原文搜索代码，找到是哪个文件/行号生成这条消息
grep -RIn "internal error\|Error: internal error" backend/ frontend/ adapters/ \
  --include="*.py" --include="*.ts" --include="*.js"
```
这一步能直接命中根源文件，避免盲目翻日志。

**第三步：顺数据流往上追调用链（结合运行时日志验证）**
1. Pod 全 Running → 排除崩溃，是运行时/代码逻辑错误
2. 从源头服务日志确认错误触发：`kubectl logs --since=2h | grep -v "GET /health" | grep -i "error\|400\|500" | tail -30`
3. 顺 SSE/HTTP 数据流逐层检查：下游服务 → 中间层（conversation-service）→ api-gateway → 前端
4. **特别注意**：错误信息可能不是通过 `event:error` SSE 帧发出，而是混入普通 `data:` 帧的 assistant 内容里，后端直接透传，前端无感知地追加显示 —— 此时运行时日志层看不到 4xx/5xx，必须读源码数据流

## 原则六：K8s ConfigMap subPath 挂载会导致目录 root 权限

ConfigMap 以 subPath 方式挂载单个文件时（如 `/home/node/.openclaw/openclaw.json`），
Kubernetes 会以 root:755 创建父目录 `/home/node/.openclaw/`，导致非 root 进程无法在该目录下创建子目录。

现象：`EACCES: permission denied, mkdir '/home/node/.openclaw/workspace'`

解决方案：用 busybox initContainer 代替 subPath：
- ConfigMap 挂到只读路径（如 `/etc/app-config/`）
- initContainer 执行 `mkdir -p + cp + chmod`，写入 emptyDir
- 主容器从 emptyDir 读，进程拥有完整写权限

## V-011：图片级 LLM 统计不得直接当作 KBD 案例级完成状态

**症状：** Vision API 日志显示多张图片 `done`，但 Pipeline 进度把多数 KBD 标成失败，进而把分类和 Signal 抽取显示为“失败”。

**根因：** Vision Job 的 `done/failed` 是图片级计数；案例是否完成还取决于 `images_json` 是否包含全部 seq、每个 `desc` 是否非空以及 Evidence 质量状态。若只用图片总数或只用一次 API 返回值投影案例状态，会把粒度不同的事实混在一起。

**修复：** API 返回 `case_results` 和图片级统计；编排层以数据库 `images_json` 的逐图片聚合为 KBD 最终状态，并记录 `VISION_STATE_INCONSISTENT`。状态不一致必须进入排障队列，不能静默转为普通失败或成功。

**预防：** 回归测试必须同时断言图片级 done/failed、案例级 succeeded/warning/failed、下游 blocked_by_dependency 数量；日志中保留 support_id、seq、job_id、trace_id 和 error_code。任何阶段改变结果粒度时，都要显式定义聚合函数。

## V-012：QKV acquire.keyword 不得复用 QFK 数组语义

**症状：** Signal 抽取或保存门禁报错 `acquire.args.keyword 类型错误：期望 string，实际 list`；LLM 输出的 `qkv_task`/`qkv_alert` 中 `acquire.args.keyword` 为单元素或多元素数组。

**根因：** Prompt 曾使用未限定字段路径的“keyword pattern 数组”“多关键字使用数组”等规则。模型把 QFK 的 `match.pattern` 或 `extract.rows.include/exclude` 数组语义错误迁移到 QKV `acquire.args.keyword`，而共享 Schema 正确要求 QKV 关键词是单个字符串。

**修复：** 保持 `qkv_* .acquire.args.keyword` 严格为非空 `string`；Prompt v2.3 和存量迁移 023 明确参数类型矩阵。多个任务动作拆成多条 qkv Candidate；数组仅允许用于 `match.pattern` 或 `extract.rows.include/exclude`，不做单元素自动 unwrap 或多元素 join。

**预防：** 维护 Prompt 合同测试、Schema 类型测试和迁移负向断言；对 `QKV_KEYWORD_MUST_BE_STRING` 错误码做日志/指标观测，部署后抽样检查实际 `system_prompt` 版本与 QKV 输出类型。

---

## 高频场景：工单创建 500 / `case.close_reason` 字段缺失（迁自 network-service-check.md）

**现象：**
- 前端提示：`创建工单失败: Request failed with status code 500`
- `api-gateway` 日志：`POST /api/cases/ ... 500`、`JSONDecodeError: Expecting value`
- `case-service` 日志：`UndefinedColumnError: column "close_reason" of relation "case" does not exist`

**根因：** 应用代码已升级依赖 `case.close_reason` 字段，但目标库 Schema 未收敛到期望状态。本仓库**不存在增量 Schema 迁移脚本**（旧文档所写的 `database/migrate_evaluation_v1.sql` 在仓库中不存在，属误记）：`close_reason` 已定义于 `database/desired_schema.sql`，由 Atlas 声明式收敛，完整流程见 [数据库迁移管理](../../../database/README.md)。

**快速修复：**
```bash
# 1) 先确认目标库确实缺列
kubectl exec -n hci-troubleshoot postgres-0 -- \
  psql -U hci_admin -d hci_troubleshoot -c "\d case"
# 2) 收敛到期望 Schema（无迁移文件可 cat，由声明式差量计算完成）
#    部署态走 scripts/db-migrate.sh；本地直连库可用：
cd /aihci/hci-troubleshoot-platform
atlas schema apply --env local --auto-approve
```

**验证（期望返回 201）：**
```bash
python3 - <<'PY'
import json, urllib.request
u = 'http://127.0.0.1:4888/api/cases/'
p = {"title": "回归验证", "description": "回归验证",
     "client_id": "client-regression-verify", "assistant_type": "productionclaw"}
req = urllib.request.Request(u, data=json.dumps(p).encode(),
      headers={'Content-Type': 'application/json'}, method='POST')
with urllib.request.urlopen(req, timeout=8) as resp:
    print(resp.getcode())
PY
```

**预防：** 每次发布后校验 `case.close_reason` 字段是否存在；将数据库迁移纳入发布门禁。

---

## V-004：命令输出形态变化后不能复用旧行筛选条件

**症状：** 命令在 terminal_bridge 日志中 `exit_code=0` 且 `stdout_raw_len>0`，Agent 却报告 `QFK_OUTPUT_EMPTY` 或 `QFK_NO_MATCH`。把命令从 `ps -p PID -o pid=,args=` 改为 `ps -p PID -o cmd=` 后尤其容易出现。

**根因：** 只修改了命令，没有同步审查 `extract.include/exclude/column/cardinality`。`cmd=` 输出只含命令行，不含 PID；旧的 `include=[PID]` 会在边缘安全筛选阶段丢弃唯一一行。错误中的“stdout 为空”指筛选后逻辑流为空，不代表远端物理 stdout 为空。

**修复：** 逐层核对 remote exit code、raw stdout、filtered stdout 和 extractor 结果。对于 `ps -p {{PID}} -o cmd=`，由命令参数完成 PID 定向，extract 使用空 include/exclude、`exactly_one + whole` 取得 CMD。

**预防：** 命令参数或输出列发生变化时，把整套 extract 视为同一个契约重新评审；golden test 除 PASS 状态外必须断言实际产出值，并保留旧筛选条件的失败反例。禁止运行时静默忽略人工配置的 include。

---

## V-006：QFK 产出变量模式不得被 Matcher 前置校验短路

**症状：** Bridge/远端命令已经返回 `exit_code=0`，筛选后的 stdout 也能按 KBD 的 `produces` 规则取到值，但 `tool_result` 却失败为 `QFK_MATCHER_MISSING: QFK 判定必须配置新版 match.extract`；下游信号随后显示 `blocked_dependency`，例如缺少 `PID`。KBD27123 的 `lsof -> PID -> ps` 链在 `Q2026080323483` 中即出现该现象：lsof 从约 413462 行筛出两行，现场复放可取得 `PID=8369`，但变量池没有该值。

**根因：** QFK 有两种不同的语义：`match` 模式用于判断证据是否命中，`match=null + orchestrate.produces` 的 producer 模式用于从成功输出中产出变量。若 QFK 引擎在命令成功后无条件要求 `matcher`，producer 会在调用提取函数前被错误短路。此时不能依据工具总状态推断远端命令执行失败，必须同时检查 remote exit、筛选后输出、QFK 引擎错误和变量池。

**修复：** 由调用方显式传入 `execution_mode="produce" | "match"`。producer 成功时返回受输出上限保护的完整结果给 `produces` 提取；仅 match 模式要求 matcher。调用链必须在 producer 成功后执行变量池填充，并将“命令执行”“结果处理/变量提取”“下游依赖”作为独立状态记录和展示。

**预防：** 为完整链路建立端到端测试：执行 `qfk_system lsof`、按 VM 字面量筛选、从第 2 列提取 PID、再执行 `ps -p {{PID}} -o cmd=`。测试同时断言实际 PID、变量池内容、下游调度和各阶段状态；不能只覆盖 `_fill_pool_from_qfk()` 等局部辅助函数。保存/发布校验必须把 producer 与 matcher 作为互斥的、均可执行的模式验证。
## V-014：仿真场景标签不得充当 KBD 候选答案

**症状：** 分类全量 CDD 因某篇非目标 KBD 的命令没有 fixture 而输出 `PARTIAL`；修复时读取 TestRun 的 `support_id/kbd_revision`，只保留目标 KBD，于是单篇信号通过后直接得到 `DEFINITIVE`。

**根因：** 混淆了两种身份：TestRun 的目标 KBD 标识“hci-sim 要模拟哪种故障现场”，S0 分类快照标识“Agent 必须验证哪些诊断假设”。用前者筛选后者会泄漏标准答案，CDD 的区分能力、其他候选排除和 Conclusion Gate 都被绕过。

**修复：** 真实与仿真始终加载同一个 S0 分类完整可执行 KBD 集合；`sim-ssh` 只能切换 acquisition provider。Bundle 编译和发布必须覆盖分类 SignalPlan 可能执行的 RouteKey，并为非目标候选提供可审查的反证现场数据。`fixture_not_found/ERROR` 仍然 fail closed，绝不能改判为 `CONTRADICTED`。

**预防：** 回归测试必须断言 real/sim 的 candidate IDs 完全相同，并通过至少两篇同分类 KBD 的端到端用例证明目标 KBD `SUPPORTED`、其他 KBD 由真实 Matcher 得到 `REJECTED`。Bundle freshness 在 TestRun 创建前检查；版本过期阻止使用旧仿真数据，但不得在 Agent S1 阶段把 revision 当作候选过滤条件。

## V-015：离线 producer 必须用与 Collector 一致的结构化证据形态取值

**症状：** 已发布 KBD 有可读 Evidence Item，但 QKV/QFK producer 变为 `NOT_MATCHED` 或 `UNKNOWN`，下游变量不再解锁；常见于测试将 producer 输出写成泛化文本 `"collected"`。

**根因：** 离线 `structured_data` 不是统一字符串：文本采集器以 `{preview, truncated}` 包装，JSON 采集器保存对象，控制台截图保存视觉观察对象。Evidence Item 存在不能证明变量已从 `produces` 路径取到。

**修复：** QKV 复用共享 parser 展开 JSON、文本 preview 和弹框日志；QFK 对 preview 使用完整文本取值。`truncated=true` 的文本索引保持 `UNKNOWN`。PostgreSQL 样例为每种 producer 提供可按契约解析的真实输出形态。

**预防：** producer 回归同时断言实际变量、`MATCHED` 和下游依赖解锁；为 JSON、文本 preview、弹框日志、控制台观察和截断文本各保留一条测试。不要用“有可读证据”替代变量提取断言。

## V-019：网关「上游服务暂时不可用」实为身份 Cookie 轮换触发的会话归属 403

**症状：** 用户在工单排障对话中，第一条消息 AI 正常回复；隔几分钟再发第二条（如点确认分类）时，AI 回复固定为「上游服务暂时不可用」，刷新/重开也无法自愈。日志中 conversation-service 报 `auth_conversation_forbidden` → 403「无权访问此会话」，api-gateway 报 `POST /api/conversations/{id}/message → 403`。工单 Q2026101330905、Q2026101328057 同时命中。

**根因：** 两个独立缺陷叠加：
1. **身份静默轮换（真因）**：#1064 把 client_id 从「浏览器自持久化的 `X-Client-ID` 头」改为「服务端签名 HttpOnly Cookie `hci_client_id`」，但 `verify_identity` 复用了服务间出口签名的 `CLOCK_SKEW_SECONDS=300` 硬时间窗，且 `IdentityMiddleware` 验签成功后**从不刷新** Cookie 内嵌时间戳（无滑动续期）。于是身份从首次签发起最多只有 5 分钟总寿命；用户读长回复/跑命令离线超过 5 分钟后，下一次请求被判超窗 → 网关静默铸造全新 client_id → 工单 `case.client_id` 仍是旧 id → `verify_conversation_ownership` 判定「换了人」→ 403。
2. **错误掩盖（放大器）**：`proxy_request_stream` 把任何非 200 上游响应一律包装成「上游服务暂时不可用」（detail 仅 `status N`），使 403 权限拒绝被伪装成上游不可用，严重误导排障方向。

**修复：**
- `shared/security/identity.py`：身份 Cookie 过期语义改为对齐浏览器 Cookie 寿命的 `IDENTITY_MAX_AGE_SECONDS`（默认 30 天），新增 `refresh_identity`（保持 client_id 不变仅刷新时间戳）；出口签名 `signature.py` 的 300s 防重放窗口**保持不变**（每请求重签，语义不同）。
- `api-gateway/app/middleware/identity.py`：验签成功即滑动续期回写 Cookie，令活跃会话身份绝不轮换；Cookie `max_age` 对齐 `IDENTITY_MAX_AGE_SECONDS`。
- `api-gateway/app/routes/conversations.py`：`proxy_request_stream` 按状态分类透传真因——401/403 → `SESSION_INVALID`「会话身份已失效或无权访问，请刷新页面后重新进入工单」，其他 4xx → `VALIDATION_ERROR`，5xx 才报「上游服务暂时不可用」；并记 `gateway_upstream_error` 日志含真实 status/detail。

**预防：**
- 「无登录的服务端签名身份」不得复用「每请求一次性 nonce」的短防重放窗口；身份 Cookie 必须做滑动续期或长寿命，否则任何有限空闲窗口都会被「用户走开一会儿」击穿。
- 网关代理兜底错误**禁止**把 4xx 权限/校验类响应笼统降级为「上游不可用」，必须透传真实状态与 detail，否则可观测性反噬排障。
- 回归测试：`tests/unit/test_security_hardening.py::TestIdentityCookieLifetime`（超 300s 仍验签通过、超 30 天/未来时间戳拒绝、refresh 保身份）、`backend/api-gateway/tests/unit/test_identity_sliding_renewal.py`（合法/陈旧 Cookie 身份不轮换 + 滑动续期）、`backend/api-gateway/tests/unit/test_conversations_upstream_error.py`（403 不再伪装为上游不可用）。

## V-020：S0 意图识别「已正确识别却报无法识别」——harness 解析契约对特定模型输出风格过拟合

**症状：** 用户在 S0 描述故障，AI 明明已给出「确认分类：虚拟机-017 …」的正确判断，却紧接着回复「抱歉，暂时无法识别您的故障类型」。工单 Q2026101493534 复现；DB 中会话停在 `diagnostic_stage=S0`、`category_id` 为空，agent-service 日志报 `intent_parse_failed`（`reply_preview` 里其实含合法分类码）。切换到 Qwen3.8-Flash 后愈发频繁。

**根因：** 旧范式是「Prompt 约定文本标记 + 下游正则抠字」——`triage_agent._parse_intent_result` 的 `confirmed_pattern` 只认字面量「已确认故障分类/故障分类」，`candidate_pattern` 只认行首 ①②③④。该契约对 glm-5 的输出习惯**隐式过拟合**（默认模型即 glm-5）。Qwen3.8-Flash 书写风格漂移：改写「**确认分类**：」（缺「故障」二字）、用 `**粗体**` 包裹分类码（`*` 不在字符类）、用 `- ` 短横线代替圈号候选行——任一即令正则提取集为空，被静默降级为「无法识别」的**假阴性**。

**修复（解耦「模型文本风格」与「解析契约」，业界结构化输出范式）：**
- **主链路**：S0 由 `chat_completion_stream` 改为 `ai_client.invoke(..., response_format={"type":"json_object"})`，Prompt 强制模型只输出 JSON 对象 `{reasoning, decision, category_code, candidate_codes, clarification_question}`；harness 读字段并经权威分类字典 `cat_map` 校验（`_extract_json_object` → `_intent_from_structured`）。面向用户展示 `reasoning`，决策走结构体，不再正则抠 Markdown。
- **多层兜底**：JSON 解析失败 → 剥 ```` ```json ```` 围栏/截取首个 `{...}` 重试 → **模型无关归一化文本解析**（`_normalize_reply` 剥离 `**`/反引号、`_CONFIRM_MARKERS` 兼容「确认分类」等同义词、`_BULLET` 兼容 `- * • ①-⑳ 1.`）→ 确定性关键词候选 → 受控降级。故「新代码 + 旧文本 Prompt」也能靠归一化兜底正常工作，向后兼容迁移滞后。
- **可观测性**：新增 `intent_contract_drift`（模型未按 JSON 契约返回但走了文本兜底），与真实 `intent_parse_failed`（无任何可用分类）区分。
- **Prompt 三处同步**：`database/seeds/02_system_prompts.sql`、`database/data-migrations/045_s0_intent_structured_output_prompt.sql`、`backend/shared/utils/prompt_loader.py` mock 模板保持一致；模板 JSON 字面花括号必须写成 `{{ }}` 以通过 `str.format`。

**预防：**
- **禁止用正则解析 LLM 自由文本作为控制流依据**；需要结构化决策时用 `response_format=json_object`/function calling，让模型产出机器可读信封、harness 消费并校验结构。
- 任何「按某个特定模型的输出习惯调出来的解析器」都是定时炸弹：换模型即回退。解析层必须**模型无关**（归一化 + 同义兼容 + 权威字典校验），并保留分层兜底与漂移告警。
- 回归测试：`backend/agent-service/tests/unit/test_triage_agent.py` 的 `TestExtractJsonObject`/`TestIntentFromStructured`/`TestNormalizationFallback`，含真实 Qwen 漂移样本（`确认分类：**虚拟机-017 …**`、`- ` 短横线候选）与「契约漂移端到端仍弹确认卡」用例。

## V-022：挤牙膏式复发的根因是「层间契约缝隙无人覆盖」——发布前须用 Live E2E smoke 守贯穿链路

**症状：** 硬盘坏道工单 Q2026101511258 的会话控制链路反复「今天修一个、明天冒一个」：#1149（前端变量提交态机）、V-020（S0 意图解析假阴性）、#1151/V-021（S2 幻觉追问）相继出现。每一次单点修复都各自补了隔离单测，但下一层缝隙又在下一次真实工单里暴露——问题不是「没测」，而是「各层各自测、缝隙没人测」。

**根因：** 分层测试金字塔缺了贯穿真实代码路径的会话控制链路级（L3）验收：
- agent-service 单测用假 `replay_runner`/桩 LLM，从不发起真实 `/interactive-response` 与变量池落库；
- conversation-service 单测 mock 掉 agent_client，从不消费真实 SSE `event: interactive_request`；
- 前端 jsdom 组件测（`MessageBubble.variable-submit.spec.ts`）守的是渲染态机，与后端真值不同维度。

于是「SSE 事件 `kind` 契约 / 签名身份头 / 变量池 `context_variables` 写入时序 / 引擎侧过滤守卫是否真的触发」这类**跨层契约**始终处于测试盲区，只能靠线上工单挤牙膏发现。

**修复（补 L3 Live E2E smoke，可复现防回潮）：**
- `evaluation/live_e2e/`：`asserts.py`（纯逻辑：SSE 解析、交互卡归一化、user_questions 落地性、结论无溯源数值、5 条跨层不变量汇总）、`driver.py`（httpx 异步：建案→建会话→流式 SSE→遇 `variable_input` 按应答脚本提交 `/interactive-response`，**复刻 #1149 时序：先确认后端变量池落库、pending 清空再触发续跑消息**→循环至终态）、`smoke.py`（CLI，`--attempts` 多数表决治理真实 LLM 非确定性）、`scenarios/disk_badtrack.json`（场景 spec）。
- `scripts/hci-sim/disk_badtrack_llm_stub.py`：确定性对抗 LLM 桩，把历史坏输出（重复索取已提交 `disk_dev`、凭空索要未声明 `disk_sn/disk_type`）**确定性地**注入真实 agent-service，逼 `filter_ungrounded_user_questions` 守卫**真的触发**并被断言捕获——实现验收标准『破坏缝隙能验红』。
- `make live-e2e-disk-badtrack`（`scripts/verify/live_e2e_disk_badtrack.sh`）：全栈 compose 编排 + 端口暴露覆盖（`evaluation/live_e2e/docker-compose.live-e2e.yml`，刻意置于 evaluation/ 下不进 CI/config-contract-check）+ `trap` 收尾 teardown。
- **断言只打结构不变量 + 守卫事件**（`agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}` 计数增量、`sop/execution.context_variables` 落库），绝不断言逐字文本——真实 LLM 输出不可复现，断言文本即假阳性/假阴性之源。

**预防：**
- 每修一个跨层挤牙膏 bug，**必须**在 `evaluation/live_e2e/asserts.py` 增/挂一条对应不变量（历史 bug ↔ 判据一一映射），否则该缝隙仍无回归守护。
- 定位：**发布前 gate，不进每 PR CI**（依赖 docker+LLM+bridge，慢且非确定性）；其纯逻辑（解析/判定/断言/桩）脱网单测进 CI（`tests/unit/test_live_e2e_*.py`、`test_disk_badtrack_llm_stub.py`），端到端实跑在具备全栈的宿主机执行。
- 本 L3 是后端会话链路级，不含真实浏览器渲染；#1149 那类前端态机缝隙仍由 jsdom 组件测守，如需浏览器级另起 L2 playwright。
- 运行手册见 `../evaluation/硬盘坏道LiveE2E运行手册.md`。

## V-028：端到端仿真 harness 必须复刻「采集器过滤语义 + 变量前提 + 主键类型」，否则同时产出假绿与假红

**症状：** 新建的「信号双路」端到端验证脚本（真实 DB 行 + 真实 CDD 链路 + 唯一替身 `_execute_acquirer`）首版跑出三类互相矛盾的结论：①健康态现场里 KBD 202/305/982 被判 SUPPORTED（假阳性）；②健康态/兄弟篇里根因路只显示 `BLOCKED`，没有真正反证（假阴性，掩盖了「根因信号是否有判别力」这一核心问题）；③加载候选时 `StopIteration`（假失败）。

**根因：** 替身只替换了最外层物理采集，却用**自己的简化语义**解释 `acquire.args`，与被测链路真实语义不一致：
1. `qkv_task` 的 `keyword` + `is_failed` 在真实 handler 里是**过滤条件**，仿真里被忽略 → 只要求「存在失败任务」的生产者型 must 在任意健康现场都能满足（这正是产品缺陷被仿真放大的地方）；
2. 根因 `acquire` 用 `{{HOST}}`/`{{DATE}}`，这两个变量由关键路 `qkv_task` 生产者填充；仿真场景没给变量池 → 采集前置校验直接 BLOCKED，信号根本没求值，自然永远不会 REJECTED；
3. asyncpg 返回 **int 主键**，而内部候选/信号键规范为 **str**（`kbd.id` 来自 JSON），未统一即键空间漂移。

**修复：**
- 仿真 World 实现 `task_records()`，逐条复刻 `keyword` 子串 + `is_failed` 过滤 + `limit` 截断；未命中返回 `("[]", None, False, None)`，绝不「有任务就算命中」；
- 负向场景（健康态/兄弟篇）显式补齐 `env={"HOST","DATE"}`，让根因路**真正求值到 REJECTED**，而不是在变量缺失处短路；
- `_load_rows` 统一 `row["id"]=str(...)`/`row["support_id"]=str(...)`，与内部键空间对齐；
- 把三条修正写进脚本 docstring，并标注「这是仿真保真度问题，不是产品缺陷」——避免后人把 harness 的错当成产品的错去改产品。

**预防：**
- **唯一替身原则**：e2e 只替换不可控 IO（真实网络/SSH/ACLI），其余必须走生产代码；替身一旦开始「解释参数」，就必须逐条对齐被替换层下游的真实语义（过滤条件、默认值、类型）。
- **负向控制组是必需品**：只证明「故障态命中」不能排除万能匹配，必须同时断言「健康态零命中 + 兄弟篇不误命中目标」；且负向场景要保证被测代码**能求值到反证**（变量/前置条件齐全），否则 `BLOCKED` 会被误读成「判别力成立」。
- **键空间按真实驱动对齐**：DB 驱动的 int 主键、JSON 里的 str 主键、`support_id` 的 str 语义必须显式统一，不能依赖 Python 隐式比较。
