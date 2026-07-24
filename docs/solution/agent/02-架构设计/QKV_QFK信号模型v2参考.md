# QKV/QFK 信号模型 v2 参考

> 版本：v2.0 ｜ 日期：2026-07-24
> 权威契约：`backend/shared/schemas/signals/signal.v2.schema.json`
> 校验入口：`backend/shared/schemas/signal_schema.py :: validate_signals_json`（保存 KBD 条目时强制校验，失败返回 HTTP 422）
>
> 本文与 `signal.v2.schema.json` **逐字段对齐**，供运营 / SRE 编写 `signals_json` 时直接对照。姊妹文档：[QKV_QFK信号配置操作指南.md](./QKV_QFK信号配置操作指南.md)（面向配置步骤）。

---

## 1. 模型总览

`signals_json` 是 KBD 条目（知识库差分条目）的一个字段，描述"**如何采集信号 + 如何判定对错**"。v2 采用**嵌套文档模型**：一个信号 = 一个"采集 + 判定"单元，整段文档形如：

```jsonc
{
  "schema_version": 2,   // 固定为 2；缺失或非 2 直接 422
  "signals": [ /* 信号数组，每个元素见 §2 / §3 */ ]
}
```

每个信号由五段组成，**仅 `acquire` 必填**，其余按需：

| 段 | 必填 | 作用 | 典型使用者 |
|----|------|------|-----------|
| `acquire` | **是** | 采集：用哪个工具（`tool`）+ 参数（`args`） | 全部 |
| `match` | 否（可 `null`） | 判定：把采集文本与 `pattern` 比对 | 后端 QFK |
| `orchestrate` | 否 | 编排：本信号产出哪些变量（`produces`）、依赖哪些变量（`requires`） | 前端 QKV |
| `provenance` | 否 | 来源：信号是前端 / 后端产生、置信度、风险 | 全部 |
| `review` | 否（`require_human_confirm` 必填） | 复核：是否需人工确认后才生效 | 全部 |

---

## 2. 逐行注释的完整示例

### 2.1 QKV 信号（前端，带 `produces` 产出变量）

```jsonc
{
  "id": "s_vm_boot_failed",        // 可选：信号唯一标识，便于引用 / 调试
  "acquire": {                      // 【必填】采集段
    "tool": "qkv_task",             // 采集工具枚举（见 §4.1）；QKV 三选一：qkv_alert / qkv_task / qkv_dialog
    "args": {                       // 该 tool 的参数对象（字段见 §4.2）
      "keyword": "启动虚拟机",       // QKV 采集关键词（acli task get -k 的检索词，非自由描述）
      "is_failed": true,            // qkv_task 专属：仅取失败任务（等价于 acli -s failed）
      "limit": 100,                 // 翻页上限，默认 100
      "timeout": 10,                // 采集超时（秒），默认 10
      "instruction": "提取启动虚拟机失败的任务" // 人类可读语义说明
    }
  },
  "match": null,                    // QKV 通常只采集不判定，置 null 或省略本段
  "orchestrate": {                  // 编排段：把采集结果字段提升为变量
    "produces": [                   // 产出变量数组
      { "name": "HOST", "path": "host|hostname|hostid" }, // 变量名 + JSON 路径（| 多路径容错）
      { "name": "VM_ID", "path": "vm_id|vmid" },
      { "name": "TASK_ID", "path": "task_id" }
    ],
    "requires": []                  // 本信号依赖的变量（QKV 一般为空）
  },
  "provenance": {                   // 来源段
    "category": "frontend",         // frontend=前端(QKV) / backend=后端(QFK)
    "method": "acli task get",      // 采集方法
    "confidence": 0.9,              // 置信度 0~1
    "risk": 0.3,                    // 风险 0~1
    "needs_review": false,          // 是否需复核
    "evidence": "QKV 任务查询"       // 证据说明
  },
  "review": {                       // 复核段
    "require_human_confirm": false, // 【必填】是否需人工确认后才生效
    "notes": ""                     // 复核备注（可选）
  }
}
```

### 2.2 QFK 信号（后端，带 `match` 判定 + 依赖变量）

```jsonc
{
  "id": "s_qcow2_lock",             // 可选标识
  "acquire": {                      // 【必填】采集段
    "tool": "qfk_system",           // 后端工具（见 §4.1）：qfk_log/system/service/vm/network/storage/hardware/platform
    "args": {
      "command": "lsof",            // acli system 子命令（qfk_system 必填）
      "host": "{{HOST}}",           // 目标主机，取变量池；特殊值 "cluster" 表示遍历集群
      "resource_keyword": "overlay2/docker", // 资源 / 主题选择器（注意：非匹配词！见 §6）
      "container": "asv-con",       // qfk_system 专属默认容器
      "timeout": 30,                // 超时秒
      "instruction": "检查 qcow2 镜像是否被占用"
    }
  },
  "match": {                        // 判定段（后端核心）
    "type": "regex",                // 判定类型：keyword / regex（见 §5）
    "pattern": "vm-disk|\\.qcow2",  // 匹配内容（keyword=关键词；regex=正则）
    "mode": "or",                   // 多词逻辑：or / and / not
    "expected": true                // 期望命中：true=应出现；false=应不出现
  },
  "orchestrate": {
    "produces": [],                 // 后端信号一般不再产出变量
    "requires": ["HOST"]            // 依赖前面 QKV 产出的 HOST 变量
  },
  "provenance": {
    "category": "backend",
    "method": "acli system lsof",
    "confidence": 0.85,
    "risk": 0.5,
    "needs_review": false,
    "evidence": "QFK 系统检查"
  },
  "review": {
    "require_human_confirm": false,
    "notes": ""
  }
}
```

---

## 3. 字段要点表

> 通用约束：除顶层 `signals` 数组内每个对象，以及 `match` 之外，所有对象均 `additionalProperties: false`——**出现未声明字段即 422**。

### 3.1 顶层

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | `const 2` | 是 | 固定为 `2`，否则校验失败 |
| `signals` | `array<signal>` | 是 | 信号数组，至少 1 个 |

### 3.2 signal（数组元素）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 否 | 信号唯一标识 |
| `acquire` | object | **是** | 采集段（见 §3.3） |
| `match` | object \| null | 否 | 判定段（见 §3.4），可省略或 `null` |
| `orchestrate` | object | 否 | 编排段（见 §3.5） |
| `provenance` | object | 否 | 来源段（见 §3.6） |
| `review` | object | 否 | 复核段（见 §3.7） |

### 3.3 acquire（采集段，必填）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tool` | enum(11) | **是** | 采集工具（见 §4.1） |
| `args` | object | **是** | 参数；按 `tool` 选对应的 `acquirer_args/<tool>.schema.json` 校验（见 §4.2） |

### 3.4 match（判定段）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | **是** | 判定类型：`keyword` / `regex`（其余类型见 §5.3 限制） |
| `pattern` | string | **是** | 匹配内容（`keyword`=关键词；`regex`=正则串） |
| `mode` | string | **是** | 多词逻辑：`or` / `and` / `not` |
| `expected` | boolean | **是** | 期望结果：`true`=应出现；`false`=应不出现 |

> ⚠️ 当前 schema 的 `match` 仅声明上述 4 个字段且 `additionalProperties: false`。`state/threshold/json_path/exists` 类型所需的 `value/operator/path/expected_value` 等额外字段**会在保存校验时被拒绝**（详见 §5.3）。

### 3.5 orchestrate（编排段）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `phase` | string | 否 | 编排阶段标记 |
| `action` | string | 否 | 动作 |
| `source` | string | 否 | 来源 |
| `target` | string | 否 | 目标 |
| `container` | string | 否 | 容器 |
| `produces` | `array<{name, type?, path?}>` | 否 | 产出变量；每项 `name` 必填，`path` 支持 `\|` 多路径容错 |
| `requires` | `array<string>` | 否 | 依赖的变量名列表（取自前面信号的 `produces`） |

### 3.6 provenance（来源段）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `category` | enum(frontend/backend) | 否 | 信号来源类别 |
| `method` | string | 否 | 采集方法 |
| `source_section` | string | 否 | 来源段落 |
| `confidence` | number | 否 | 置信度 0~1 |
| `risk` | number | 否 | 风险 0~1 |
| `needs_review` | boolean | 否 | 是否需要复核 |
| `evidence` | string | 否 | 证据说明 |

### 3.7 review（复核段）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `require_human_confirm` | boolean | **是** | 是否需人工确认后才生效 |
| `notes` | string | 否 | 复核备注 |

---

## 4. acquire.tool 枚举与 args 字段

### 4.1 枚举（共 11 个）

- **前端 QKV**：`qkv_alert` / `qkv_task` / `qkv_dialog`
- **后端 QFK**：`qfk_log` / `qfk_service` / `qfk_system` / `qfk_vm` / `qfk_network` / `qfk_storage` / `qfk_hardware` / `qfk_platform`

### 4.2 各 tool 的 `args` 字段表

> 通用字段（所有 tool 均含）：`timeout`(int, 默认 10, 采集/执行超时秒)、`instruction`(str, 语义说明)。
> 所有 `args` 对象均 `additionalProperties: false`——拼写错字段（如把 `host` 写成 `hostname`）会 422。

**qkv_alert**（acli alert get -k）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `keyword` | string | **是** | 采集关键词（acli alert get -k） |
| `limit` | int | 否 | 翻页上限，默认 100 |
| `alert_type` | string | 否 | 告警类型过滤 |

**qkv_task**（acli task get -k）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `keyword` | string | **是** | 采集关键词（acli task get -k） |
| `limit` | int | 否 | 翻页上限，默认 100 |
| `is_failed` | boolean | 否 | 仅取失败任务（默认 false，等价于 -s failed） |

**qkv_dialog**（acli dialog get -k）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `keyword` | string | **是** | 采集关键词（acli dialog get -k） |
| `limit` | int | 否 | 翻页上限，默认 100 |

**qfk_log**（acli --host {{HOST}} log get -k）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | string | **是** | 日志文件名 |
| `end` | string | 否 | 结束时间（变量池获取，如 `2026-07-21 10:00:00`） |
| `host` | string | 否 | 目标主机（变量池；`cluster`=遍历集群） |
| `resource_keyword` | string | 否 | 资源选择器（非匹配词，见 §6） |

**qfk_service**（acli service <container> <name> <command>）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `resource_keyword` | string | 否 | **服务名选择器**（acli service <container> <name> 的 `<name>`；改名消歧，非匹配词） |
| `container` | string | 否 | 服务容器，默认 `asv`（可选 asv/vn/...） |
| `command` | string | 否 | 操作子命令，如 `status` / `restart` |
| `host` | string | 否 | 目标主机 |

**qfk_system**（acli --container <c> --host {{HOST}} system <command>）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `command` | string | **是** | 系统命令，如 `lsof` / `ps auxf` / `lsblk` / `iostat` / `smartctl` |
| `container` | string | 否 | 执行容器，默认 `asv-con` |
| `host` | string | 否 | 目标主机（`cluster`=遍历集群） |
| `resource_keyword` | string | 否 | 资源/主题选择器，如 `overlay2/docker` |

**qfk_vm / qfk_network / qfk_storage / qfk_hardware / qfk_platform**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `command` | string | **是** | 对应 acli 子命令（如 `vm list`、`network <cmd>`、`storage asan disk list`、`hardware <cmd>`、`platform <cmd>`） |
| `host` | string | 否 | 目标主机（`cluster`=遍历集群） |
| `resource_keyword` | string | 否 | 资源名选择器（如虚拟机名） |

---

## 5. 判定器 match 说明

### 5.1 字段含义

- `type`：判定算法。`keyword`=子串包含；`regex`=正则匹配。
- `pattern`：匹配内容。`keyword` 时为关键词；`regex` 时为正则串（忽略大小写）。
- `mode`：多词组合逻辑，仅对「多个关键词」有意义（见 §5.2）。
- `expected`：期望值。`true`=文本应命中；`false`=文本不应命中（取反）。

### 5.2 mode（多词逻辑）

| 模式 | 说明 |
|------|------|
| `or` | 任一关键词命中即判定为真 |
| `and` | 全部关键词都命中才判定为真 |
| `not` | 所有关键词都不出现才判定为真 |

### 5.3 match 字段范围（已修复）

`backend/agent-service/app/tools/qfk/matcher.py` 的 `evaluate_matcher` **已实现 6 类**：`keyword`、`regex`、`state`、`threshold`、`json_path`、`exists`。

> ✅ **已修复（2026-07-24）**：`signal.v2.schema.json` 的 `match` 段已扩宽 —— `additionalProperties: false` 同时允许 `value`/`operator`/`path`/`expected_value`。`type` 为自由字符串（已知匹配器：keyword/regex/state/threshold/json_path/exists），`mode` 为自由字符串（已知：or/and/not；历史 fixture 的 `any` 运行时等同 or）。**目前 6 类判定均可在保存校验中通过**。注意：`threshold` 的 `value` 在 schema 中为可选，但运行时若缺 `value`/`operator` 会判定为"未知(None)"，建议始终显式给出。

多关键词 OR 匹配的安全写法：使用 `regex` + 正则或（`"pattern": "kw1|kw2"`），或 `keyword` + 列表（`pattern: ["kw1","kw2"]`，运行时接受；schema 当前按 string 校验，列表写法以运行时为准）。

---

## 6. 关键字语义消歧（极易配错）

v2 里有**三个名字相近但语义完全不同**的字段，混淆会导致"采集错对象"或"判定恒真/恒假"：

| 字段 | 属于 | 语义 | 示例 |
|------|------|------|------|
| `acquire.args.keyword` | 仅 QKV | **采集检索词**（acli `<task\|dialog\|alert>` get -k 的参数），用于"拉取哪些记录" | `"启动虚拟机"` |
| `acquire.args.resource_keyword` | 仅 QFK | **资源/主题选择器**（acli 的 `<name>`/资源定位），**不是匹配词** | `"vtpdaemon"`、`"overlay2/docker"` |
| `match.pattern` | 仅 QFK | **匹配词（唯一权威）**：判定采集文本里"该不该出现"的词 | `"vm-disk\|\\.qcow2"` |

要点：
- QFK 的"该不该命中"只由 `match.pattern` 决定；`resource_keyword` 只决定"查哪个资源"，不参与判定。
- QKV 的 `keyword` 是"检索词"，决定采到哪些原始记录，与 QFK 的 `match.pattern` 不是一回事。
- 选错 QKV 生产者类型（如给"开机失败"用 `qkv_alert`）会查错子系统、返回空，信号恒假——准确性第一杀手（详见操作指南 §2.3）。

---

## 7. 校验与常见错误

保存 KBD 条目时 `validate_signals_json` 强制校验；`additionalProperties: false` 会拒绝一切未声明字段与顶层 `keyword` 等回归写法。

| 现象 | 原因 | 解决 |
|------|------|------|
| 422：`'schema_version' is a required property` | 漏写或写成 1 | 顶层加 `"schema_version": 2` |
| 422：`'acquire' is a required property` | 少了 acquire 段 | 每个信号必须含 `acquire` |
| 422：`'tool' must be one of [...]` | tool 写错/用了 v1 的 `qkv.task`（点号） | 用下划线枚举 `qkv_task` 等 11 个 |
| 422：`Additional properties are not allowed ('keyword' was unexpected）` | 把 v1 扁平字段（顶层 `keyword`/`matcher`）搬进 v2 | 改为 `acquire.args.*` 与 `match.*` |
| 422：QKV 缺 `keyword` | qkv_* 的 `args.keyword` 必填 | 补 `args.keyword` |
| 判定恒真/恒假 | `match.pattern` 与 `resource_keyword` 混淆 | 参照 §6 区分 |
| 多关键词被拒 | 用 `keyword`+列表但 schema 仅接受 string | 改用 `regex`+`|` 或等 schema 放开（§5.3） |

---

*文档结束。与 `signal.v2.schema.json` 对齐；如发现契约已更新请以 schema 文件为准并同步本文。*
