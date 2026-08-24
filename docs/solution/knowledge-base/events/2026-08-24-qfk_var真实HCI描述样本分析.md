---
status: active
category: solution
audience: developer, tester, expert
last_updated: 2026-08-24
owner: team
---

# `qfk_var` 真实 HCI description 样本分析

## 1. 分析范围和证据

本次分析使用用户提供的 SQLite 文件：

```text
文件：log_new.db
SHA-256：5d7d8b839fc26b289d112931f5bad2c0b0788cbada3e009c71da568532e1fd9c
访问方式：SQLite URI mode=ro，只读查询
完整性：PRAGMA integrity_check = ok
```

附件不是合成 fixture，而是一个真实 HCI 环境的事件库快照。分析只读取表结构、统计信息和脱敏后的模式；仓库文档不保存原始主机、虚拟机、请求 ID 或完整 description。

## 1.1 现场采样方式（只读）

后续需要更新样本时，优先使用 aCLI 的结构化接口：

```bash
acli --formatter json alert list
acli --formatter json task list
```

如果需要核对 aCLI 与本地事件库的关系，在 HCI 节点上以只读 URI 查询 `/sf/log/log_new.db`。主机地址、账号和密码只通过受控 SSH/Secret 注入，不写入 KBD、脚本或文档：

```bash
sqlite3 -readonly '/sf/log/log_new.db' \
  "select a.id, a.type, a.host, a.vm, a.object_name, t.lang, t.description from alert a join alert_translation t on t.log_id=a.id where t.lang='zh_CN' limit 20;"

sqlite3 -readonly '/sf/log/log_new.db' \
  "select l.id, l.type, l.host, l.vm, l.object_name, l.status, l.errcode_tracing, t.lang, t.description from log l join log_translation t on t.log_id=l.id where t.lang='zh_CN' limit 20;"
```

注意：数据库没有 `task` 表；`task` 业务对象存储在 `log`，其中文描述来自 `log_translation`。告警必须连接 `alert_translation`，任务必须连接 `log_translation`，不能仅凭相同整数 `id` 跨表连接。

## 2. 数据源事实

### 2.1 表和记录数

| 业务对象 | 事实表 | 展示/翻译表 | 中文记录 | 中文 description 去重 | 中文 type 去重 |
|---|---|---|---:|---:|---:|
| 告警 | `alert` | `alert_translation` | 4,829 | 83 | 23 |
| 任务/操作 | `log` | `log_translation` | 1,067 | 212 | 117 |

事件时间范围（CST）：`alert` 为 2026-06-06 21:03:26 至 2026-08-24 15:02:43；`log` 为 2026-06-06 21:06:47 至 2026-08-24 14:31:02。

每条事实记录均有 `zh_CN` 和 `en_US` 一条翻译记录。`alert` 和 `log` 的事实表 `description` 含内部模板编码、控制字符和 `par` 参数块，例如 `%s`、`domain...product03HCI` 等；它不是适合直接交给 `qfk_var` 的用户语义文本。

因此，KBD/QKV 需要明确：

```text
事实字段：alert/log 的 host、vm、object_id、object_name、status、errcode_tracing 等
展示描述：alert_translation/log_translation 中指定语言的 description
```

不能把事实表 `description` 与翻译表 `description` 混用，也不能把 `alert_translation` 与 `log_translation` 跨表连接。

### 2.2 结构化字段覆盖

| 表 | `host` | `vm` | `object_name` | `description` | 其他关键字段 |
|---|---:|---:|---:|---:|---|
| `alert` | 4,829/4,829 | 149/4,829 | 4,829/4,829 | 4,829/4,829 | `object_id/status/start/end` 全量 |
| `log` | 1,067/1,067 | 343/1,067 | 804/1,067 | 1,067/1,067 | `status=2` 832 条，`status=3` 235 条；`errcode_tracing` 232 条 |

这直接决定了抽取优先级：

1. 已有结构化列的值，先走字段路径，不从 description 猜测。
2. description 只补充事实表没有的展示语义、原因、阈值、变更前后值等信息。
3. `log.vm` 主要是 VM ID，`log.object_name` 才通常是用户可读的 VM/设备名称；不能把 VM ID 当 VM 名称。

## 3. 真实 description 形态与特征提取

以下示例使用占位符替换了真实动态值，仅保留原始结构。

### 3.0 四层处理流水线

真实 description 不是一段需要先被抽象理解的自由文本，而是包含大量模板特征的半结构化文本。`qfk_var` 固定采用四层命令：

```text
第一层：确定性变量提取
  -> 第二层：变量命名和归一化
  -> 第三层：类型和基数校验
  -> 第四层：AI 兜底（仅在前三层无法确定时）
```

第一层负责找到变量候选的原文边界和上下文；第二层根据标签或模板位置把候选命名为 `vm_name`、`host`、`percent.current` 等变量；第三层验证类型和基数；第四层只处理没有稳定边界或出现新格式的文本。四层是固定流水线，普通 KBD 编辑人员只选择输入变量和目标变量。

在本附件的中文 description 中，成对符号是最明显的边界特征：中文圆括号 `（...）`、尖括号 `<...>`、中文方头括号 `【...】`，少量场景还使用 ASCII 括号。按中文翻译记录统计，5,896 条记录中 2,947 条（49.98%）至少包含一种成对符号；按去重后的 297 种 `type+description` 形态统计，257 种（86.5%）包含成对符号。未使用成对符号的描述仍可能通过字段标签和冒号、数字/百分数、数字+单位或已审核模板确定变量边界。

特征类型和真实样本示例：

| 特征 | 示例 | 作用 |
|---|---|---|
| 成对符号 | `主机（SVR_aCloud_670）`、`网口（eth3）` | 直接提供候选值边界 |
| 字段标签 + 冒号 | `源主机：172.28.24.4`、`错误码：E10023` | 通过左侧标签命名右侧值 |
| 整数/小数 | `96`、`92.35`、`7.99` | 候选数值，必须结合标签和单位 |
| 百分数 | `使用率：96%`、`剩余寿命8%` | 候选百分比，保留 `%` 证据 |
| 字母+数字组合 | `Ubuntu-26.04`、`eth0`、`E10023` | 资源名称、接口名、错误码等标识符 |
| 数字+单位 | `7.99 GB`、`96GB`、`30s` | 一个完整的 quantity 候选，不能只取数字 |
| 已审核模板 | `从（A）变更为（B）` | 处理多值、重复值和关系顺序 |

第一层只输出候选片段，例如：

```json
{
  "raw_value": "7.99 GB",
  "context": "剩余",
  "feature": "number_with_unit",
  "evidence": "剩余：7.99 GB"
}
```

它不直接把所有数字或括号内容写入变量池。`主机（SVR_aCloud_670）的计算内存使用量（92.35 GB）超过阈值（92.34 GB），剩余：7.99 GB，使用率：92%` 同时包含主机名、三个容量值和一个百分比；第二层结合 `主机`、`使用量`、`阈值`、`剩余`、`使用率` 标签分别命名和归一化，第三层再验证类型和基数。

### 3.1 告警 description

#### 虚拟机内存

```text
虚拟机（<VM_NAME>）的内存不足。当前使用<USED_PERCENT>%，超出阈值<THRESHOLD_PERCENT>%
```

#### 存储容量

```text
主机<<HOST>>的存储<<STORAGE_NAME>>当前已使用<USED_PERCENT>%，超过阈值<THRESHOLD_PERCENT>%，请删除部分闲置虚拟机或迁移虚拟机至其他存储。
```

#### 磁盘损坏

```text
检测到硬盘（主机<<HOST>>，硬盘名称：<DISK_NAME>）存在坏道，请尽快联系硬件供应商...
```

#### SSD/磁盘寿命

```text
主机（<HOST>）SSD寿命告警（<SLOT>号盘），告警盘槽位（<SLOT>），剩余寿命<REMAINING_PERCENT>%！...
```

#### 网口

```text
主机（<HOST>）的网口（<INTERFACE>）离线；...
```

#### GPU/CPU 利用率

```text
虚拟机（<VM_NAME>）的GPU利用率持续超过<THRESHOLD_PERCENT>%
虚拟机（<VM_NAME>）的CPU利用率持续超过<THRESHOLD_PERCENT>%
```

现场样本中观察到的锚点命中数量：

| 目标变量/特征模板 | 观测到的中文 description 行数 |
|---|---:|
| `vm_name`：`虚拟机（...）` | 149 |
| `host`：`主机<...>` | 1,786 |
| `host`：`主机（...）` | 181 |
| `percent.current`：`当前使用...%` | 117 |
| `percent.threshold`：`超过/超出阈值...%` | 179 |
| `disk_name`：`硬盘名称：...` | 1,676 |
| `disk_name`：`硬盘/磁盘（...）` | 1,689 |
| `interface`：`网口（...）` | 30 |

这些数量说明：有稳定锚点的场景适合平台预置抽取器；不能为每一条 description 编写一条人工正则。

### 3.2 任务 description

#### 成功/失败包装

```text
执行（<ACTION>）完成
执行系统备份失败，系统备份失败：备份存储可用空间小于10GB，请清理存储空间或切换备份存储。
```

#### 启动虚拟机失败原因

```text
没有主机能够启动这台虚拟机，具体原因如下：
主机（<HOST_1>）：此主机计算内存不足，可用计算内存<AVAILABLE> GB，虚拟机需要<REQUIRED> GB ...
主机（<HOST_2>）：虚拟机CPU核心数超过此主机当前可独占CPU核心数（<CPU_COUNT>）。
```

此类 description 里通常没有 VM 名称；VM 名称应使用 `log.object_name`，VM ID 使用 `log.vm`，失败状态使用 `log.status`。

#### 迁移虚拟机

```text
调度原因：...虚拟机<VM_NAME>调度到主机<DEST_HOST>上，...
源主机：<SOURCE_HOST>，源存储：<SOURCE_STORAGE>，目的主机：<DEST_HOST>，目的存储：<DEST_STORAGE>。
```

#### 变更记录

```text
磁盘1的磁盘大小（GB）从（<BEFORE>）变更为（<AFTER>）
```

#### 错误码

```text
...错误码：<ERROR_CODE>。
```

现场任务样本中，description 具有较强的动作/结果叙事性，但单一格式覆盖率较低：`log_translation` 有 117 个中文 type、212 个不同中文 description。任务应优先使用 `type/status/object_name/vm/errcode_tracing`，description 只抽取补充字段。

## 4. 对原设计示例的纠正

原来的示例：

```text
故障描述：vm_name=vm-001，虚拟机无法启动
```

不是本附件中观察到的真实 HCI description 形态；原正则还混入了错误的 `*` 量词和转义，不能作为 KBD 配置示例。

真实样本中，如果要获得 VM 名称，应按以下顺序：

```text
  log.object_name / alert.object_name
  -> 直接字段取值
  -> 若只能使用中文 description，再使用特征提取 + 标签/模板映射
  -> 特征无命中、多命中或格式不支持时，才进入受控 AI 兜底
```

对于稳定锚点，平台内部可以使用类似下面的实现正则，但不应让普通 KBD 编辑人员手写它：

```regex
虚拟机[（(](?<VM_NAME>[^）)\r\n]+)[）)]
```

这是实现细节，不是推荐的用户配置格式。用户配置应选择“从 description 提取 → 虚拟机名称”，而不是填写正则。

## 5. 抽取方式建议

### 5.1 方式一：结构化字段路径，默认首选

```json
{
  "operation": "field",
  "input": "{{TASK_RECORD}}",
  "path": "object_name",
  "value_type": "string"
}
```

适合：VM 名称、VM ID、host、hostname、object_id、status、request_id、errcode_tracing、object_name。

优点：不受文案、语言、标点和模板变化影响；可以直接审计到数据库列。

### 5.2 方式二：特征提取，面向普通用户

```json
{
  "operation": "feature_extract",
  "input": "{{ALERT_DESCRIPTION_ZH}}",
  "target_variable": "percent.current",
  "value_type": "percentage",
  "cardinality": "exactly_one"
}
```

第一版建议提供固定目标变量：

| 目标变量 | 目标类型 | 真实样本依据 |
|---|---|---|
| `vm_name` | string | `虚拟机（...）`、`启动虚拟机（...）` |
| `host` | string | `主机（...）`、`主机<...>`、`源主机：...` |
| `storage_name` | string | `存储<...>`、`源存储：...`、`目的存储：...` |
| `disk_name` | string | `硬盘名称：...`、`硬盘/磁盘（...）` |
| `interface_name` | string | `网口（...）` |
| `percent.current` | percentage | `当前使用...%`、`使用率：...%` |
| `percent.threshold` | percentage | `超过阈值...%`、`超出阈值...%` |
| `error_code` | string | `错误码：...` |
| `source_host` | string | `源主机：...` |
| `destination_host` | string | `目的主机：...` |
| `change_pair` | object | `从（...）变更为（...）` |

`feature_extract` 固定执行四层流水线：第一层提取候选特征，第二层使用多个已审核模板或字段标签映射变量名并记录 `template_id`，第三层验证目标类型和基数，第四层才在没有稳定边界或出现新格式时调用受控 AI；外部只暴露目标变量和类型，不暴露复杂正则。

### 5.3 方式三：JSON 路径

当变量本身是结构化 JSON 时，继续使用 JSON 路径；不应把 JSON 序列化成文本再使用 `feature_extract`。

### 5.4 方式四：受控 AI 兜底

确定性字段、前三层处理或模板映射失败后，才允许显式配置 AI 兜底；AI 只处理没有稳定边界或出现新格式的文本：

如果第一层已经找到多个稳定候选，只是第三层基数不满足，则应返回 `ambiguous`，不能把它当成新格式交给 AI 猜测。

```json
{
  "fallback": {
    "type": "ai_extract",
    "instruction": "从候选告警描述中提取虚拟机名称；只返回原文中逐字出现的名称",
    "value_type": "string",
    "cardinality": "exactly_one"
  }
}
```

AI 兜底复用现有 `qfk` AI 提取器的安全契约：

1. 只接收本节点 `requires` 指向的候选变量，不接收整个变量池。
2. 输入先转换为有界候选项；数组变量按记录编号建立逻辑行号。
3. AI 必须返回 `value` 和 `evidence_lines`/候选项引用。
4. 返回值必须逐字存在于引用文本中，类型转换后仍保留原始字面量回查。
5. 候选超限、AI 不可用、返回非法 JSON、类型错误、无证据引用均 Fail Closed。
6. AI 不能改变 `assert` 的真假判定；在 `derive` 中只能提供经过溯源校验的变量值。

## 6. 结论

真实样本不支持“所有 description 都按一条通用正则处理”的假设。`qfk_var` 应采用：

```text
结构化字段 / JSON 路径（结构化输入）
  -> 直接得到字段值

description
  -> 第一层：确定性变量提取
  -> 第二层：变量命名和归一化
  -> 第三层：类型和基数校验
  -> 第四层：AI 兜底（仅处理没有稳定边界或出现新格式）
```

description 抽取的目标不是让专家编写正则，也不是先让模型猜抽象业务槽位，而是让普通编辑人员选择“虚拟机名称、当前使用率、阈值、错误码”等目标变量；平台先发现原文特征，再维护少量经过真实 HCI 样本验证的标签/模板映射，并将模板版本、候选证据和原始字面量写入调用链。
