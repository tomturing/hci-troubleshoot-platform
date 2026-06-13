# SOP 变量设计分析与数据流诊断

> **案例**：工单 Q2026061363967（磁盘 SSD 寿命告警排障）  
> **SOP 文档**：`sop_document.id = 2`（磁盘寿命到期）  
> **分析时间**：2026-06-13  
> **分析方法**：第一性原理 + 实际数据库数据验证 + 代码路径追踪

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

### 2.2 三路合并（`merge_variable_schema`）的保护逻辑——为什么旧值被保留

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

**结论：三路合并的"保护人工编辑"逻辑，在这里反向产生了副作用——它把数据库里的旧 `env_injection` 当成"人工编辑的权威值"保护了起来，让 Markdown 里的更新永远无法生效。**

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

### 3.2 `node_ip`：语义正确，提取字段错误

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
SOP 中 `node_ip` 的含义是"告警硬盘所在主机"，应取 `target`。

### 3.3 `smart_info`：应为 `tool_call`，不能用 `llm_inference`

SMART 信息是执行 `smartctl -a /dev/sdX` 命令的实时输出，**LLM 无法推断**。若用 `llm_inference`：
- LLM 要么编造数据（幻觉）
- 要么通过对话向用户询问，体验极差

正确策略：`tool_call`，关联一个执行 `smartctl` 的工具。

### 3.4 `is_sys_disk`：应为 `skill_call`

判断是否系统盘需要执行 `lsblk | grep boot` 或 `df /boot` 等命令，且依赖 `disk_dev` 变量已就绪。当前 `llm_inference` 无数据来源支撑，属于策略降级。

SOP 正文中其实已经给出了判断规则：若 `alert_type` 包含 `vs`，则 `is_sys_disk` 为 false。这个规则完全可以封装为 `skill_call`，完全确定性、零 LLM 调用。

### 3.5 整体缺失：变量依赖图（DAG）

当前 `variable_schema` 是扁平列表，没有 `depends_on` 字段。正确的执行拓扑应为：

```
env_injection（并行）: hci_version, node_ip
    ↓
tool_call: asan_disks（acli_storage_disk_list）
    ↓
llm_inference: disk_sn（从 asan_disks 中提取匹配告警槽位的 SN）
    ↓
llm_inference: disk_dev（从 asan_disks 中找 disk_sn 对应的 dev）
    ↓
并行执行：
  ├── tool_call: smart_info（smartctl）
  └── skill_call: is_sys_disk（alert_type 规则 or lsblk）
    ↓
skill_call: check_meth（disk_vendor_lifetime）
```

缺少 DAG 声明导致引擎无法保证执行顺序，可能在 `asan_disks` 未就绪时就请求 `disk_sn`。

---

## 四、改进方案

### 4.1 紧急修复（不改代码，改 Markdown + 强制刷新 schema）

因为 `merge_variable_schema` 的保护逻辑会保留旧值，单纯更新 Markdown 无效。  
正确做法是通过 **`PATCH /api/admin/sop/{id}/variables`** 接口直接更新 `variable_schema` 字段：

```json
[
  { "name": "disk_sn",    "acquisition_strategy": "llm_inference", "acquisition_tool": null },
  { "name": "node_ip",    "acquisition_strategy": "env_injection",  "acquisition_tool": null },
  { "name": "smart_info", "acquisition_strategy": "llm_inference",  "acquisition_tool": null }
]
```

> ⚠️ 注意：这会绕过三路合并，直接覆盖指定字段。适合纠正历史错误数据。

### 4.2 `node_ip` 提取字段修复（代码修改）

**文件**：`backend/conversation-service/app/routes/sop_execution.py:447-453`

```python
# 修复前（host 优先，语义错误）
val = (
    lookup_dict(alert, "node_ip")
    or lookup_dict(alert, "host")    # 监控节点，不是故障节点
    or lookup_dict(alert, "ip")
    or lookup_dict(alert, "target")
)

# 修复后（target 优先，对磁盘类告警语义正确）
if var_def.get("type") == "ip" or var_name in ("node_ip", "host_ip"):
    val = (
        lookup_dict(alert, "node_ip")
        or lookup_dict(alert, "target")  # 磁盘类告警：故障目标节点
        or lookup_dict(alert, "host")    # fallback
        or lookup_dict(alert, "ip")
    )
```

> 注意：需要区分告警类型，部分告警（如内存告警）`host` 和 `target` 是同一台机器，此修复安全。

### 4.3 `merge_variable_schema` 修复——让 Markdown 更新生效

**文件**：`backend/kb-service/app/services/sop_parser.py:1239-1245`

当前问题：只要 old_schema 中有非 `user_input` 的策略，就永远覆盖新值，即使用户明确在 Markdown 中修改了来源字段。

```python
# 修复：仅当新解析出的策略与旧策略相同（说明用户没改），才保留旧值
# 如果新值与旧值不同，说明用户在 Markdown 中做了修改，应以新值为准
strategy_overridden = (
    old_var.get("acquisition_strategy")
    and old_var["acquisition_strategy"] not in ("", "user_input", None)
    and new_var.get("acquisition_strategy") in ("user_input", None, "")  # 新值是兜底/未识别
)
```

### 4.4 `variable_schema` 增加 `depends_on` 字段（中期改进）

在 schema 数据结构中增加 `depends_on: list[str]`，在引擎层实现拓扑排序：

```python
# engine.py: sop_request_variable 中增加依赖检查
depends_on = var_def.get("depends_on", [])
missing_deps = [d for d in depends_on if d not in context_variables]
if missing_deps:
    return {
        "error": f"变量 {variable_name} 的前置依赖 {missing_deps} 尚未就绪，请先获取这些变量"
    }
```

---

## 五、问题汇总与优先级

| # | 问题 | 类型 | 影响 | 优先级 |
|---|---|---|---|---|
| 1 | `disk_sn` 策略为 `env_injection` 但告警不含 SN | **设计错误** | 排障在第一步直接被阻断 | P0 |
| 2 | Markdown 更新不能覆盖 `variable_schema`（三路合并副作用） | **系统行为** | 用户改了 Markdown 但 Agent 行为不变，产生信任危机 | P0 |
| 3 | `node_ip` 取了监控节点而非故障节点 | **语义错误** | SSH 执行命令打到错误节点 | P1 |
| 4 | `smart_info` 用 `llm_inference` 无法获取真实数据 | **策略错误** | SMART 检测结论不可信 | P1 |
| 5 | `is_sys_disk` 判断规则已在 SOP 正文中明确，但未封装为 skill | **优化项** | 每次都需要 LLM 理解规则，存在幻觉风险 | P2 |
| 6 | 变量间缺乏 `depends_on` 依赖声明 | **架构缺失** | 引擎无法保证执行顺序 | P2 |

---

## 六、附：`_parse_acquisition_source` 源码解析规则

`sop_parser.py:970-983` 中，Markdown 来源字段到 `acquisition_strategy` 的映射规则：

| Markdown 来源字段 | 解析结果 strategy | 解析结果 tool |
|---|---|---|
| `tool:acli_storage_disk_list` | `tool_call` | `acli_storage_disk_list` |
| `skill:disk_vendor_lifetime` | `skill_call` | `disk_vendor_lifetime` |
| `skill:alert-parsing` | `skill_call` | `alert-parsing` |
| `env:hci_version` | `env_injection` | null（来源字符串存入 tool 字段被丢弃） |
| `llm_inference` | `llm_inference` | null |
| `user_confirm` | `user_confirm` | null |
| `user_input` | `user_input` | null |

**注意**：`env:xxx` 格式只映射到 `env_injection` 策略，`xxx` 部分（实际 env key 名）被丢弃，**不写入 `acquisition_tool`**。这导致系统在初始化时无法知道应该读取哪个环境变量 key，只能用变量名猜测（`lookup_dict` 模糊匹配），这是另一个潜在的不可靠点。
