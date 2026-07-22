# QKV/QFK 信号配置操作指南

> 版本：v1.1
> 日期：2026-07-21
> 适用对象：运营人员、SRE 工程师
> 关联文档：[QKV_QFK扩展性与配置易用性评估.md](./02-架构设计/QKV_QFK扩展性与配置易用性评估.md)

---

## 一、概述

QKV（前端信号）和 QFK（后端信号）是关键信号架构的核心组件，用于在 KBD 差分引擎中提取和判定关键信息。本文档说明如何在 admin-ui 中配置 QKV/QFK 的参数。

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
4. 选择需要配置的工具

---

## 二、QKV 关键词清洗规则

### 2.1 自动清洗规则

为规范化 `-k` 参数值，系统会自动清洗 LLM 抽取的关键词中的类型后缀：

| 信号类型 | 清洗后缀 | 示例 |
|----------|----------|------|
| `qkv_alert` | "告警" | `"虚拟机软关机告警"` → `"虚拟机软关机"` |
| `qkv_task` | "失败"、"成功"、"运行中" | `"启动虚拟机失败"` → `"启动虚拟机"` |
| `qkv_dialog` | "弹框" | `"用户确认弹框"` → `"用户确认"` |

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

---

## 三、QKV 产出变量配置（produces）

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

1. 在工具管理页面选择 QKV 类型工具（如 `qkv.task`）
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

### 3.6 变量引用方式

配置完成后，后续信号可通过 `{{变量名}}` 引用：

```json
{
  "acquirer": "qfk.log",
  "acquirer_args": {
    "keyword": "{{HOST}}"
  }
}
```

---

## 四、QFK 后端信号字段规范

### 4.1 共有字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `instruction` | str | 否 | - | 关键信号说明 |
| `host` | str | 否 | 变量池 | 主机（变量池获取，特殊值 `cluster` 表示遍历集群） |
| `vm` | str | 否 | 变量池 | 虚拟机（变量池获取，可为空） |
| `keyword` | list[str] | **是** | - | 关键字 |
| `timeout` | int | 否 | 10 | 超时时间（秒） |
| `expected` | bool | 否 | true | 期望结果 |
| `match_mode` | str | 否 | "or" | 匹配模式：or/and/not |

### 4.2 特有字段

#### qfk_log

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `file` | str | **是** | - | 日志文件名 |
| `end` | str | 否 | 变量池 | 结束时间 |

**命令格式**：
```bash
acli --host {{HOST}} --timeout 10 log get -k "keyword" -f "file" [-t "end"]
```

#### qfk_system

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `command` | str | **是** | - | 执行命令 |
| `container` | str | 否 | "asv-con" | 容器类型 |

**命令格式**：
```bash
acli --container asv-con --host {{HOST}} --timeout 10 system lsof
```

#### qfk_service

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `service` | str | **是** | - | 服务名称 |
| `container` | str | 否 | "asv" | 容器类型（asv/vn/vn-agent/vs） |
| `action` | str | 否 | "status" | 动作 |

**命令格式**：
```bash
acli service <container> <service> <action>
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

### 4.4 配置示例

#### qfk_log 示例

```json
{
  "acquirer": "qfk_log",
  "instruction": "检查虚拟机镜像相关日志",
  "host": "{{HOST}}",
  "keyword": ["vm-disk", "qcow2"],
  "timeout": 30,
  "expected": true,
  "match_mode": "or",
  "file": "vtpdaemon.log",
  "end": "2026-07-21 10:00:00"
}
```

#### qfk_system 示例

```json
{
  "acquirer": "qfk_system",
  "instruction": "检查虚拟机镜像文件是否被占用",
  "host": "{{HOST}}",
  "vm": "{{VM}}",
  "keyword": ["vm-disk-.*\\.qcow2", "ClwDRDBClient"],
  "timeout": 30,
  "expected": true,
  "match_mode": "or",
  "command": "lsof",
  "container": "asv-con"
}
```

#### qfk_service 示例

```json
{
  "acquirer": "qfk_service",
  "instruction": "检查 vtpdaemon 服务状态",
  "host": "{{HOST}}",
  "keyword": ["running", "active"],
  "timeout": 10,
  "expected": true,
  "match_mode": "or",
  "service": "vtpdaemon",
  "action": "status"
}
```

---

## 五、QFK 判定器配置（matcher）

### 5.1 matcher 类型说明

QFK 支持 6 种判定类型：

| 类型 | 用途 | 关键参数 |
|------|------|---------|
| `keyword` | 关键字匹配 | `pattern`、`mode` |
| `regex` | 正则表达式匹配 | `pattern` |
| `state` | 状态值匹配 | `pattern` |
| `threshold` | 数值阈值比较 | `operator`、`value` |
| `json_path` | JSON 路径取值 | `path`、`expected_value` |
| `exists` | 存在性判定 | 无额外参数 |

### 5.2 匹配模式说明

| 模式 | 说明 | 判定逻辑 |
|------|------|----------|
| `or` | 任一匹配 | 任一关键字命中即判定为真 |
| `and` | 全部匹配 | 全部关键字都命中才判定为真 |
| `not` | 均不出现 | 所有关键字都不出现才判定为真 |

---

## 六、注意事项

### 5.1 配置生效条件

> **重要**：QKV/QFK 参数配置保存在 `tool_definition.parameters_schema` 中，实际运行时由 KBD 差分引擎读取 `signals_json` 配置。

如需修改已生效的信号配置，需同步更新 KBD 的 `signals_json` 字段。

### 5.2 常见错误

| 错误现象 | 可能原因 | 解决方案 |
|---------|---------|---------|
| 变量未提取 | produces 路径不匹配 | 检查 path 是否与 acli 输出字段一致 |
| 判定不生效 | matcher 配置错误 | 确认期望结果设置是否正确 |
| 多路径失败 | 所有路径都不存在 | 添加更多容错路径或检查数据源 |
| 关键词搜索无结果 | 关键词包含状态后缀 | 系统会自动清洗，检查清洗后的关键词 |

### 5.3 最佳实践

1. **变量命名规范**：使用大写下划线格式（如 `HOST`、`VM_ID`），便于识别
2. **多路径容错**：为关键字段配置 2-3 个备选路径
3. **判定器测试**：先用 JSON 编辑器验证配置正确性，再切换到可视化编辑
4. **关键词规范**：LLM 抽取时建议使用纯动作关键词，状态通过单独参数指定

---

## 六、附录

### 6.1 已注册的 QKV 工具

| namespace | display_name | 说明 | 默认产出变量 |
|-----------|--------------|------|-------------|
| qkv.alert | 前端信号-告警查询 | 查询告警列表 | alert_type, end, target, type, description, host, vm |
| qkv.task | 前端信号-任务查询 | 查询任务列表 | type, end, target, description, host, vm, errcode_tracing, request_id, status |
| qkv.dialog | 前端信号-弹框日志查询 | 查询弹框日志，提取 request_id | request_id, end, line |

### 6.2 已注册的 QFK 工具

| namespace | display_name | 说明 |
|-----------|--------------|------|
| qfk.log | 后端信号-日志关键字判定 | 日志关键字匹配 |
| qfk.service | 后端信号-服务状态判定 | 服务状态检查 |
| qfk.vm | 后端信号-虚拟机状态 | 虚拟机状态检查 |
| qfk.network | 后端信号-网络检查 | 网络连通性检查 |
| qfk.storage | 后端信号-存储状态 | 存储状态检查 |
| qfk.hardware | 后端信号-硬件状态 | 硬件状态检查 |
| qfk.platform | 后端信号-平台状态 | 平台组件状态检查 |
| qfk.system | 后端信号-系统指标 | CPU/内存/磁盘等指标 |

### 6.3 变更历史

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v1.1 | 2026-07-21 | 新增关键词清洗规则、时间格式转换说明、默认产出变量表 |
| v1.0 | 2026-07-16 | 初始版本 |

---

*文档结束。*