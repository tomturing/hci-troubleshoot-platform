---
status: active
category: solution
audience: developer
last_updated: 2026-06-12
owner: hci-team
---

# SOP Markdown 命令归一化与变量来源门禁方案

## 背景

2026-06-12 在 dev 环境查询 `sop_document` 后确认，已发布 SOP「磁盘寿命到期」的真实数据如下：

| 字段 | 值 |
| --- | --- |
| id | 2 |
| source_id | sop-upload-72b051b10a3f |
| category_id | 硬件-024 |
| status | published |
| tree_validation_status | warnings |
| tree_leaf_count | 5 |
| published_at | 2026-06-11 14:46:00+00 |
| updated_at | 2026-06-11 14:53:13+00 |

该 SOP 的 Markdown 正文由人编写，包含大量现场运维习惯命令：

```bash
container_exec -n vs-cp-manager -c "smartctl -a /dev/sdX"
container_exec -n vs-cp-manager -c "smartctl.real -a /dev/sdX"
container_exec -n vs-cp-manager -c "nvme smart-log /dev/nvme0n1"
container_exec -n vs-cp-manager -c "storcli64 /c0/vall show"
```

当前 `tree_json` 会将这些命令原样写入 `diagnosis.acli_methods` 或 `prerequisite_items.description`，执行阶段再由 LLM 自行理解并选择工具。随着 `bash_exec` 升级为 `container + command + reason` 契约，如果要求 SOP 作者把 Markdown 改成 JSON tool_call，改造成本高且违背人类知识文档的职责边界。

## 第一性原理

SOP Markdown 是人类知识载体，负责表达排障意图、条件和现场命令。工具调用契约是机器执行协议，负责安全边界、审计和执行。二者不应混为一层。

因此平台应把 Markdown 中的命令视为轻量 DSL，在发布或执行前归一化为结构化工具意图：

```text
人写 Markdown 运维命令
  -> SOP 解析器/导航工具归一化
  -> ToolIntent(acli_exec/bash_exec)
  -> ReAct 调用真实工具
```

## 方案概述

新增 SOP 命令归一化层，保持 Markdown 低改造成本：

| Markdown 命令形态 | 识别结果 | 工具意图 |
| --- | --- | --- |
| `acli ...` | aCLI 命令 | `acli_exec(command=原命令)` |
| `container_exec -n <container> -c "<cmd>"` | HCI 容器命令 | `bash_exec(container=<container>, command=<cmd>)` |
| `host_exec -c "<cmd>"` | 显式 host 命令 | `bash_exec(container=host, command=<cmd>)` |
| 其他代码块命令，如 `ls -h` | 默认 host 命令 | `bash_exec(container=host, command=原命令)` |

`host_exec` 只作为可选显式写法，不强制 SOP 作者使用。

## 命令执行策略

### host

`container=host` 表示在 HCI 物理机上直接执行：

```json
{
  "tool_name": "bash_exec",
  "args": {
    "container": "host",
    "command": "ls -h",
    "reason": "检查当前目录"
  }
}
```

服务端不应生成容器 wrapper，最终下发命令就是净化后的 `ls -h`。

### 容器

HCI 容器执行优先使用用户操作层推荐命令 `container_exec`：

```bash
container_exec -n vs-cp-manager -c "smartctl -a /dev/sda" -d
```

`-d` 由服务端自动追加，因为 `bash_exec` 是一次性非交互工具调用，不是人工进入容器 shell。fallback 顺序为：

1. `container_exec`
2. `nerdctl -n k8s.io exec`
3. `docker exec`（仅兜底；HCI 上常是 alias，非交互 shell 不应依赖 alias）
4. `crictl/ctr` 底层兜底

## ToolIntent 数据结构

在 `get_sop_node` 返回中保留原始 `commands`，新增机器可执行的 `tool_calls`：

```json
{
  "commands": [
    "container_exec -n vs-cp-manager -c \"smartctl -a /dev/sda\""
  ],
  "tool_calls": [
    {
      "tool_name": "bash_exec",
      "args": {
        "container": "vs-cp-manager",
        "command": "smartctl -a /dev/sda",
        "reason": "获取 SMART 信息"
      },
      "source_command": "container_exec -n vs-cp-manager -c \"smartctl -a /dev/sda\""
    }
  ]
}
```

这样页面和人工审阅仍可展示原始命令，LLM 和执行器优先使用结构化工具意图。

## 变量来源门禁

### 现状问题

「磁盘寿命到期」当前 `variable_schema` 中，`is_sys_disk` 为 `user_input`。按语义，Agent 在需要判断系统盘/aSAN 盘分支前应调用：

```json
{
  "tool_name": "sop_request_variable",
  "args": {
    "variable_name": "is_sys_disk",
    "reason": "SOP 变量声明要求由用户确认是否系统盘"
  }
}
```

但当前系统只把 `sop_request_variable` 暴露给 LLM，未在进入节点前强制检查变量来源。LLM 可以跳过变量声明，自行执行命令判断，导致遵循度不稳定。

### 根因

1. `variable_schema` 只存储在 `sop_document`，`get_sop_node` 返回节点时没有给出“当前节点需要哪些变量、获取策略是什么”。
2. `sop_request_variable` 是可选工具，调用时机完全由 LLM 决策。
3. `sop_advance` 仅校验目标节点合法性，不校验目标节点前置条件依赖的变量是否已经按 `acquisition_strategy` 获取。
4. `merge_variable_schema()` 会保留旧 schema 中人工编辑过的字段。若管理端曾把变量策略改成 `user_input`，后续重新发布会继续保留旧值。这是保护人工配置的设计，但也会让 Markdown 中新写的 `llm_inference` 等来源不一定覆盖旧值。

### 调整

新增变量门禁：

1. 发布阶段记录变量依赖：扫描节点 `prerequisite_items`、`diagnosis`、`solution` 中的 `{var}`，形成 `required_variables`。
2. `get_sop_node` 返回当前节点和子节点的 `required_variables`，包含 `name/type/acquisition_strategy/acquisition_tool/description/is_missing`。
3. ReAct prompt 明确：若节点或分支依赖变量且未在已知变量中，必须先调用 `sop_request_variable`，禁止用命令自行替代 `user_input`、`user_confirm`。
4. `sop_advance` 增加轻量校验：目标分支条件中出现的 `user_input/user_confirm/env_injection` 变量未就绪时，返回结构化错误，提示调用 `sop_request_variable` 或补齐环境变量。

## 兼容策略

| 风险 | 处理 |
| --- | --- |
| 旧 SOP 已写 `container_exec` | 自动归一化，不要求改正文 |
| 裸 bash 命令歧义 | 默认 host；需要容器时作者继续用 `container_exec -n ... -c ...` |
| 多行代码块 | v1 只支持单条命令；多行返回 warning，提示拆分 |
| `container_exec` 无 `-d` | SOP 正文可不写，执行层自动追加 |
| 旧 variable_schema 覆盖新声明 | 管理端显示“来源与 Markdown 不一致”warning，并允许一键重置为 Markdown 来源 |

## 验收标准

- `bash_exec.container` enum 支持 `host/asv-con/vn-con/vn-agent/vs-cp-manager`。
- `bash_exec(container=host, command="ls -h")` 最终下发命令为 `ls -h`。
- `bash_exec(container=vs-cp-manager, command="smartctl -a /dev/sda")` 优先生成 `container_exec -n vs-cp-manager -c 'smartctl -a /dev/sda' -d` 语义的 wrapper。
- SOP Markdown 中 `container_exec -n vs-cp-manager -c "smartctl -a /dev/sda"` 自动归一化为 `bash_exec(container=vs-cp-manager, command="smartctl -a /dev/sda")`。
- SOP Markdown 中 `acli ...` 自动归一化为 `acli_exec`。
- SOP Markdown 中裸代码块 `ls -h` 自动归一化为 `bash_exec(container=host, command="ls -h")`。
- `is_sys_disk` 为 `user_input` 且未就绪时，Agent 必须触发 `sop_request_variable` 交互，而不是自行运行命令替代用户判断。
