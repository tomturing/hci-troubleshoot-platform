# 在线模式 vs 离线诊断模式：从 KBD/信号到诊断结论的完整路径

> 文档版本：V1.1
> 创建日期：2026-08-07
> 基于代码基线：分支 `codex/offline-diagnosis-p0-foundation`
> 关联文档：业务设计 V3.4、需求说明 V3.4、里程碑 V2.5
> **V1.1 关键变更**：架构重构为"离线 = 在线 CDD + AcquisitionProvider 适配"

---

## 一、共享起点：KBD + signals_json (v2)

无论在线还是离线，KBD 条目和它的 `signals_json` (v2) 是唯一的诊断起点。信号按数据流向分为两类：

**生产者信号（QKV 前端信号）**——查询 HCI API/数据库，产出变量供下游使用：

| 信号类型 | acquire.tool | 关联 acli 命令 | 核心属性 |
|---------|-------------|--------------|---------|
| `qkv_alert` | 告警查询 | `acli --formatter json alert get -k {keyword} -l {limit}` | `instruction`、`keyword`、`limit`、`produces` |
| `qkv_task` | 任务查询 | `acli --formatter json task get -k {keyword} [-s failed] -l {limit}` | `instruction`、`keyword`、`limit`、`is_failed`、`produces` |
| `qkv_dialog` | 弹框日志 | `acli log get -k {keyword} -p {path} -c {context_lines}` | `instruction`、`keyword`、`paths`、`context_lines`、`produces` |

**消费者信号（QFK 后端信号）**——执行 acli 命令查询节点状态，通过 matcher 判定真伪：

| 信号类型 | acquire.tool | 关联 acli 命令 | 共有属性 | 特有属性 |
|---------|-------------|--------------|---------|---------|
| `qfk_log` | 日志检查 | `acli log get -k {keyword} [-f {file}] [-p {path}] [-t {time}]` | `instruction`、`host`、`vm`、`keyword`、`timeout`、`expected`、`match_mode` | `file`(必填)、`end`、`source_family`、`parser`、`context_lines` |
| `qfk_system` | 系统检查 | `acli system {command}` | 同上 | `command`(必填)、`container` |
| `qfk_service` | 服务检查 | `acli service {service} {action}` | 同上 | `service`(必填)、`action` |
| `qfk_vm` | 虚拟机检查 | `acli vm {command}` | 同上 | `command`(必填) |
| `qfk_network` | 网络检查 | `acli network {command}` | 同上 | `command`(必填) |
| `qfk_storage` | 存储检查 | `acli storage {command}` | 同上 | `command`(必填) |
| `qfk_hardware` | 硬件检查 | `acli hardware {command}` | 同上 | `command`(必填) |
| `qfk_platform` | 平台检查 | `acli platform {command}` | 同上 | `command`(必填) |

每个信号在 v2 模式中包含以下结构：

```json
{
  "id": "sig_002",
  "role": "must",
  "acquire": {
    "tool": "qfk_system",
    "args": { "command": "lsof", "container": "asv-con" }
  },
  "match": {
    "type": "keyword",
    "mode": "or",
    "extract": {
      "type": "text",
      "rows": { "include": ["qemu"] }
    },
    "pattern": ["qemu-kvm"]
  },
  "orchestrate": {
    "produces": ["KVM_PID"],
    "requires": ["TASK_ID"]
  },
  "provenance": { "needs_review": false },
  "review": { "require_human_confirm": false }
}
```

**关键依赖关系**：

- 生产者信号（QKV）产出的变量是消费者信号（QFK）的前置依赖。例如 `qkv_alert` 产出 `KVM_PID` → `qfk_system` 的 `command=lsof` 消费 `KVM_PID` 作为参数；
- `acquire` 声明了采集工具和参数，`match` 声明了判定规则，`orchestrate.produces/requires` 声明了变量依赖图；
- 共享属性中 `host`、`vm`、`end` 等标记为"变量池获取"的字段，其值不在 KBD 中写死，而是由上游生产者信号或环境上下文在运行时注入。

---

## 二、信号输出模式与取值配置

每个信号（无论生产者还是消费者）的输出统一为两种模式：

### 2.1 匹配模式（match）——判断证据是否符合条件

在取值完成后，根据判定类型完成检测。当前支持 7 种判定类型：

| 判定类型 | 英文标识 | 检测逻辑 | 典型场景 |
|---------|---------|---------|---------|
| 关键字匹配 | `keyword` | 在取值文本中搜索关键字（支持 or/and/not 组合） | 日志中出现 "Connection reset" |
| 正则表达式 | `regex` | 用正则匹配取值文本 | 匹配 IP 地址或错误码格式 |
| 状态判定 | `state` | 匹配特定状态值（大小写不敏感） | 服务状态为 "running" |
| 数值阈值 | `threshold` | 数值比较（gt/gte/lt/lte/eq/neq） | 延迟 > 100ms |
| 首末差值 | `delta` | 周期日志计数器的首末差值 vs 目标值 | 错误计数增长 > 0 |
| 变化趋势 | `trend` | 连续采样的方向判定（increasing/decreasing/stable） | RX 丢包持续增长 |
| 存在性判定 | `exists` | 检查取值结果是否非空 | 进程列表中存在指定 PID |

### 2.2 产出变量模式（produce）——将取值结果写入变量池

在取值完成后，根据以下属性完成变量注入：

| 属性 | 说明 | 示例 |
|------|------|------|
| 变量名 (`name`) | 变量池中的唯一标识（必填） | `KVM_PID`、`DISK_SN` |
| 变量类型 (`type`) | `string` / `integer` / `number` / `boolean` / `array` / `object` / `array<object>` | `integer` |
| 变量值 (`value`) | 从取值结果中提取的实际值 | `12345` |

### 2.3 取值配置（extract）——两种模式共用

| 取值方式 | 英文标识 | 行选择 | 列选择 | 结果数量 | 输出来源 | 适用场景 |
|---------|---------|--------|--------|---------|---------|---------|
| 完整输出 | `full` | 不筛选 | 不筛选 | `all` | stdout | 纯文本日志搜索 |
| 文本行列 | `textExtract` | `all`/`keywords`(include/exclude)/`indices`+`ranges` | `header`(name+aliases)/`index` | `all`/`first`/`last`/`exactly_one` | stdout | 表格式命令输出（ps、lsof、iostat） |
| JSON 路径 | `jsonExtract` | 不适用 | 点号分隔路径（禁止 jq/通配符） | `all`/`first`/`last`/`exactly_one` | stdout（JSON 输出） | acli --formatter json 命令 |

三种取值方式之后，均有一个**可选的 AI 提取后置步骤**：当确定性取值无法精确捕获目标值（如从非结构化文本中提取数值），由 LLM 从已取值结果中二次提取。AI 提取仅作用于已筛选的取值结果，不得绕过确定性取值直接处理原始输出。

---

## 三、路径一：在线模式（terminal_bridge）

```
┌──────────────────────────────────────────────────────────────────────┐
│                       在线模式数据流                                   │
│                                                                      │
│  S0: 用户描述 → TriageAgent → CategoryDecision(category_id=虚拟机-003) │
│   │                                                                  │
│   ▼                                                                  │
│  S1: kb_search(category_id) → 该分类下全部已发布 KBD                    │
│   │   返回 KBD 27123 + 其他同分类 KBD                                  │
│   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ KBDDiagnostic.diagnose()                                     │     │
│  │ agent-service/adapters/agents/htp/kbd_differential.py:183    │     │
│  │                                                              │     │
│  │ Step 1: compile_signal_plan(candidates)                      │     │
│  │   解析每篇 KBD 的 signals_json                                │     │
│  │   → SignalPlan { acquisitions[], signal_refs[], variables }  │     │
│  │   → 建立 produces → requires 变量依赖图                       │     │
│  │                                                              │     │
│  │ Step 2: ActiveDiagnosticScheduler                            │     │
│  │   按信息增益 + 成本 + 解锁价值选择下一个 acquisition             │     │
│  │   utility = wd*discrimination + wc*coverage + wu*unlock      │     │
│  │                                                              │     │
│  │ Step 3: 循环执行 acquisition → 直到停止条件满足                 │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   │  对每个 acquisition（按调度器选择顺序）:                             │
│   │                                                                  │
│   ├── QKV 生产者信号                                                  │
│   │   qkv_exec(signal, conversation_id, node_ip)                     │
│   │   agent-service/tools/qkv/engine.py:87                           │
│   │   │                                                              │
│   │   │  1. build_commands(signal): "acli --formatter json           │
│   │   │     task get -k 备份 -s failed -l 100"                       │
│   │   │                                                              │
│   │   │  2. BridgeRelayExecutor.execute()                            │
│   │   │     ├── WebSocket → terminal_bridge → SSH → 客户主机          │
│   │   │     └── 返回: stdout + stderr + exit_code                    │
│   │   │                                                              │
│   │   │  3. parse_frontend_value(stdout, produces)                   │
│   │   │     解析 JSON → 提取变量: TASK_ID=12345, TASK_STATUS=failed   │
│   │   │                                                              │
│   │   │  4. variable_pool.set("TASK_ID", 12345)                      │
│   │   │     variable_pool.set("TASK_STATUS", "failed")               │
│   │   │                                                              │
│   │   │  输出: QKVResult(success=True, values=[...])                 │
│   │   │                                                              │
│   │   └── 前端展示: "查询到 3 条失败备份任务"                            │
│   │                                                                  │
│   ├── QFK 消费者信号                                                  │
│   │   qfk_exec(signal, conversation_id, node_ip)                     │
│   │   agent-service/tools/qfk/engine.py:122                          │
│   │   │                                                              │
│   │   │  1. 变量注入: 从 variable_pool 读取 requires 变量              │
│   │   │     command = f"acli system lsof" (TASK_ID 注入到 args)      │
│   │   │                                                              │
│   │   │  2. HandlerRegistry.get(namespace).build_commands(signal)     │
│   │   │     → ["acli system lsof --container asv-con"]               │
│   │   │                                                              │
│   │   │  3. BridgeRelayExecutor.execute()                            │
│   │   │     ├── WebSocket → terminal_bridge → SSH → 客户主机          │
│   │   │     └── 返回: stdout (进程列表) + stderr + exit_code          │
│   │   │                                                              │
│   │   │  4. 终端故障哨兵检查                                           │
│   │   │     "SSH 会话不存在"/"执行超时" → 硬失败，不进 matcher          │
│   │   │                                                              │
│   │   │  5. extract_output_values(stdout, match.extract)             │
│   │   │     agent-service/tools/qfk/extractor.py                     │
│   │   │     按 rows.include=["qemu"] 过滤行                           │
│   │   │     → 提取匹配行文本                                          │
│   │   │                                                              │
│   │   │  6. evaluate_matcher(matcher, extracted_text)                │
│   │   │     agent-service/tools/qfk/matcher.py:75                    │
│   │   │     keyword mode="or", pattern=["qemu-kvm"]                  │
│   │   │     → MatcherResult(matched=True, detail={...})              │
│   │   │                                                              │
│   │   │  7. _evaluate_signal_outcome(signal, result)                 │
│   │   │     PASS → signal outcome = MATCHED                          │
│   │   │                                                              │
│   │   └── 输出: QFKResult(matched=True, evidence="...")              │
│   │                                                                  │
│   └── 每轮执行后:                                                     │
│       reduce_candidates(plan, assessments)                           │
│       → 按信号结果更新每篇 KBD 的候选状态:                              │
│         SUPPORTED / REJECTED / INCONCLUSIVE / NOT_EXECUTABLE         │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ 停止条件（满足任一）:                                          │     │
│  │ 1. 全部候选达到终态                                           │     │
│  │ 2. ≥1 SUPPORTED 且其余均为 SUPPORTED/REJECTED                │     │
│  │ 3. 无可执行 acquisition                                       │     │
│  │ 4. 达到预算/安全/用户边界                                      │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   ▼                                                                  │
│  Conclusion Gate                                                     │
│  ├── DEFINITIVE: ≥1 SUPPORTED, 其余终态                               │
│  ├── PARTIAL: ≥1 SUPPORTED, 仍有 INCONCLUSIVE                         │
│  ├── INCONCLUSIVE: 无 SUPPORTED, 有未决候选                           │
│  └── NO_MATCH: 全部 REJECTED                                         │
│   │                                                                  │
│   ▼                                                                  │
│  S4: 报告生成 → KBD 27123 SUPPORTED → 根因: "qemu-kvm 进程 segfault"   │
│       证据引用: sig_002 MATCHED (lsof 确认 qemu-kvm 存在)              │
│               sig_003 MATCHED (error.log 中 12 条 segfault)           │
└──────────────────────────────────────────────────────────────────────┘
```

### 在线模式关键代码路径

| 步骤 | 代码位置 | 函数 |
|------|---------|------|
| 编译信号计划 | `agent-service/adapters/agents/htp/kbd_differential.py:203` | `compile_signal_plan()` |
| 调度器选择 | `agent-service/adapters/agents/htp/kbd_differential.py:253` | `scheduler.choose()` |
| QKV 执行 | `agent-service/tools/qkv/engine.py:87` | `qkv_exec()` |
| QFK 执行 | `agent-service/tools/qfk/engine.py:122` | `qfk_exec()` |
| 结构化取值 | `agent-service/tools/qfk/extractor.py` | `extract_output_values()` |
| Matcher 求值 | `agent-service/tools/qfk/matcher.py:75` | `evaluate_matcher()` |
| 候选归约 | `agent-service/adapters/agents/htp/kbd_differential.py` | `reduce_candidates()` |
| 信号结果评估 | `agent-service/adapters/agents/htp/kbd_differential.py:717` | `_evaluate_signal_outcome()` |
| 变量池 | `agent-service/app/memory/variable_pool/engine.py` | `VariablePool` |

---

## 四、路径二：离线诊断模式

离线诊断模式分为两个阶段：**Phase 0 资源准备**（管理员操作，非每次诊断执行）和 **Phase 1-8 诊断执行**（客户触发）。

### Phase 0: 资源准备（Offline Resource Sync）

```
┌──────────────────────────────────────────────────────────────────┐
│ Offline Resource Sync                                            │
│ diagnosis-service/services/offline_resource_sync_service.py       │
│                                                                  │
│ 1. extract_requirements(kbd)                                     │
│    读取 kbd.signals_json.signals[]                                │
│    对每个 signal:                                                 │
│      acquire.tool → normalize_acquirer() → tool name             │
│      acquire.args → 参数绑定                                      │
│      match → matcher 契约                                         │
│                                                                  │
│ 2. build_tool_collector_candidate(requirement, tool)              │
│    从 Tool Registry 读 usage_template                             │
│    → 编译为 Collector 命令模板 + 参数 snapshot                      │
│    → collector_id = kbd_{tool}_{fingerprint[:12]}                │
│                                                                  │
│ 3. 生成三类资源:                                                   │
│    ├── Collector Definition (managed_by=kbd_sync)                │
│    ├── Offline Signal Mapping                                    │
│    │   acquire_tool → collector_id (含 category_scope,           │
│    │   command_scope, query_type, field_mapping)                 │
│    └── Collection Profile (per scenario)                         │
│        items[] → collector_id + required_level                   │
│                                                                  │
│ 输出: 4 个 Profile, 15+ Collector, N 条 Signal Mapping            │
└──────────────────────────────────────────────────────────────────┘
```

**KBD Signal 到 Collector 的映射示例**：

```text
KBD 27123 / signals_json:

  sig_001: acquire.tool = "qkv_task"
    → Collector: kbd_qkv_task_abc123
    → Signal Mapping: (qkv_task, *, *, kbd_qkv_task_abc123, query_type=json)

  sig_002: acquire.tool = "qfk_system", args.command = "lsof"
    → Collector: kbd_qfk_system_def456
    → Signal Mapping: (qfk_system, *, lsof, kbd_qfk_system_def456, query_type=command_output)

  sig_003: acquire.tool = "qfk_log", args.file = "error.log"
    → Collector: kbd_qfk_log_ghi789
    → Signal Mapping: (qfk_log, *, error.log, kbd_qfk_log_ghi789, query_type=log)
```

### Phase 1-8: 诊断执行

```
┌──────────────────────────────────────────────────────────────────────┐
│                      离线诊断模式数据流                                 │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ Phase 1: 客户创建诊断会话                                      │     │
│  │                                                              │     │
│  │ POST /api/diagnosis-sessions                                 │     │
│  │   case_id: "Q2026080700001"                                  │     │
│  │   selected_scenario: "vm_start_failed"                       │     │
│  │   incident: { start_time, end_time, timezone }               │     │
│  │   affected_objects: [{ type: "vm", id: "vm-027" }]           │     │
│  │                                                              │     │
│  │ → DiagnosisSession { session_id, status: "created" }         │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ Phase 2: 生成采集计划                                          │     │
│  │                                                              │     │
│  │ POST /api/diagnosis-sessions/{id}/collection-plans            │     │
│  │ 加载 Collection Profile (vm_start_failed)                     │     │
│  │ → CollectionPlan:                                            │     │
│  │   ├── qkv_task Collector (mandatory)  ← 生产者信号             │     │
│  │   ├── qfk_system/lsof Collector (mandatory) ← 消费者信号       │     │
│  │   ├── qfk_log/error.log Collector (mandatory)                 │     │
│  │   └── ... (recommended/conditional/deferred)                  │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ Phase 3: 生成签名采集器制品                                     │     │
│  │                                                              │     │
│  │ POST .../collector-artifacts                                 │     │
│  │ 读取 Collector Revision 快照 → 渲染命令模板                     │     │
│  │ → Structured Collector Artifact (schema 1.2):                │     │
│  │   {                                                          │     │
│  │     "items": [                                               │     │
│  │       {"collector_id": "kbd_qkv_task_abc123",                │     │
│  │        "argv": ["acli", "--formatter", "json", "task",      │     │
│  │                "get", "-k", "备份", "-s", "failed", "-l","100"]},│     │
│  │       {"collector_id": "kbd_qfk_system_def456",              │     │
│  │        "argv": ["acli", "system", "lsof",                    │     │
│  │                "--container", "asv-con"]},                   │     │
│  │       {"collector_id": "kbd_qfk_log_ghi789",                 │     │
│  │        "argv": ["acli", "log", "get", "-k", "segfault",     │     │
│  │                "-f", "error.log"]}                           │     │
│  │     ],                                                       │     │
│  │     "signature": { "algorithm": "Ed25519", ... }             │     │
│  │   }                                                          │     │
│  │                                                              │     │
│  │ → 客户下载 Verification Bundle (hci-collect-linux-amd64 +    │     │
│  │     签名制品 + case.json + Trust Store + Revocation List)     │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ Phase 4: 客户侧执行 + 上传                                     │     │
│  │                                                              │     │
│  │ 客户主机上:                                                    │     │
│  │ $ ./hci-collect-linux-amd64 \                                │     │
│  │     --expected-root-fingerprint <指纹>                        │     │
│  │                                                              │     │
│  │ Go 运行时依次:                                                 │     │
│  │ 1. 校验自身 SHA-256 + Manifest 签名                            │     │
│  │ 2. 校验采集制品签名 + 有效期 + 吊销清单                          │     │
│  │ 3. 展示采集范围，等待用户确认 (--yes 跳过)                       │     │
│  │ 4. 逐项执行 argv（直接 exec，不经 /bin/sh）                     │     │
│  │    每项记录: exit_code, stdout_bytes, stderr_bytes,           │     │
│  │    truncated, duration_ms                                    │     │
│  │ 5. 单项失败不终止，继续下一项                                   │     │
│  │ 6. 生成 execution_manifest.json                               │     │
│  │ 7. AES-256-GCM 信封加密打包 → evidence.tar.gz.enc             │     │
│  │                                                              │     │
│  │ 上传:                                                         │     │
│  │ POST .../uploads (创建上传会话)                                │     │
│  │ PUT .../uploads/{id}/parts/{n} (分片直传，绕过 API Gateway)    │     │
│  │ POST .../uploads/{id}/complete → 触发异步 Worker              │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ Phase 5: 安全处理 + 证据标准化 (Diagnosis Worker)               │     │
│  │                                                              │     │
│  │ 1. magic number 校验 → tar.gz                                │     │
│  │ 2. 恶意文件扫描                                                │     │
│  │ 3. 路径穿越 + 符号链接 + 压缩炸弹检查                            │     │
│  │ 4. 信封解密 (RSA-OAEP-SHA256 unwrap → AES-256-GCM decrypt)    │     │
│  │ 5. 解压到隔离临时目录                                           │     │
│  │ 6. manifest.json 一致性校验 (SHA-256 per file)                │     │
│  │ 7. 逐文件入库 evidence_item (只追加，不可变):                    │     │
│  │    evidence_id, collector_id, source_path,                    │     │
│  │    media_type, collected_start/end, structured_data,          │     │
│  │    evidence_status (available/collection_failed/...),         │     │
│  │    sha256, size_bytes                                         │     │
│  │ 8. 清理临时明文目录                                             │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ Phase 6: 证据评估 (Evidence Assessment)                        │     │
│  │                                                              │     │
│  │ OfflineAnalysisService._calculate_assessment()                │     │
│  │                                                              │     │
│  │ 逐 Collection Plan Item 检查:                                  │     │
│  │   ├── qkv_task Collector → evidence_status=available         │     │
│  │   ├── qfk_system/lsof → evidence_status=available            │     │
│  │   ├── qfk_log/error.log → evidence_status=available          │     │
│  │   └── ...                                                    │     │
│  │                                                              │     │
│  │ completeness_score = round(100 * available / total)           │     │
│  │ ready_for_diagnosis = mandatory_available > 0 and score >= 40│     │
│  │                                                              │     │
│  │ → EvidenceAssessment { completeness_score: 100,              │     │
│  │     ready_for_diagnosis: true }                              │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ Phase 7: 离线诊断 (核心对标在线 CDD)                             │     │
│  │                                                              │     │
│  │ OfflineAnalysisService.assess_and_diagnose()                  │     │
│  │ diagnosis-service/services/offline_analysis_service.py:145    │     │
│  │                                                              │     │
│  │ Step 1: _evaluate_kbds(session_row, evidence)                │     │
│  │   查询 kbd_entry WHERE category_id = ANY(相邻分类)             │     │
│  │   AND status='published' AND signals_json <> '[]'            │     │
│  │                                                              │     │
│  │ Step 2: 对每篇 KBD 的每个 signal, 调用 _evaluate_signal():     │     │
│  │                                                              │     │
│  │   2a. 查找 Offline Signal Mapping:                            │     │
│  │       acquire_tool="qfk_system" + category_scope +           │     │
│  │       command_scope → collector_id                           │     │
│  │                                                              │     │
│  │   2b. 从 evidence_item 表查找对应 Collector 的证据:             │     │
│  │       WHERE collector_id IN (...) AND evidence_status         │     │
│  │                                                              │     │
│  │   2c. 如果证据 available:                                      │     │
│  │       ┌──────────────────────────────────────────────┐       │     │
│  │       │ ★ 当前实现 (有差距):                           │       │     │
│  │       │   _evaluate_matcher(matcher, structured_data) │       │     │
│  │       │   → 仅支持 keyword/equals/numeric 4 种类型     │       │     │
│  │       │   → 不做 extract_output_values 结构化取值      │       │     │
│  │       │                                              │       │     │
│  │       │ ★ 目标实现 (对齐在线):                         │       │     │
│  │       │   1. extract_output_values(stdout, extract)   │       │     │
│  │       │   2. evaluate_matcher(matcher, extracted)     │       │     │
│  │       │      → 支持全部 7 种类型                       │       │     │
│  │       └──────────────────────────────────────────────┘       │     │
│  │                                                              │     │
│  │   2d. 状态映射:                                                │     │
│  │       matched=True → MATCHED                                 │     │
│  │       matched=False → NOT_MATCHED                            │     │
│  │       无证据/证据失败 → UNKNOWN                                │     │
│  │                                                              │     │
│  │   2e. ★ 当前缺失: produce 变量处理                             │     │
│  │       目标: QKV 信号取值后 → variable_pool.set(name, value)    │     │
│  │             QFK 信号评估前 → variable_pool.get(requires)       │     │
│  │             依赖未就绪 → BLOCKED (非 UNKNOWN)                  │     │
│  │                                                              │     │
│  │ Step 3: 候选评分:                                              │     │
│  │   matched = sum(state == MATCHED)                             │     │
│  │   not_matched = sum(state == NOT_MATCHED)                     │     │
│  │   unknown = sum(state == UNKNOWN)                             │     │
│  │   coverage = (matched + not_matched) / total                  │     │
│  │   score = max(0, min(1, (matched/total)*0.75                  │     │
│  │            + coverage*0.25 - not_matched*0.15))               │     │
│  │                                                              │     │
│  │   取 Top-10 候选 → diagnosis_candidate 表                     │     │
│  │   所有 signal 评估 → signal_evaluation 表                      │     │
│  └─────────────────────────────────────────────────────────────┘     │
│   │                                                                  │
│   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ Phase 8: 结论策略 + 报告生成                                    │     │
│  │                                                              │     │
│  │ _conclusion(assessment, candidates):                         │     │
│  │   无候选或无就绪证据 → Insufficient                            │     │
│  │   top.score >= 0.85 ∧ unknown == 0 → Confirmed               │     │
│  │   top.score >= 0.6 ∧ matched > 0 → Probable                  │     │
│  │   matched > 0 ∧ not_matched > 0 → Conflicted                 │     │
│  │   其余 → Suspected                                            │     │
│  │                                                              │     │
│  │ ★ 目标: 复用在线 Conclusion Gate, 而非硬编码阈值                │     │
│  │                                                              │     │
│  │ _create_report() → diagnosis_report 表                        │     │
│  │   状态: draft → review_pending → engineer_confirmed           │     │
│  │        → customer_published                                   │     │
│  └─────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────┘
```

### 离线模式关键代码路径

| 步骤 | 代码位置 | 函数 |
|------|---------|------|
| 资源同步-提取需求 | `diagnosis-service/services/offline_resource_sync_service.py:96` | `extract_requirements()` |
| 资源同步-构建 Collector | `diagnosis-service/services/offline_resource_sync_service.py` | `build_tool_collector_candidate()` |
| 资源同步-预览/发布 | `diagnosis-service/services/offline_resource_sync_service.py` | `preview()` / `publish()` |
| 创建会话 | `diagnosis-service/services/diagnosis_session_service.py` | `create()` |
| 证据评估 | `diagnosis-service/services/offline_analysis_service.py` | `_calculate_assessment()` |
| 离线诊断入口 | `diagnosis-service/services/offline_analysis_service.py:145` | `assess_and_diagnose()` |
| KBD 候选评估 | `diagnosis-service/services/offline_analysis_service.py:652` | `_evaluate_kbds()` |
| 单信号评估 | `diagnosis-service/services/offline_analysis_service.py:734` | `_evaluate_signal()` |
| 离线 Matcher | `diagnosis-service/services/offline_analysis_service.py:1493` | `_evaluate_matcher()` |
| 离线证据查询 | `diagnosis-service/services/offline_analysis_service.py:38` | `OfflineEvidenceProvider.query()` |
| 结论计算 | `diagnosis-service/services/offline_analysis_service.py:1531` | `_conclusion()` |

---

## 五、两种模式对照总表

```
                    在线模式                          离线模式
                    ────────                          ────────

数据获取通道
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ terminal_bridge (WebSocket│      │ 客户侧 Go 采集器           │
    │ → SSH → 客户主机)         │      │ → 证据包上传               │
    │ 实时执行 acli 命令         │      │ → 隔离 Worker 解包入库      │
    └──────────────────────────┘      └──────────────────────────┘

信号执行
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ ActiveDiagnosticScheduler │      │ 批量全信号评估             │
    │ 按信息增益逐轮选择         │      │ 所有信号一次性评估          │
    │ 可复用已执行的采集结果      │      │ 证据已在包中，不需要调度     │
    └──────────────────────────┘      └──────────────────────────┘

QKV 生产者
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ qkv_exec()                │      │ OfflineEvidenceProvider   │
    │ → BridgeRelayExecutor     │      │   .query(collector_id)    │
    │ → 实时 acli alert/task/   │      │ → evidence_item 表读取     │
    │   log get                 │      │   预采集的 JSON/日志       │
    │ → parse → variable_pool   │      │ → ★待实现: variable_pool   │
    └──────────────────────────┘      └──────────────────────────┘

QFK 消费者
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ qfk_exec()                │      │ _evaluate_signal()        │
    │ → BridgeRelayExecutor     │      │ → OfflineEvidenceProvider │
    │ → 实时 acli system/lsof   │      │   查询 evidence_item       │
    │ → extract_output_values() │      │ → ★待对齐: extract_output  │
    │ → evaluate_matcher()      │      │ → ★待对齐: evaluate_matcher│
    │ → MatcherResult           │      │ → MATCHED/NOT_MATCHED/    │
    │ → PASS/FAIL/UNKNOWN       │      │   UNKNOWN                 │
    └──────────────────────────┘      └──────────────────────────┘

Matcher
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ evaluate_matcher()        │      │ ★ 当前: _evaluate_matcher │
    │ matcher.py:75             │      │   (独立实现, 仅 4/7 类型)  │
    │ 7 种类型 + extract 规范    │      │ ★ 目标: 复用同一个函数      │
    └──────────────────────────┘      └──────────────────────────┘

候选归约
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ reduce_candidates()       │      │ ★ 当前: 覆盖率加权评分      │
    │ CandidateStateReducer     │      │   score = matched*0.75... │
    │ SUPPORTED / REJECTED /    │      │ ★ 目标: 复用 CDD 归约器    │
    │ INCONCLUSIVE / NOT_EXEC   │      │   和决策表达式             │
    └──────────────────────────┘      └──────────────────────────┘

结论门禁
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ ConclusionGate            │      │ _conclusion()             │
    │ DEFINITIVE / PARTIAL /    │      │ Confirmed / Probable /    │
    │ INCONCLUSIVE / NO_MATCH   │      │ Suspected / Insufficient  │
    │                           │      │ / Conflicted              │
    │ ★ 四级 → 五级由版本化      │      │ ★ 当前: 硬编码阈值          │
    │   ConclusionPolicy 映射   │      │ ★ 目标: 共享策略映射       │
    └──────────────────────────┘      └──────────────────────────┘

变量池
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ variable_pool/engine.py   │      │ ★ 当前: 不存在             │
    │ produces → set(name,val)  │      │ ★ 目标: 离线变量池         │
    │ requires → get(name)      │      │   从 IncidentContext 初始化 │
    │ 依赖拓扑排序               │      │   QKV produce → 写入       │
    │                           │      │   QFK requires → 注入      │
    └──────────────────────────┘      └──────────────────────────┘

报告
    ┌──────────────────────────┐      ┌──────────────────────────┐
    │ S4: 诊断报告               │      │ diagnosis_report 表        │
    │ 证据引用 + KBD 链接 +      │      │ draft → review_pending    │
    │ 根因 + 处置建议             │      │ → engineer_confirmed      │
    │                           │      │ → customer_published      │
    │                           │      │ 工程师审核后对客户发布       │
    └──────────────────────────┘      └──────────────────────────┘
```

---

## 六、关键收敛点：AcquisitionProvider 架构

### 6.1 架构核心

离线诊断 = 在线 CDD 内核 + `AcquisitionProvider` 适配层。

```text
┌─────────────────────────────────────────────────────────────────┐
│                      在线 CDD 内核（唯一权威）                      │
│                                                                 │
│  SignalPlanCompiler → ActiveDiagnosticScheduler                 │
│       → ConstrainedExecutor → DeterministicSignalEvaluator     │
│       → CandidateStateReducer → ConclusionGate                  │
│                                                                 │
│  所有确定性判定逻辑在此。离线不得独立实现等价逻辑。                     │
│  KBDDiagnostic.diagnose(candidates, provider, variable_pool)     │
└─────────────────────────────────────────────────────────────────┘
                    ▲                          ▲
                    │                          │
          ┌─────────┴─────────┐    ┌──────────┴──────────┐
          │ AcquisitionProvider│    │  AcquisitionProvider  │
          │ (在线实现)          │    │  (离线实现)           │
          ├────────────────────┤    ├─────────────────────┤
          │ BridgeRelayProvider │    │ OfflineEvidenceProvider│
          │ → terminal_bridge  │    │ → evidence_item 表    │
          │ → SSH → 客户主机    │    │   预采集的 JSON/日志   │
          │ 实时 acli 执行      │    │ 返回 AcquisitionResult │
          └────────────────────┘    └──────────────────────┘
```

**AcquisitionProvider 接口定义**：

```python
class AcquisitionProvider(Protocol):
    """在线 CDD 内核通过此接口获取采集数据。"""

    async def execute(
        self,
        acquisition: Acquisition,
        resolved_args: dict[str, str],
    ) -> AcquisitionResult:
        """执行一次采集，返回 stdout/stderr/exit_code。"""
        ...
```

两模式共享同一个 KBD 和 signals_json 起点，`AcquisitionProvider` 是唯一的分叉点。
在此接口之上，**所有组件（extract、matcher、变量池、候选归约、结论门禁）均为共享代码**。

### 6.2 当前差距与目标状态

| 组件 | 当前离线实现 | 目标 | 优先级 |
|------|------------|------|--------|
| `AcquisitionProvider` 接口 | 不存在，离线独立调用 `_evaluate_signal` | 在 `shared/` 定义接口，在线离线各自实现 | **P0 阻断** |
| `extract_output_values` | 离线 Provider 不做结构化取值 | 提取到 `shared/`，通过 Provider 接口被统一调用 | **P0 阻断** |
| `evaluate_matcher` | `_evaluate_matcher` 独立实现，仅 4/7 类型 | 复用 `matcher.py:75` 同一函数 | **P0 阻断** |
| `VariablePool` | 不存在 | 复用在线 `VariablePool`，从 IncidentContext 初始化 | **P0 阻断** |
| `KBDDiagnostic.diagnose()` 委托 | 离线独立实现 `_evaluate_kbds` + `_conclusion` | 删除独立实现，委托给在线 CDD 内核 | **P0 阻断** |
| QKV 离线适配 | QKV Collector 未纳入 Mandatory | QKV Collector 纳入 Mandatory + Provider 适配 JSON/日志查询 | **P0 阻断** |
| KBD 快照 | kbd_snapshot 缺失 revision/checksum | 补齐 kbd_revision 引用 | P0 阻断 |

### 允许差异

| 维度 | 在线 | 离线 | 原因 |
|------|------|------|------|
| `AcquisitionProvider` 实现 | `BridgeRelayProvider` (WebSocket → terminal_bridge → SSH) | `OfflineEvidenceProvider` (evidence_item 表查询) | 离线场景无 SSH，这是**唯一允许的差异点** |
| 执行调度 | `ActiveDiagnosticScheduler` 按信息增益逐轮选择 | 批量全信号评估（`execution_mode="batch"`） | 离线证据已全量可用，跳过调度器优化 |
| 结论等级映射 | 四级：DEFINITIVE/PARTIAL/INCONCLUSIVE/NO_MATCH | 五级：Confirmed/Probable/Suspected/Insufficient/Conflicted | 离线面向工程师审核，需要更细粒度的人可读等级 |

**不允许的差异**（红线）：
- 在 `diagnosis-service` 中独立实现 matcher/extractor/candidate reducer/conclusion gate；
- 离线因 matcher 类型不支持而返回 UNKNOWN；
- 离线跳过 `match.extract` 直接对原始文本做扁平匹配；
- 离线不处理 produces/requires 变量依赖。

---

## 七、信号类型覆盖矩阵

### QKV 生产者信号

| 信号 | 在线执行入口 | 离线 Collector 类型 | 离线证据查询类型 | 离线数据来源 | 当前状态 |
|------|------------|------------------|---------------|------------|---------|
| `qkv_alert` | `qkv/engine.py:qkv_exec()` | JSON 查询型 | `json` | `evidence_item` (commands/) | Collector 已生成，Provider 待适配 |
| `qkv_task` | `qkv/engine.py:qkv_exec()` | JSON 查询型 | `json` | `evidence_item` (commands/) | Collector 已生成，Provider 待适配 |
| `qkv_dialog` | `qkv/engine.py:qkv_exec()` | 日志查询型 | `log` | `evidence_item` (logs/) | Collector 已生成（is_log=True），Provider 待适配 |

### QFK 消费者信号

| 信号 | 在线执行入口 | 离线 Collector 生成 | 参数归一化 | 当前状态 |
|------|------------|------------------|----------|---------|
| `qfk_log` | `qfk/engine.py:qfk_exec()` | `build_tool_collector_candidate()` | `_qfk_log_parameters()` 专用 | ✅ 已实现 |
| `qfk_system` | `qfk/engine.py:qfk_exec()` | `build_tool_collector_candidate()` | `_requirement_bindings()` 通用 | ✅ 已实现 |
| `qfk_service` | `qfk/engine.py:qfk_exec()` | `build_tool_collector_candidate()` | `_requirement_bindings()` 通用 | ✅ 已实现 |
| `qfk_vm` | `qfk/engine.py:qfk_exec()` | `build_tool_collector_candidate()` | `_requirement_bindings()` 通用 | ✅ 已实现 |
| `qfk_network` | `qfk/engine.py:qfk_exec()` | `build_tool_collector_candidate()` | `_requirement_bindings()` 通用 | ✅ 已实现 |
| `qfk_storage` | `qfk/engine.py:qfk_exec()` | `build_tool_collector_candidate()` | `_requirement_bindings()` 通用 | ✅ 已实现 |
| `qfk_hardware` | `qfk/engine.py:qfk_exec()` | `build_tool_collector_candidate()` | `_requirement_bindings()` 通用 | ✅ 已实现 |
| `qfk_platform` | `qfk/engine.py:qfk_exec()` | `build_tool_collector_candidate()` | `_requirement_bindings()` 通用 | ✅ 已实现 |

### Matcher 类型覆盖

| Matcher 类型 | 在线 `evaluate_matcher` | 离线 `_evaluate_matcher` | 差距 |
|------------|----------------------|------------------------|------|
| `keyword` | ✅ 支持（含 mode: or/and/not + expected） | ✅ 支持（仅 pattern 扁平匹配，无 mode/extract） | 缺 mode/extract/expected |
| `regex` | ✅ 支持（re.search IGNORECASE\|DOTALL） | ❌ 不支持 → 返回 None → UNKNOWN | **全部缺失** |
| `state` | ✅ 支持（casefold 比较） | ❌ 不支持 → 返回 None → UNKNOWN | **全部缺失** |
| `threshold` | ✅ 支持（gt/gte/lt/lte/eq/neq + 7 种聚合） | ⚠️ 部分支持（gt/gte/lt/lte/eq，无聚合） | 缺聚合函数 |
| `delta` | ✅ 支持（首末差值 + minimum_samples） | ❌ 不支持 → 返回 None → UNKNOWN | **全部缺失** |
| `trend` | ✅ 支持（increasing/decreasing/stable） | ❌ 不支持 → 返回 None → UNKNOWN | **全部缺失** |
| `exists` | ✅ 支持（非空判定） | ❌ 不支持 → 返回 None → UNKNOWN | **全部缺失** |
