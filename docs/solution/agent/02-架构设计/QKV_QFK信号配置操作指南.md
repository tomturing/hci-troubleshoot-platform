# QKV/QFK 信号配置操作指南

> 版本：v2.0
> 日期：2026-07-24
> 适用对象：运营人员、SRE 工程师
> 信号模型：v2 嵌套文档（`schema_version: 2`，契约见 `backend/shared/schemas/signals/signal.v2.schema.json`）
> 关联文档：[QKV_QFK信号模型v2参考.md](./QKV_QFK信号模型v2参考.md)（逐字段对齐的权威参考）、[QKV_QFK扩展性与配置易用性评估.md](./QKV_QFK扩展性与配置易用性评估.md)

---

## 一、概述

QKV（前端信号）和 QFK（后端信号）是关键信号架构的核心组件，用于在 KBD 差分引擎中提取和判定关键信息。本文档说明如何在 admin-ui 中配置 QKV/QFK 的参数（即编写 KBD 条目的 `signals_json`）。

> **v2 提示**：`signals_json` 采用嵌套文档模型，每个信号由 `acquire`（采集）/ `match`（判定）/ `orchestrate`（编排）/ `provenance`（来源）/ `review`（复核）五段组成，**仅 `acquire` 必填**。完整的逐字段说明与示例见关联参考文档。

### 1.1 什么是 QKV/QFK

| 类型 | 全称 | 作用 | 调用方 |
|------|------|------|--------|
| QKV | Query Key Value | 前端信号（生产者） | KBD 差分引擎 |
| QFK | Query For Key | 后端信号（消费者） | KBD 差分引擎 |

### 1.2 配置入口

在 admin-ui 中，QKV/QFK 已注册为工具定义，可通过 **工具管理** 页面配置：

1. 登录 admin-ui
2. 进入 **系统管理** → **工具管理**
3. 在分类下拉框选择 `qkv` 或 `qfk`
4. 选择需要配置的工具，编辑其 `signals_json`（v2 文档）

---

## 二、QKV 关键词清洗规则

### 2.1 自动清洗规则

为规范化 `-k` 参数值，系统会自动清洗 LLM 抽取的关键词中的类型后缀：

| 信号类型 | 清洗后缀 | 示例 |
|----------|----------|------|
| `qkv_alert` | "告警" | `"虚拟机软关机告警"` → `"虚拟机软关机"` |
| `qkv_task` | "失败"、"成功"、"运行中" | `"启动虚拟机失败"` → `"启动虚拟机"` |
| `qkv_dialog` | "弹框" | `"用户确认弹框"` → `"用户确认"` |

> 注：v2 中上述关键词对应 `acquire.args.keyword`（acli `<task|dialog|alert>` get -k 的检索词）。

### 2.2 状态自动检测（仅 qkv_task）

当关键词包含"失败"时，系统会自动：
1. 清洗掉"失败"字样
2. 添加 `-s failed` 参数

**示例**：
```
LLM 抽取: keyword="启动虚拟机失败"
清洗后:   keyword="启动虚拟机", is_failed=True
命令:     acli --formatter json task get -k '启动虚拟机' -s failed -l 100
```

### 2.3 关键字须对齐分类基线（语义约束）

QKV 的 `acquire.args.keyword` 是 `acli <task|dialog|alert> get -k` 的**检索词**，不是自由描述。它必须能命中平台真实记录命名，即与本案例所属**分类基线**标签语义对齐，否则查不到记录、信号恒为假（静默漏诊）。

- **任务失败型**（分类基线标签多以 `失败/卡住/异常/不达预期` 结尾，如 `虚拟机开机失败`）→ 走 `qkv_task` / `qkv_dialog`
- **告警型**（标签以 `告警` 结尾，如 `虚拟机CPU或内存占用过高告警`）→ 走 `qkv_alert`

> 生产者类型 ↔ 故障性质强绑定：选错类型（如给"开机失败"用 `qkv_alert`）会查错子系统、返回空，信号恒假——这是准确性第一杀手。

### 2.4 校验与容忍原则（不硬拒）

- **输入容忍**：KBD 是人写的，内容偏口语化、有同义词甚至错别字（如"镜象忙""磁盘被拨出"）。错别字/同义词由 LLM 抽取阶段兜底，**不应对原始输入做硬性校验或拒抽**。
- **实时基线**：分类基线（198 类）由运营持续调整、非固定词表。任何校验都须读取**运行时实时基线**（`category_repo.get_all_active()` / 前端 `useCategories`），**不得**把"硬件部静态词表"硬编码进代码或 prompt。
- **只软校验输出**：前端生产者编辑区已对"类型↔关键字性质强冲突"做**高亮软告警**（不阻止保存）；后端抽取管线的实时基线模糊匹配（建议实现中）只会产出 `warnings` 进人工复核队列，**绝不阻断抽取**。

> 关联设计：[2026-07-23-QKV关键信号关键字分类基线校验设计.md](../../events/2026-07-23-QKV关键信号关键字分类基线校验设计.md)
> 关联任务：[2026-07-23-QKV关键信号关键字分类基线校验任务.md](../../../task/events/2026-07-23-QKV关键信号关键字分类基线校验任务.md)

---

## 三、QKV 产出变量配置（orchestrate.produces）

> v2 中"产出变量"位于信号的 `orchestrate.produces`，供后续信号通过 `{{变量名}}` 引用。

### 3.1 produces 字段说明

`produces` 定义要从查询结果中提取的变量，供后续信号引用。

| 参数 | 说明 | 示例 |
|------|------|------|
| `name` | 输出变量名（建议大写下划线格式） | `HOST`、`VM_ID` |
| `path` | JSON 字段路径（支持 `\|` 多路径容错） | `host\|hostname\|hostid` |

### 3.2 各信号类型的默认产出变量

#### qkv_alert

| 变量 | 说明 | 格式 |
|------|------|------|
| `alert_type` | 告警类型 | 字符串 |
| `end` | 告警时间 | `"YYYY-MM-DD HH:MM:SS"` |
| `target` | 目标对象 | 字符串 |
| `type` | 类型 | 字符串 |
| `description` | 描述 | 字符串 |
| `host` | 主机名 | 字符串 |
| `vm` | 虚拟机 ID | 字符串 |

#### qkv_task

| 变量 | 说明 | 格式 |
|------|------|------|
| `type` | 任务类型 | 字符串 |
| `end` | 任务结束时间 | `"YYYY-MM-DD HH:MM:SS"` |
| `target` | 目标对象 | 字符串 |
| `description` | 描述 | 字符串 |
| `host` | 主机名 | 字符串 |
| `vm` | 虚拟机 ID | 字符串 |
| `errcode_tracing` | 错误码追踪 | 字符串 |
| `request_id` | 请求 ID | 字符串 |
| `status` | 状态 | 字符串 |

#### qkv_dialog

| 变量 | 说明 | 格式 |
|------|------|------|
| `request_id` | 请求 ID（从日志提取） | 32位十六进制字符串 |
| `end` | 日志时间 | `"YYYY-MM-DD HH:MM:SS"` |
| `line` | 日志行内容 | 字符串 |

### 3.3 时间格式说明

所有 `end` 字段统一转换为人类可读格式：

| 原始格式 | 转换后格式 |
|----------|------------|
| `"2026-07-20 18:45:22"` | `"2026-07-20 18:45:22"` (保持不变) |
| `"2026/07/20 18:45:22"` | `"2026-07-20 18:45:22"` |
| Unix 时间戳 `1784544322` | `"2026-07-20 18:45:22"` |

### 3.4 可视化编辑步骤

1. 在工具管理页面选择 QKV 类型工具（如 `qkv_task`）
2. 切换到 **可视化编辑** Tab
3. 在 **产出变量 (produces)** 区域：
   - 点击 **添加变量** 新增一行
   - 填写变量名和 JSON 路径
   - 点击删除图标移除不需要的变量
4. 点击 **保存** 提交配置

### 3.5 配置示例

**场景**：从任务查询结果中提取主机信息

```
产出变量列表：
┌────────────┬─────────────────────────┐
│ 变量名     │ JSON Path               │
├────────────┼─────────────────────────┤
│ HOST       │ host|hostname|hostid    │
│ VM_ID      │ vm_id|vmid              │
│ TASK_ID    │ task_id                │
└────────────┴─────────────────────────┘
```

**多路径容错说明**：
- `host|hostname|hostid` 表示依次尝试 `host`、`hostname`、`hostid` 三个字段
- 找到第一个有效值即停止，避免因字段名变化导致提取失败

### 3.6 变量引用方式（v2 写法）

配置完成后，后续信号可通过 `{{变量名}}` 引用（置于对应 `args` 字段，而非 v1 的顶层 `keyword`）：

```json
{
  "acquire": {
    "tool": "qfk_log",
    "args": {
      "host": "{{HOST}}",
      "file": "vtpdaemon.log"
    }
  }
}
```

---

## 四、QFK 后端信号字段规范（acquire + match）

> v2 中后端信号 = `acquire`（采集）+ `match`（判定）。`acquire.tool` 枚举：`qfk_log` / `qfk_service` / `qfk_system` / `qfk_vm` / `qfk_network` / `qfk_storage` / `qfk_hardware` / `qfk_platform`。全部 `args` 字段见《v2 参考》§4.2。

### 4.1 共有字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `instruction` | str | 否 | - | 关键信号说明（acquire.args.instruction） |
| `host` | str | 否 | 变量池 | 主机（变量池获取，特殊值 `cluster` 表示遍历集群） |
| `resource_keyword` | str | 否 | - | **资源/主题选择器**（如服务名、虚拟机名）；非匹配词，见 §6 消歧 |
| `command` | str | 部分必填 | - | acli 子命令（qfk_system/vm/network/storage/hardware/platform 必填；qfk_service 可选） |
| `timeout` | int | 否 | 10 | 超时时间（秒） |
| `match.type` | str | 否 | - | 判定类型：`keyword` / `regex` |
| `match.pattern` | str | 否 | - | 匹配内容（**判定唯一权威**，见 §6） |
| `match.mode` | str | 否 | "or" | 匹配模式：or/and/not |
| `match.expected` | bool | 否 | true | 期望结果：true=应出现；false=应不出现 |

### 4.2 特有字段

#### qfk_log

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `file` | str | **是** | - | 日志文件名 |
| `end` | str | 否 | 变量池 | 结束时间 |

**命令格式**：
```bash
acli --host {{HOST}} --timeout 10 log get -k "<resource_keyword>" -f "file" [-t "end"]
```

#### qfk_system

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `command` | str | **是** | - | 执行命令（lsof/ps auxf/lsblk/iostat/smartctl） |
| `container` | str | 否 | "asv-con" | 容器类型 |

**命令格式**：
```bash
acli --container asv-con --host {{HOST}} --timeout 10 system lsof
```

#### qfk_service

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `resource_keyword` | str | 否 | - | **服务名称**（acli service <container> <name> 的 `<name>`） |
| `container` | str | 否 | "asv" | 容器类型（asv/vn/vn-agent/vs） |
| `command` | str | 否 | "status" | 动作（status/restart） |

**命令格式**：
```bash
acli service <container> <resource_keyword> <command>
```

**示例**：
```bash
acli service asv vtpdaemon status
```

#### qfk_vm / network / storage / hardware / platform

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `command` | str | **是** | - | 执行命令 |

**命令格式**：
```bash
acli --host {{HOST}} --timeout 10 vm <command>
```

### 4.3 host 字段特殊处理

| host 值 | 命令参数 | 说明 |
|---------|----------|------|
| `"cluster"` | `--cluster` | 遍历集群所有主机执行 |
| 其他值 | `--host {{HOST}}` | 指定主机执行 |

### 4.4 配置示例（v2 写法）

#### qfk_log 示例

```json
{
  "acquire": {
    "tool": "qfk_log",
    "args": {
      "instruction": "检查虚拟机镜像相关日志",
      "host": "{{HOST}}",
      "resource_keyword": "vm-disk",
      "timeout": 30,
      "file": "vtpdaemon.log",
      "end": "2026-07-21 10:00:00"
    }
  },
  "match": {
    "type": "keyword",
    "pattern": "qcow2",
    "mode": "or",
    "expected": true
  }
}
```

#### qfk_system 示例

```json
{
  "acquire": {
    "tool": "qfk_system",
    "args": {
      "instruction": "检查虚拟机镜像文件是否被占用",
      "host": "{{HOST}}",
      "resource_keyword": "overlay2/docker",
      "timeout": 30,
      "command": "lsof",
      "container": "asv-con"
    }
  },
  "match": {
    "type": "regex",
    "pattern": "vm-disk|\\.qcow2|ClwDRDBClient",
    "mode": "or",
    "expected": true
  }
}
```

#### qfk_service 示例

```json
{
  "acquire": {
    "tool": "qfk_service",
    "args": {
      "instruction": "检查 vtpdaemon 服务状态",
      "host": "{{HOST}}",
      "resource_keyword": "vtpdaemon",
      "command": "status"
    }
  },
  "match": {
    "type": "keyword",
    "pattern": "running|active",
    "mode": "or",
    "expected": true
  }
}
```

---

## 五、QFK 判定器配置（match）

> 判定逻辑集中在 `match` 段。详细字段与已知限制见《v2 参考》§5。

### 5.1 matcher 类型说明

运行时 `evaluate_matcher` 已实现 6 种判定类型：

| 类型 | 用途 | 关键参数 |
|------|------|---------|
| `keyword` | 关键字匹配 | `pattern`、`mode` |
| `regex` | 正则表达式匹配 | `pattern` |
| `state` | 状态值匹配 | `pattern` |
| `threshold` | 数值阈值比较 | `operator`、`value` |
| `json_path` | JSON 路径取值 | `path`、`expected_value` |
| `exists` | 存在性判定 | 无额外参数 |

> ✅ **已修复（2026-07-24）**：`signal.v2.schema.json` 的 `match` 段已扩宽，`type` 为匹配器名称（keyword/regex/state/threshold/json_path/exists），`additionalProperties: false` 同时允许 `value`/`operator`/`path`/`expected_value`。`state/threshold/json_path/exists` 所需的额外字段现在保存校验可正常通过，**6 类判定均可直接使用**。多关键词 OR 匹配优先用 `regex` + `|` 或 `keyword` + 列表写法。

### 5.2 匹配模式说明

| 模式 | 说明 | 判定逻辑 |
|------|------|----------|
| `or` | 任一匹配 | 任一关键字命中即判定为真 |
| `and` | 全部匹配 | 全部关键字都命中才判定为真 |
| `not` | 均不出现 | 所有关键字都不出现才判定为真 |

---

## 六、注意事项

### 6.1 配置生效条件

> **重要**：QKV/QFK 参数配置保存在 KBD 条目的 `signals_json`（v2 嵌套文档）中，实际运行时由 KBD 差分引擎读取。

如需修改已生效的信号配置，需更新对应 KBD 条目的 `signals_json` 字段；保存时 `validate_signals_json` 会强制校验（不通过返回 422）。

### 6.2 常见错误

| 错误现象 | 可能原因 | 解决方案 |
|---------|---------|---------|
| 变量未提取 | produces 路径不匹配 | 检查 path 是否与 acli 输出字段一致 |
| 判定不生效 | match 配置错误 | 确认 type/pattern/expected 设置是否正确 |
| 多路径失败 | 所有路径都不存在 | 添加更多容错路径或检查数据源 |
| 关键词搜索无结果 | 关键词包含状态后缀 | 系统会自动清洗，检查清洗后的关键词 |
| 关键词搜索无结果（且分类基线无对应标签） | 关键字与分类基线语义不对齐 / 生产者类型选错 | 见 §2.3：确认关键字取自本案例分类基线标签，且类型与故障性质一致（任务失败型走 task/dialog、告警型走 alert） |
| 保存 422：`Additional properties are not allowed` | 沿用 v1 扁平字段（顶层 `keyword`/`matcher`/`acquirer`） | 改为 v2 的 `acquire.args.*` 与 `match.*` |
| 判定恒真/恒假 | `match.pattern` 与 `resource_keyword` 混淆 | 见 §6 语义消歧：`match.pattern` 才是判定词 |

### 6.3 最佳实践

1. **变量命名规范**：使用大写下划线格式（如 `HOST`、`VM_ID`），便于识别
2. **多路径容错**：为关键字段配置 2-3 个备选路径
3. **判定器测试**：先用 JSON 编辑器验证配置正确性（参考《v2 参考》§7 常见错误），再切换到可视化编辑
4. **关键词规范**：LLM 抽取时建议使用纯动作关键词，状态通过单独参数指定
5. **语义消歧**：严格区分 `acquire.args.keyword`（QKV 检索词）、`resource_keyword`（QFK 资源选择器）、`match.pattern`（QFK 判定词）

---

## 七、附录

### 7.1 已注册的 QKV 工具（acquire.tool 值）

| acquire.tool | display_name | 说明 | 默认产出变量 |
|--------------|--------------|------|-------------|
| `qkv_alert` | 前端信号-告警查询 | 查询告警列表 | alert_type, end, target, type, description, host, vm |
| `qkv_task` | 前端信号-任务查询 | 查询任务列表 | type, end, target, description, host, vm, errcode_tracing, request_id, status |
| `qkv_dialog` | 前端信号-弹框日志查询 | 查询弹框日志，提取 request_id | request_id, end, line |

### 7.2 已注册的 QFK 工具（acquire.tool 值）

| acquire.tool | display_name | 说明 |
|--------------|--------------|------|
| `qfk_log` | 后端信号-日志关键字判定 | 日志关键字匹配 |
| `qfk_service` | 后端信号-服务状态判定 | 服务状态检查 |
| `qfk_vm` | 后端信号-虚拟机状态 | 虚拟机状态检查 |
| `qfk_network` | 后端信号-网络检查 | 网络连通性检查 |
| `qfk_storage` | 后端信号-存储状态 | 存储状态检查 |
| `qfk_hardware` | 后端信号-硬件状态 | 硬件状态检查 |
| `qfk_platform` | 后端信号-平台状态 | 平台组件状态检查 |
| `qfk_system` | 后端信号-系统指标 | CPU/内存/磁盘等指标 |

### 7.3 变更历史

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v2.0 | 2026-07-24 | 全文档迁移到 v2 嵌套信号模型（acquire/match/orchestrate/provenance/review）；§三/§四/§五 去除 v1 扁平词汇（acquirer/acquirer_args/keyword/match_mode/matcher）；新增关键字语义消歧与 422 常见错误；附录工具名统一为 `acquire.tool` 下划线枚举 |
| v1.2 | 2026-07-23 | 新增 §2.3 关键字须对齐分类基线、§2.4 校验与容忍原则；§6.2 补"分类基线不对齐"常见错误 |
| v1.1 | 2026-07-21 | 新增关键词清洗规则、时间格式转换说明、默认产出变量表 |
| v1.0 | 2026-07-16 | 初始版本 |

---

*文档结束。*
