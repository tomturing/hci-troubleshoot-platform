# SOP 变量设计分析与数据流诊断

> **案例**：工单 Q2026061363967（磁盘 SSD 寿命告警排障）
> **SOP 文档**：`sop_document.id = 2`（磁盘寿命到期）
> **分析时间**：2026-06-13
> **分析方法**：第一性原理 + 实际数据库数据验证 + 代码路径追踪

> **PR1 更新**：平台内置/硬编码治理已完成第一批实现。动态 `skill_call` 已接入 `skill_definition`，Python 内置业务 Skill 已移除；`env_injection` 已收敛为确定性 `env_info` 字段直取；`depends_on`、`output_path`、`fallback_strategy` 已可解析、合并并参与变量池运行时决策。本文件保留事故现场数据，用于说明问题来源，同时标注 PR1 后的当前状态和 PR3 剩余优化。

---

## 一、现场真实数据

### 1.1 数据库 `content_md` 中的变量声明（Markdown 原文）

这是当前数据库存储的 `sop_document.content_md` 文档开头的**变量声明表格**：

```markdown
## 变量声明

| 变量名       | 类型    | 来源                        | 说明                                           |
| ------------- | ------- | --------------------------- | ---------------------------------------------- |
| hci_version   | string  | env:hci_version             | 超融合版本信息                                 |
| node_ip       | ip      | skill:alert-parsing         | 告警硬盘所在主机                               |
| is_sys_disk   | boolean | llm_inference               | 是否是系统盘                                   |
| asan_disks    | json    | tool:acli_storage_disk_list | aSAN硬盘信息                                   |
| disk_sn       | string  | llm_inference               | 告警硬盘的标识                                 |
| disk_dev      | string  | llm_inference               | asan_disks中匹配告警硬盘disk_sn的标识记录中的dev的值 |
| smart_info    | string  | llm_inference               | 执行硬盘SMART原始回显信息                      |
| check_meth    | string  | skill:disk_vendor_lifetime  | 磁盘厂商寿命检测结果（正常 / 返修）            |
```

### 1.2 数据库 `variable_schema` 中的实际策略（运行时依据）

这是 `sop_document.variable_schema` 字段中存储的 JSON，**这才是 Agent 运行时真正读取的数据**：

| 变量名 | 类型 | acquisition_strategy | acquisition_tool |
|---|---|---|---|
| hci_version | string | `env_injection` | null |
| node_ip | ip | `env_injection` | null |
| **disk_sn** | string | **`env_injection`** | null |
| is_sys_disk | boolean | `llm_inference` | null |
| asan_disks | json | `tool_call` | acli_storage_disk_list |
| disk_dev | string | `llm_inference` | null |
| smart_info | string | `llm_inference` | null |
| check_meth | string | `skill_call` | disk_vendor_lifetime |
| host_ip（deprecated） | ip | `env_injection` | null |

### 1.3 告警原始数据（`environment` 表）

```json
{
  "id": 135,
  "host": "SVR_aCloud_669",
  "type": "磁盘状态异常",
  "alert_type": "vs_disk_warn",
  "target": "SVR_aCloud_670",
  "description": "主机（SVR_aCloud_670）SSD寿命告警（1号盘），告警盘槽位（1），剩余寿命3%！",
  "object_id": "70e284243e19_355cd2e4150acc9b6",
  "urgent_type": "紧急"
}
```

### 1.4 SOP 执行实例注入结果（`sop_execution.context_variables`）

```json
{
  "node_ip":     { "value": "SVR_aCloud_669", "source": "env_context" },
  "hci_version": { "value": "6.11.1_R1",     "source": "env_context" }
}
```

`disk_sn` **未被注入**，执行实例状态为 `interrupted`。

PR1 后新建执行实例的语义变化：

- `env_injection` 不再从 `alert_logs/task_logs` 猜测 `node_ip/disk_sn/request_id`。
- `alert_logs/task_logs/env_info` 只在 `variable_schema` 显式声明变量名或 `depends_on` 引用时进入变量池，`source=environment_context`。
- 若 `node_ip` 声明为 `skill_call + depends_on=["alert_logs"]`，变量池会把原始 `alert_logs` 解包传给动态 Skill，而不是在 conversation-service 里做业务解析。

---

## 二、问题 a：为什么更新了 Markdown，`variable_schema` 还是旧的？

这是本次分析最重要的发现。**Markdown 内容（`content_md`）和变量运行时数据（`variable_schema`）是两个独立字段，并不自动同步。**

### 2.1 完整数据链路

```
你在 SOP 管理页面编辑 Markdown
         ↓
前端调用 PUT /api/admin/sop/{id}（传入新的 content_md）
         ↓
kb-service admin.py 接收请求
         ↓ 执行步骤：
  1. parse_sop_markdown(new_content_md)  → 解析决策树结构
  2. extract_sop_variables(new_content_md, tree) → 从新 Markdown 中提取变量定义（new_schema）
  3. 读取旧 variable_schema（old_schema）
  4. merge_variable_schema(old_schema, new_schema) ← 【关键：三路合并】
         ↓
  5. 将合并结果写入 sop_document.variable_schema
```

### 2.2 三路合并（`merge_variable_schema`）的旧问题与 PR1 修复

核心逻辑在 `sop_parser.py:1211-1268`：

```python
strategy_overridden = (
    "acquisition_strategy" in old_var
    and old_var["acquisition_strategy"] is not None
    and old_var["acquisition_strategy"] != ""
    and (
        old_var["acquisition_strategy"] != "user_input"   # 旧策略不是兜底值
        or new_var.get("acquisition_strategy") == "user_input"
        or new_var.get("auto_generated", False)           # 新值是自动推断的
    )
)
# 如果满足以上条件，就用旧的 acquisition_strategy 覆盖新解析出来的
if strategy_overridden:
    merged_var["acquisition_strategy"] = old_var.get("acquisition_strategy")
```

**触发场景还原（disk_sn 变量）：**

| 时序 | 事件 |
|---|---|
| 初次导入 | Markdown 中 `disk_sn` 来源为 `llm_inference` → 解析为 `llm_inference` → 写入 old_schema |
| 某次更新后 | 旧版 Markdown 中 `disk_sn` 来源被某次修改改为了 `env:xxx` 格式 → 解析为 `env_injection` → 写入 old_schema |
| 你再次编辑 Markdown | 将 `disk_sn` 来源改回 `llm_inference` → 解析出 new_schema 中 `disk_sn.acquisition_strategy = llm_inference` |
| **合并时** | old_schema 中 `disk_sn.acquisition_strategy = env_injection`（不是 `user_input`，不为空）→ `strategy_overridden = True` → **旧的 `env_injection` 覆盖了你刚改的 `llm_inference`** |

**旧结论：三路合并的“保护人工编辑”逻辑，在这里反向产生了副作用——它把数据库里的旧 `env_injection` 当成“人工编辑的权威值”保护了起来，让 Markdown 里的更新无法生效。**

PR1 已修复该类问题：

- `merge_variable_schema` 支持 `depends_on`、`output_path`、`fallback_strategy` 三个字段。
- Markdown 明确声明且新值非空时，以 Markdown 新声明为准。
- 旧 schema 中人工维护的空值（例如 `acquisition_tool=None`）仍能被保留，避免自动推断工具名反向覆盖管理员配置。
- 历史缺少 `auto_generated` 标记的新 schema 按自动推断处理，防止重新导入时误覆盖人工字段。

这解决的是“用户在 SOP 管理页面改了变量声明，但 Agent 仍按旧变量声明执行”的根因之一。历史数据库里已经错落的 `variable_schema` 仍需要重新发布 SOP 或通过变量管理接口修正一次。

### 2.3 当前真实的 `content_md` vs `variable_schema` 差异对比

| 变量 | Markdown 来源（用户期望） | variable_schema 实际策略（Agent 执行依据） | 是否一致 |
|---|---|---|---|
| hci_version | `env:hci_version` | `env_injection` | ✅ |
| node_ip | `skill:alert-parsing` | `env_injection` | ❌ **不一致** |
| disk_sn | `llm_inference` | `env_injection` | ❌ **不一致** |
| is_sys_disk | `llm_inference` | `llm_inference` | ✅ |
| asan_disks | `tool:acli_storage_disk_list` | `tool_call` | ✅ |
| disk_dev | `llm_inference` | `llm_inference` | ✅ |
| smart_info | `llm_inference` | `llm_inference` | ✅ |
| check_meth | `skill:disk_vendor_lifetime` | `skill_call` | ✅ |

**`node_ip` 和 `disk_sn` 的 `variable_schema` 策略与 Markdown 声明存在不一致，这就是为什么你更新了 Markdown 但 Agent 行为没有改变。**

---

## 三、第一性原理分析：各变量策略的正确性

### 3.1 `disk_sn`：策略选型的根本性错误

#### 现象

当前 `variable_schema` 中策略为 `env_injection`，但 `context_variables` 中没有该变量值。

#### 根因

HCI 告警系统的架构分层：

```
告警层（事件流）──── 含：主机名、槽位号、告警类型、剩余寿命%
                                ↑ 仅此，不含 SN
存储实体层 ─────── 含：磁盘SN、磁盘型号、设备路径(dev)
```

告警 `description` = `"主机（SVR_aCloud_670）SSD寿命告警（1号盘），告警盘槽位（1），剩余寿命3%"` 中**根本没有 SN 信息**。

代码中 `_resolve_env_variable` 对 `disk_sn` 的提取逻辑（`sop_execution.py:430-445`）会：
1. 查 alert 字段中有无 `sn`/`serial_number` → 无
2. 用正则在 description 中找 `SN: xxx` → 无
3. 最后用通配正则 `\b([A-Z0-9]{8,20})\b` 匹配 → **会命中 `SVR_aCloud_670`、`host-70e284243d2d` 等无关字符串，产生错误数据**

因此，`disk_sn` 的 `env_injection` 策略是**结构性错误**，不论如何优化解析逻辑，信息源本身就不包含 SN。

#### 正确策略

`disk_sn` 必须从存储层（`acli_storage_disk_list` 返回的 `asan_disks`）中提取，正确策略为 `llm_inference`（前提：`asan_disks` 已就绪），或更可靠的 `skill_call`。

### 3.2 `node_ip`：不是简单字段映射，而是告警锚定问题

`variable_schema` 中策略为 `env_injection`，成功注入了值 `SVR_aCloud_669`。

但告警数据：

```json
{ "host": "SVR_aCloud_669",   ← 发起告警的监控节点
  "target": "SVR_aCloud_670"  ← 实际发生故障的节点（告警盘所在主机）
}
```

`_resolve_env_variable` 中 `node_ip` 的提取优先级（`sop_execution.py:447-453`）：

```python
val = (
    lookup_dict(alert, "node_ip")
    or lookup_dict(alert, "host")    # ← 命中 SVR_aCloud_669（监控节点）
    or lookup_dict(alert, "ip")
    or lookup_dict(alert, "target")  # ← 实际是 SVR_aCloud_670（故障节点）
)
```

**`host` 的优先级高于 `target`，导致注入了错误的主机名。**

但更深层的问题不是简单把优先级改成 `target > host`。`env_injection` 的本质是字段映射器，它可以读取某个字段，却无法在多条告警里判断哪一条才是当前 SOP 应锚定的告警。磁盘寿命告警场景中，`node_ip` 的正确值来自“先识别正确告警，再取该告警的目标节点”，这是一个通用告警解析任务，不是单变量字段提取任务。

因此，长期正确策略应是：

```json
{
  "name": "node_ip",
  "acquisition_strategy": "skill_call",
  "acquisition_tool": "hci-alert-parsing",
  "output_path": "node_ip",
  "depends_on": ["alert_logs"]
}
```

旧实现中 `skill:alert-parsing` 没有生效，不是因为这个方向错误，而是因为平台的动态 Skill 运行时没有打通：SOP 变量池的 `skill_call` 调用 Python 内置注册表，没有查询数据库 `skill_definition` 表中的 `hci-alert-parsing`。

PR1 已完成运行时打通：

- `sop_request_variable` 的 `skill_call` 通过 `DynamicSkillRunner` 执行数据库 active Skill。
- Skill 名称支持历史 snake_case、kebab-case 和 `hci-` 前缀候选，例如 `alert-parsing` 可匹配 `hci-alert-parsing`。
- `output_path` 可从 Skill JSON 输出中提取变量值。
- `depends_on=["alert_logs"]` 会先检查环境事实源是否已进入变量池，缺失时 fail-loud 并提示先获取依赖。

仍需 PR2/PR3 完成的是发布时引用校验和具体 `hci-alert-parsing` Skill 的业务指令/输出契约验收。

### 3.3 `smart_info`：应来自真实命令输出，不能用 `llm_inference`

SMART 信息是执行 `smartctl -a /dev/sdX` 命令的实时输出，**LLM 无法推断**。若用 `llm_inference`：
- LLM 要么编造数据（幻觉）
- 要么通过对话向用户询问，体验极差

正确策略：通过工具执行真实命令并把 stdout 绑定回变量。这里要区分两类方案：

| 方案 | 适用性 | 判断 |
|---|---|---|
| 通用 `bash_exec(container=vs-cp-manager, command="smartctl -a /dev/{disk_dev}")` | 系统级命令、低频专用命令 | 推荐，符合通用执行器设计 |
| 专用 `acli_system_smartctl` schema | 高频复用、参数/输出有稳定结构、需要专门 UI 或解析器 | 当前证据不足，不建议为普通命令单独扩张工具 |

现场验证显示命令需要指定容器边界：

```bash
acli --container vs-cp-manager system smartctl -a /dev/{disk_dev}
```

如果短期保留专用工具定义，模板必须包含 `--container vs-cp-manager`；否则工具定义本身就是错误的。长期应增强变量声明的工具参数模板能力，例如 `tool:bash_exec` + `args_template`，避免为每个系统命令新增一个 schema。

### 3.4 `is_sys_disk`：不应写成 Python 内置技能，也不应长期依赖自由 LLM

判断是否系统盘需要执行 `lsblk | grep boot` 或 `df /boot` 等命令，且依赖 `disk_dev` 变量已就绪。当前 `llm_inference` 无数据来源支撑，属于策略降级。

SOP 正文中其实已经给出了判断规则：若 `alert_type` 包含 `vs`，则 `is_sys_disk` 为 false。从确定性角度看，这个规则可以封装为规则表达式、派生变量，或由数据库动态 Skill 处理；但不应新增 Python 内置 `is_sys_disk` 函数，否则会把特定 SOP 的业务规则写死到 agent-service 微内核。

短期止血可以保留：

```json
{
  "name": "is_sys_disk",
  "acquisition_strategy": "llm_inference",
  "acquisition_tool": null,
  "depends_on": ["alert_type"]
}
```

这个方案只能解决“无前置事实时让 LLM 瞎猜”的问题，不是最终方案。长期应支持：

- `derived`：规则表达式，例如 `contains(alert_type, "vs") ? false : unknown`
- `skill_call`：调用数据库动态 Skill，不进入 Python 内置注册表
- `tool_call`：在规则无法确定时执行只读命令验证

### 3.5 整体缺失：变量依赖图（DAG）

旧 `variable_schema` 是扁平列表，没有 `depends_on` 字段。PR1 已支持字段解析、合并和运行时前置依赖拦截，但还没有实现自动拓扑调度。正确的目标拓扑仍应为：

```
env_injection（并行）: hci_version, alert_logs
    ↓
skill_call: node_ip / alert_type（hci-alert-parsing，从多条告警中锚定正确告警）
    ↓
tool_call: asan_disks（acli_storage_disk_list）
    ↓
llm_inference: disk_sn（从 asan_disks 中提取匹配告警槽位的 SN）
    ↓
llm_inference: disk_dev（从 asan_disks 中找 disk_sn 对应的 dev）
    ↓
并行执行：
  ├── tool_call: smart_info（通用 bash_exec/acli_exec 执行 smartctl）
  └── derived/skill_call/tool_call: is_sys_disk（依赖 alert_type、disk_dev）
    ↓
skill_call: check_meth（disk_vendor_lifetime）
```

缺少 DAG 声明会导致引擎无法保证执行顺序，可能在 `asan_disks` 未就绪时就请求 `disk_sn`。PR1 增加的是前置依赖拦截，而不是自动拓扑调度；也就是说，引擎会阻止乱序获取，但仍需要 ReAct 下一轮按提示先获取依赖变量。自动拓扑调度应放入 PR2/PR3。

---

## 四、改进方案

### 4.1 短期止血与长期修复边界

PR1 后，重新发布 SOP 时 Markdown 明确声明的新策略可以同步到 `variable_schema`；对于已经错落在数据库中的历史执行数据，仍可以通过 **`PATCH /api/admin/sop/{id}/variables`** 接口直接修正一次。

建议 PR3 在 PR1/PR2 基础上把磁盘寿命 SOP 的目标变量声明收敛为：

```json
[
  { "name": "hci_version", "acquisition_strategy": "env_injection", "acquisition_tool": "env:hci_version", "depends_on": [] },
  { "name": "alert_logs",  "acquisition_strategy": "env_injection", "acquisition_tool": "env:alert_logs",  "depends_on": [] },
  { "name": "alert_type",  "acquisition_strategy": "skill_call", "acquisition_tool": "hci-alert-parsing", "output_path": "alert_type", "depends_on": ["alert_logs"] },
  { "name": "node_ip",     "acquisition_strategy": "skill_call", "acquisition_tool": "hci-alert-parsing", "output_path": "node_ip", "depends_on": ["alert_logs"] },
  { "name": "asan_disks",  "acquisition_strategy": "tool_call",     "acquisition_tool": "acli_storage_disk_list", "depends_on": [] },
  { "name": "disk_sn",     "acquisition_strategy": "llm_inference", "acquisition_tool": null, "depends_on": ["asan_disks"] },
  { "name": "disk_dev",    "acquisition_strategy": "llm_inference", "acquisition_tool": null, "depends_on": ["disk_sn", "asan_disks"] },
  { "name": "smart_info",  "acquisition_strategy": "tool_call", "acquisition_tool": "bash_exec", "acquisition_args_template": {"container": "vs-cp-manager", "command": "smartctl -a /dev/{disk_dev}", "node_ip": "{node_ip}"}, "depends_on": ["disk_dev", "node_ip"] },
  { "name": "is_sys_disk", "acquisition_strategy": "derived", "expression": "contains(alert_type, 'vs') ? false : unknown", "depends_on": ["alert_type"] },
  { "name": "check_meth",  "acquisition_strategy": "skill_call",    "acquisition_tool": "disk_vendor_lifetime", "depends_on": ["smart_info"] }
]
```

> 注意：PR1 已实现动态 Skill、`output_path` 和 `depends_on` 的基础运行链路；`acquisition_args_template`、`derived`、发布期引用校验和自动拓扑调度仍属 PR2/PR3 范围。当前分支不应提交 `revert_*.sql` 类临时脚本。

### 4.2 `node_ip` 修复不应停留在 `target > host`

**文件**：`backend/conversation-service/app/routes/sop_execution.py:447-453`

```python
# 修复前（host 优先，语义错误）
val = (
    lookup_dict(alert, "node_ip")
    or lookup_dict(alert, "host")    # 监控节点，不是故障节点
    or lookup_dict(alert, "ip")
    or lookup_dict(alert, "target")
)

目标不是继续扩写 `_resolve_env_variable` 的 if/else，而是把“从多条告警中识别正确告警”移到动态 Skill：

skill:hci-alert-parsing(alert_logs, sop_title, category, user_query)
  -> { node_ip, alert_type, matched_alert_id, confidence, evidence }
```

`env_injection` 只保留确定性字段直取能力，不再承载复杂业务解析。

### 4.3 `merge_variable_schema` 修复——让 Markdown 更新生效

**文件**：`backend/kb-service/app/services/sop_parser.py:1239-1245`

PR1 修复后的原则：

- Markdown 明确声明的新值非空时，新值优先。
- 新值来自自动推断或未声明时，保留旧人工字段。
- `None` 也可以是人工字段，例如 `acquisition_tool=None`，不能被自动推断工具名覆盖。

### 4.4 `variable_schema` 增加 `depends_on` 字段（PR1 已实现基础能力）

PR1 已在 schema 数据结构中增加 `depends_on: list[str]`，并在引擎层实现前置依赖拦截：

```python
# engine.py: sop_request_variable 中增加依赖检查
depends_on = var_def.get("depends_on", [])
missing_deps = [d for d in depends_on if d not in context_variables]
if missing_deps:
    return {
        "error": f"变量 {variable_name} 的前置依赖 {missing_deps} 尚未就绪，请先获取这些变量"
    }
```

PR2/PR3 需要继续补齐自动拓扑执行和循环依赖发布校验。

---

## 五、问题汇总与优先级

| # | 问题 | 类型 | 影响 | 优先级 |
|---|---|---|---|---|
| 1 | `disk_sn` 策略为 `env_injection` 但告警不含 SN | **设计错误** | 排障在第一步直接被阻断 | P0 |
| 2 | Markdown 更新不能覆盖 `variable_schema`（三路合并副作用） | **系统行为** | 用户改了 Markdown 但 Agent 行为不变，产生信任危机 | P0 |
| 3 | `node_ip` 用 `env_injection` 做复杂告警锚定 | **架构错误** | 多告警场景可能 SSH 到错误节点 | P0 |
| 4 | `smart_info` 用 `llm_inference` 无法获取真实数据 | **策略错误** | SMART 检测结论不可信 | P1 |
| 5 | `is_sys_disk` 需要派生变量/动态 Skill，不应成为 Python 内置函数 | **硬编码风险** | 特定 SOP 业务规则污染平台微内核 | P1 |
| 6 | 变量间缺乏自动拓扑调度 | **架构缺失** | PR1 已能拦截乱序，但仍需 ReAct 下一轮手动按依赖推进 | P2 |

PR1 已完成：#2 的合并逻辑修复，#3 的 env 复杂解析移除和动态 Skill 通道打通，#5 的 Python 内置业务 Skill 移除，#6 的基础依赖拦截。#1/#4/#5 的具体 SOP 变量最终形态需在 PR3 完成。

---

## 六、附：`_parse_acquisition_source` 源码解析规则

`sop_parser.py:970-983` 中，Markdown 来源字段到 `acquisition_strategy` 的映射规则：

| Markdown 来源字段 | 解析结果 strategy | 解析结果 tool |
|---|---|---|
| `tool:acli_storage_disk_list` | `tool_call` | `acli_storage_disk_list` |
| `skill:disk_vendor_lifetime` | `skill_call` | `disk_vendor_lifetime` |
| `skill:alert-parsing` | `skill_call` | `alert-parsing`（PR1 运行时会生成候选名并可匹配 `hci-alert-parsing`） |
| `env:hci_version` | `env_injection` | `env:hci_version` |
| `llm_inference` | `llm_inference` | null |
| `user_confirm` | `user_confirm` | null |
| `user_input` | `user_input` | null |

**PR1 后状态**：`skill:` 方向已经接入数据库 `skill_definition`，Python 内置注册表不再作为生产变量池的业务 Skill 执行源。

---

## 七、PR 拆分建议

| PR | 目标 | 范围 |
|---|---|---|
| PR-1 | 内置/硬编码治理 | 已完成动态 Skill 基础运行、工具 registry TTL 热刷新、env 复杂解析移除、变量 `depends_on/output_path/fallback_strategy` 基础能力、错误回滚脚本不带入 |
| PR-2 | 五大动态资源统一运行时 | KBD、SOP、工具、技能、Prompt 统一走资源发布、校验、热加载、版本审计和执行接口 |
| PR-3 | SOP 变量方案重审与优化 | 在 PR1/PR2 基础上重新确认 `node_ip`、`smart_info`、`is_sys_disk`、`disk_sn/disk_dev` 的最终变量声明、动态 Skill 和工具输出绑定 |
