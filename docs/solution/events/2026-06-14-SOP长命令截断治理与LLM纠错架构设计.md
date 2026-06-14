# SOP 长命令截断治理与 LLM 命令约束自纠错架构设计方案

> **案例分析工单**：`Q2026061321687`（磁盘 SSD 寿命告警排障）
> **会话 ID**：`f5a273fd-75d4-4bf6-a7f6-a2cf3564d150`
> **核心痛点**：结构化 CLI 大输出被 smart_truncate 截断导致信息丢失 → Agent 盲目 SSH 管道 python 过滤 → 物理机无 Python 环境报错 → 连续 8 步 grep 模糊匹配混乱 → SOP 技能命名偏差导致变量池调用完全失效。
> **文档版本**：v2.0（经代码精确校对版）
> **归档位置**：`docs/solution/agent/SOP长命令截断治理与LLM纠错架构设计.md`

---

## 1. 工单 `Q2026061321687` 实案诊断审计报告

系统调取了该工单对应会话 `f5a273fd-75d4-4bf6-a7f6-a2cf3564d150` 的全部 `message` 对话历史与 `tool_result` 工具执行记录，进行完整复盘。

### 1.1 诊断步骤与工具调用时序审计表

下表为 Agent 在推进诊断分支 `n-1-2`（aSAN盘寿命异常）和 `n-1-2-2`（硬件RAID阵列盘）过程中的完整审计轨迹（共 15 步）：

| 步序 | 工具名称 | 执行边界 | 输入命令 | 执行结果 / 退出码 | 耗时 | 痛点分析 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **3** | `get_sop_node` | 本地 | `{"node_id": "n-1"}` | 返回进入条件及命令提示 | 31ms | SOP 引导正常 |
| **4** | `get_active_alerts` | 本地 | `{}` | 返回 `vs_disk_warn`，SVR_aCloud_670 1号盘寿命告警（剩余3%） | 102ms | 正常命中告警 |
| **5** | `acli_exec` | 平台中转 | `acli --formatter json platform node list` | 返回主机列表含 `172.28.24.4` | 64ms | ⚠️ **痛点A**：Agent 完全绕过变量池 `node_ip` 的 `skill_call:hci-alert-parsing` 获取路径，手动查 IP |
| **6** | `sop_advance` | 本地 | `{"target_node_id": "n-1-2"}` | 推进到 `n-1-2`（aSAN盘寿命异常） | 8ms | 正常 |
| **7** | `get_sop_node` | 本地 | `{"node_id": "n-1-2"}` | 返回诊断命令说明 | 13ms | 正常 |
| **8** | `acli_exec` | 平台中转 | `acli --formatter json storage asan disk list` | **输出 >4000 字节触发 smart_truncate** | 812ms | ⚠️ **痛点B（根因）**：`smart_truncate` 保留首尾 20% + 仅保留 error/fail/exception 关键词行，正常磁盘 JSON 块无错误关键词 → 被整段剥离 |
| **9** | `bash_exec` | host | `ssh -t 172.28.24.4 'acli --formatter json storage asan disk list' 2>/dev/null` | **再次触发平台截断** | 786ms | ⚠️ **痛点C**：Agent 以为 SSH 到目标主机可绕过截断，但结果仍经平台管道传回，`smart_truncate` 再次生效 |
| **10** | `bash_exec` | host | `ssh ... \| python3 -c "import sys, json; ..."` | `bash: python3: command not found`（Exit Code 127） | 412ms | ⚠️ **痛点D（主根因）**：物理宿主机为极简裁剪内核，无 Python 解释器 |
| **11** | `bash_exec` | host | `ssh ... \| python -c "..."` | `bash: python: command not found`（Exit Code 127） | 390ms | ⚠️ **痛点D续**：降级 python 也失败，共浪费 2 轮约 800ms |
| **12** | `bash_exec` | host | `ssh ... \| grep -A5 '"disk_name": "1号盘"'` | 返回 disk_sn/disk_size，**缺少 dev 字段** | 224ms | ⚠️ **痛点E**：-A5 行数太少，dev 字段位于磁盘块上方 |
| **13** | `bash_exec` | host | `ssh ... \| grep -B10 '"disk_name"...' \| grep -E '"(dev\|disk_sn\|...)"'` | 返回 `/dev/sdc`，**混杂其他节点的 1号盘** | 301ms | ⚠️ **痛点E续**：多主机环境，grep 无法区分节点归属 |
| **14** | `bash_exec` | host | `ssh ... \| grep -B20 '"disk_name"...' \| grep -E '...'` | 混合字段输出增多 | 295ms | 持续盲目扩展 -B 范围 |
| **15** | `bash_exec` | host | `ssh ... \| grep -B30 '"disk_name"...' \| grep -E '...'` | 更长的混合块 | 310ms | 仍无法收敛 |
| **16** | `bash_exec` | host | `ssh ... \| grep -E '"(dev\|disk_sn\|...)"' \| head -60` | 全局快照，无主机过滤 | 280ms | 耗尽探索思路 |
| **17** | `bash_exec` | host | `... \| grep -A1 -B1 "host-70e284243e19"` | 最终勉强匹配到 dev 为 `/dev/sdc` | 240ms | 靠最后一步规避了污染 |

**总结**：8步物理 bash 调用中，有效信息仅在步骤 17 才最终收集完整。核心浪费：Python 失败 2 次（约 800ms），grep 反复调整 5 次（约 1600ms）。

---

### 1.2 核心痛点根因深度分析

#### 痛点A：SOP 技能策略命名偏差 → 变量池调用静默失效

**现状（错误）**：SOP Markdown 文档中 `node_ip` 变量声明的获取策略写法为 `skill:alert-parsing`。

**实际代码现状（通过代码核查确认）**：

1. `engine.py` 中的策略路由逻辑（[engine.py#L443-L600](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/memory/variable_pool/engine.py#L443-L600)）**没有**对 `skill:xxx` 前缀格式进行特殊解析路由。变量引擎支持的策略枚举为：
   - `sop_default`、`tool_call`、`user_confirm`、`user_input`、`derived`
   - `skill_call`（正式技能调用策略，需配合 `acquisition_tool` 字段）
   - `env_injection` / `env_context` / `env:xxx`（环境注入）
   - `tool`（旧版别名，兼容 tool_call）

2. 当策略字符串是 `skill:alert-parsing` 时，**不匹配任何已知策略分支**，引擎直接 fallthrough 到末尾的 `user_input` 路径，向用户弹出输入框。

3. 数据库中注册的真实技能名为 **`hci-alert-parsing`**（[03_skill_definitions.sql#L205](file:///mnt/d/aihci/hci-troubleshoot-platform/database/seeds/03_skill_definitions.sql#L205)）。

4. 正确的变量 Schema 写法应为：
   ```json
   {
     "acquisition_strategy": "skill_call",
     "acquisition_tool": "hci-alert-parsing"
   }
   ```

**结论**：`skill:alert-parsing` 从来没有被任何路由匹配，`node_ip` 的 JIT 技能获取完全静默失效，Agent 只能手动用 `acli platform node list` 绕过。

---

#### 痛点B：`smart_truncate` 的"结构化数据杀手"效应

系统内置的 [`utils.py:smart_truncate`](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/core/utils.py#L5) 截断策略（代码已核查）：

```python
# utils.py L25-L51（实际代码）
head_limit = int(max_chars * 0.2)    # 保留首部 20%（约 800 字符）
tail_limit = int(max_chars * 0.2)    # 保留尾部 20%（约 800 字符）
middle_limit = max_chars - head_limit - tail_limit - 150  # 中间仅约 2250 字符

# 中间部分：仅保留包含以下关键词的行（L51）
error_patterns = ["error", "fail", "exception", "critical", "fatal", "panic"]
```

**面对 `acli --formatter json storage asan disk list` 的 JSON 输出时的缺陷**：

- 正常磁盘对象：`"fault": "NONE"`, `"status": "ok"` → **无任何错误关键词 → 整段被剥离**
- 告警磁盘（剩余寿命 3%）：状态字段为 `"remaining_life": 3` → **也不含 error/fail 关键词 → 同样被剥离**
- 头部 800 字符仅包含 JSON 外层结构和前几块磁盘，尾部 800 字符包含最后几块磁盘，目标磁盘极大概率处于中间段 → **被彻底抹除**

---

#### 痛点C：`bash_exec` 的 SSH 管道同样受到平台截断约束

[executor.py#L600-L620](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L600-L620) 明确显示：所有通过 `bash_exec` 和 `acli_exec` 工具返回的 `stdout` 都经过 `smart_truncate(raw_stdout, STDOUT_MAX_CHARS)` 处理，`STDOUT_MAX_CHARS = 4000`（[executor.py#L274](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L274)）。SSH 仅改变执行路径，输出结果仍然通过前端 → terminal_bridge → Redis blpop → executor 管道回传，截断不可回避。

---

#### 痛点D：物理宿主机无 Python 解释器

裁剪版的 HCI 物理宿主机不包含 Python 运行环境。当前 `executor.py` 对 127 退出码的处理仅记录 `ExitCodeMeaning.COMMAND_NOT_FOUND`（[executor.py#L627-L628](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L627-L628)），**没有针对 `python` 命令的专项引导性纠错提示**，LLM 仅收到原始的 `command not found` 错误，缺乏明确的替代指引。

---

#### 痛点E：`grep` 无法处理嵌套 JSON 的多主机上下文

`acli storage asan disk list` 输出的是集群全量磁盘 JSON 数组，每个磁盘对象跨多行。`grep -B10 '"disk_name": "1号盘"'` 会同时匹配集群内**所有节点的 1号盘**，不能区分主机归属，导致数据污染。

---

## 2. 需要什么信息 + 如何减少步骤

### 2.1 最小必要信息集

通过 SOP 分析，`n-1-2`（aSAN 盘寿命告警）场景下所需的全量信息：

| 变量名 | 内容 | 最优获取路径 | 当前问题 |
|--------|------|------------|---------|
| `node_ip` | 告警节点 IP（`172.28.24.4`） | `skill_call:hci-alert-parsing` | 策略命名错误，从未执行 |
| `disk_name` | 磁盘编号（`1号盘`） | `skill_call:hci-alert-parsing` | 同上 |
| `disk_sn` | 磁盘序列号（用于 smartctl） | 从 `asan disk list` JSON 提取 | 数据被 smart_truncate 截断 |
| `disk_dev` | 设备路径（`/dev/sdc`，用于 smartctl） | 从 `asan disk list` JSON 提取 | 同上 |

### 2.2 最优化诊断路径（理想 5 步）

| 步序 | 工具 | 操作 | 预期结果 |
|:---|:---|:---|:---|
| 1 | `get_active_alerts` | 获取告警列表 | 确认 `vs_disk_warn`，`node_ip` + `disk_name` 从告警直接提取 |
| 2 | `sop_request_variable` | 请求 `node_ip` | `skill_call:hci-alert-parsing` JIT 执行，返回 `172.28.24.4` |
| 3 | `sop_request_variable` | 请求 `asan_disks` | `tool_call:acli_exec` 调用，完整 JSON 存入 Redis 缓存 |
| 4 | `sop_request_variable` | 请求 `disk_sn`、`disk_dev` | `json_extract` 策略从 Redis 缓存的 JSON 用 JSONPath 提取，**无需再次调用 SSH** |
| 5 | `bash_exec` | 执行 `smartctl -a /dev/sdc` | 基于已有变量直接诊断 |

**目标**：从当前 15 步压缩到 5 步，消除 10 步无效重试。

---

## 3. LLM 命令行生成约束与自纠错机制设计 (LLM Guardrail)

### 3.1 工具定义描述静态注入（Static Guardrail）

**方案**：在 `01_tool_definitions.sql` 中升级 `acli_exec` 和 `bash_exec` 的 `description` 字段，直接嵌入约束规则。此字段会在每次 ReAct 推理时作为 Function Calling Schema 传入 LLM。

**当前现状（代码已核查）**：`acli_exec` description 在 [01_tool_definitions.sql#L175-L204](file:///mnt/d/aihci/hci-troubleshoot-platform/database/seeds/01_tool_definitions.sql#L175-L204) 中有纠错技巧说明，但**缺少 Python 禁用约束和 jq 强制使用指引**。

**升级方案（具体 SQL）**：

```sql
-- database/seeds/01_tool_definitions.sql
-- 在 acli_exec 的 ON CONFLICT DO UPDATE 中追加以下描述尾部
UPDATE tool_definition SET description = description || E'\n\n⚠️ 命令生成约束（必须遵守）：
[禁止] 目标 HCI 物理宿主机为极简裁剪内核，【未安装 python / python3】。严禁生成任何含有 python 关键字的管道命令，否则直接报错 Exit Code 127。
[限制] 平台对所有命令输出强制执行 4000 字符截断（smart_truncate）。对可能返回大体量 JSON 的命令（disk list / vm list / node list），【必须】在管道端进行服务端过滤后再返回。
[推荐] JSON 大输出过滤：优先使用 jq。示例：
  acli --formatter json storage asan disk list | jq '\''.data.disks[] | select(.host_name == "目标主机名" and .disk_name == "1号盘")'\''
[推荐] 日志过滤：使用 grep。示例：
  acli log get --lines 1000 | grep -B2 -A10 "ERROR"
[兜底] 若 jq 不可用，使用 python -m json.tool 或 grep 进行等效过滤。'
WHERE tool_name = 'acli_exec';
```

**bash_exec 同步升级**：增加同等约束，容器 `host` 模式下无 Python 解释器。

---

### 3.2 物理执行错误自纠错反射（Dynamic Reflection）

**当前现状（代码已核查）**：[executor.py#L622-L632](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L622-L632) 对退出码 127 仅映射为 `ExitCodeMeaning.COMMAND_NOT_FOUND`，无专项 python 纠错文案。

**改造方案（精确代码定位）**：在 [executor.py#L622-L633](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L622-L633) 的退出码语义判定区块中，在 `meaning = ExitCodeMeaning.COMMAND_NOT_FOUND` 之后追加 Python 专项纠错：

```python
# executor.py — 在退出码语义判定区块追加（L627-L633 之后插入）
if exit_code != 0:
    meaning = ExitCodeMeaning.UNKNOWN_ERROR
    check_text = f"{raw_stdout}\n{raw_stderr}".lower()

    if exit_code == 127 or "command not found" in check_text:
        meaning = ExitCodeMeaning.COMMAND_NOT_FOUND
        # ✅ 新增：专项 Python 纠错引导
        if "python" in cleaned_command.lower() and "command not found" in check_text:
            stderr = (
                f"[Error 127] bash: python3: command not found.\n"
                "⚠️ 自纠错提示：目标 HCI 物理宿主机【没有 Python 运行环境】。\n"
                "请立即放弃 python 管道过滤策略，改用以下方案：\n"
                "  - JSON 过滤：使用 jq。例：acli --formatter json storage asan disk list | jq '.data.disks[] | select(.host_name == \"目标节点名\")'\n"
                "  - 文本过滤：使用 grep -B10 -A10。\n"
                "重新生成命令后再次执行。"
            )
```

---

### 3.3 CommandSanitizer 禁止规则增强（可选强制拦截）

**当前现状**：[CommandSanitizer](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L101) 中的 `_FORBIDDEN_PATTERNS` 列表（[executor.py#L116-L131](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L116-L131)）目前拦截命令替换、路径穿越等高危操作，但**未拦截 `python` 管道**。

**可选方案**：在 `_FORBIDDEN_PATTERNS` 增加 python 检测：

```python
# executor.py — _FORBIDDEN_PATTERNS 追加
(r"\|\s*python[23]?\s+-", "python 管道过滤（物理机无 Python 环境，请改用 jq/grep）"),
```

> ⚠️ **风险说明**：此方案会彻底拦截所有 python 管道，包括可能在未来容器环境中合法使用的场景。建议先通过工具描述约束（§3.1）软性引导，仅在问题持续时升级为硬性拦截。

---

## 4. 后端暂存与 JIT 变量提取引擎实现方案

### 4.1 整体架构决策

**问题核心**：`acli storage asan disk list` 返回约 20~50KB 的 JSON，包含集群全量磁盘信息。`smart_truncate` 为保护 LLM context window（4000字符限制）而截断，但这导致 Agent 丧失对目标磁盘的定位能力。

**方案选择**：

| 方案 | 描述 | 优点 | 缺点 |
|-----|------|------|------|
| **方案一（现状）** | 全量传入 LLM | 无需改造 | 超出 context window，提示词膨胀，推理成本倍增 |
| **方案二（推荐）** | 后端缓存 + 变量池 JSONPath 提取 | Agent 仅收到精确结果，无冗余 | 需改造 executor + engine |
| **方案三** | 在 acli 命令侧加 jq 过滤 | 简单直接 | 依赖 LLM 知道正确 JSONPath；多主机混合时仍可能错误 |

**推荐方案二**：完全在后端无损保存原始大文本，通过精确 JSONPath 提取目标子字段注入变量池，LLM 拿到的是已经处理好的结构化答案，无需在终端反复尝试。

---

### 4.2 Redis 缓存暂存设计

**在 [`executor.py`](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py) 的截断逻辑处增加全量缓存写入**：

**改造位置**：[executor.py#L600-L663](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L600-L663) 中，在 `truncated = len(raw_stdout) > self.STDOUT_MAX_CHARS` 判定后紧接写入：

```python
# executor.py — 在截断判定后（L604 之后）追加
raw_stdout = result_data.get("stdout") or ""
raw_stderr = result_data.get("stderr") or ""
truncated = len(raw_stdout) > self.STDOUT_MAX_CHARS

# ✅ 新增：大输出缓存到 Redis，供变量池 JIT JSONPath 提取使用
if truncated and raw_stdout:
    cache_key = f"cmd_cache:{exec_id}"
    try:
        await self._redis.client.setex(cache_key, 1800, raw_stdout.encode("utf-8"))
        logger.info(
            event="cmd_output_cached",
            exec_id=exec_id,
            cache_key=cache_key,
            raw_size=len(raw_stdout),
            truncated_size=self.STDOUT_MAX_CHARS,
        )
    except Exception as cache_err:
        logger.warning(
            event="cmd_output_cache_failed",
            exec_id=exec_id,
            error=str(cache_err),
        )

stdout = smart_truncate(raw_stdout, self.STDOUT_MAX_CHARS) if truncated else raw_stdout
```

**同时需要**：在 `ExecResult` 返回值中追加 `exec_id` 字段，使变量池引擎可以关联 Redis Key：

```python
# executor.py — ExecResult dataclass（L59-L92）新增字段
exec_id: str | None = None   # 本次执行流水号，用于缓存 Key 关联
```

**缓存参数**：
- Key：`cmd_cache:{exec_id}`（`exec_id` 在 `BridgeRelayExecutor.execute` 入口处生成，已有实现 [executor.py#L344](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py#L344)）
- TTL：1800 秒（30 分钟，覆盖单次排障会话周期）
- 编码：UTF-8 bytes

---

### 4.3 `json_extract` 变量提取策略设计

在 `variable_schema` 中引入新的获取策略 `json_extract`，专用于从已缓存的大文本中按 JSONPath 精确提取子字段：

#### 变量 Schema 设计（适用于磁盘 SSD 寿命诊断 SOP）：

```json
[
  {
    "name": "asan_disks",
    "display_name": "aSAN 磁盘列表",
    "type": "json",
    "acquisition_strategy": "tool_call",
    "acquisition_tool": "acli_exec",
    "acquisition_args": {
      "command": "acli --formatter json storage asan disk list | jq .",
      "reason": "获取集群全量 aSAN 磁盘信息，供后续子字段提取"
    },
    "output_path": "stdout",
    "depends_on": ["node_ip"],
    "description": "集群全量磁盘 JSON（在后端 Redis 缓存，不注入 LLM）"
  },
  {
    "name": "disk_sn",
    "display_name": "故障磁盘序列号",
    "type": "string",
    "acquisition_strategy": "json_extract",
    "depends_on": ["asan_disks", "disk_name"],
    "expression": "$.data.disks[?(@.host_name == '{node_hostname}' && @.disk_name == '{disk_name}')].disk_sn",
    "description": "从 aSAN 磁盘列表中精确提取目标节点的指定磁盘序列号"
  },
  {
    "name": "disk_dev",
    "display_name": "故障磁盘设备路径",
    "type": "string",
    "acquisition_strategy": "json_extract",
    "depends_on": ["asan_disks", "disk_name"],
    "expression": "$.data.disks[?(@.host_name == '{node_hostname}' && @.disk_name == '{disk_name}')].dev",
    "description": "从 aSAN 磁盘列表中精确提取目标磁盘设备路径（如 /dev/sdc）"
  }
]
```

> **关键点**：`expression` 中同时使用 `host_name` 和 `disk_name` 双重过滤，解决多主机"1号盘"数据混淆问题。

---

### 4.4 变量池 JIT 提取器新增策略路由实现

**改造位置**：[`engine.py`](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/memory/variable_pool/engine.py) 的 `sop_request_variable` 函数，在 `strategy == "tool"` 兼容分支之前（约 L686 之后）新增 `json_extract` 处理分支：

```python
# engine.py — 在 strategy == "user_confirm" 分支之前新增
if strategy == "json_extract":
    import json
    try:
        from jsonpath_ng import parse as jsonpath_parse
    except ImportError:
        return {
            "error": "json_extract_dependency_missing",
            "message": "json_extract 策略需要 jsonpath-ng 依赖，请在 pyproject.toml 中添加 jsonpath-ng>=1.6",
        }

    # 1. 检验 depends_on 前置依赖
    if not depends_on:
        return {"error": f"变量 {variable_name} 的策略为 json_extract，必须声明 depends_on 依赖的父变量名"}

    dependency_name = depends_on[0]
    dependency_payload = context_variables.get(dependency_name)
    if not dependency_payload:
        return {
            "error": "sop_variable_dependency_missing",
            "message": f"变量 {variable_name} 的 json_extract 前置依赖 {dependency_name} 尚未就绪，请先调用 sop_request_variable(variable_name='{dependency_name}')",
            "variable_name": variable_name,
            "missing_dependencies": [dependency_name],
        }

    # 2. 提取 exec_id，尝试从 Redis 取完整原始数据
    exec_id_for_cache = (
        dependency_payload.get("exec_id")
        if isinstance(dependency_payload, dict)
        else None
    )

    raw_data_str: str | None = None

    if exec_id_for_cache:
        try:
            from shared.database.redis import RedisManager
            redis_client = getattr(tool_executor, "_redis", None)
            if redis_client:
                cache_key = f"cmd_cache:{exec_id_for_cache}"
                cached_bytes = await redis_client.client.get(cache_key)
                if cached_bytes:
                    raw_data_str = cached_bytes.decode("utf-8") if isinstance(cached_bytes, bytes) else cached_bytes
                    logger.info(
                        event="json_extract_cache_hit",
                        variable_name=variable_name,
                        exec_id=exec_id_for_cache,
                        raw_size=len(raw_data_str),
                    )
        except Exception as redis_err:
            logger.warning(
                event="json_extract_redis_failed",
                variable_name=variable_name,
                error=str(redis_err),
            )

    # 3. 缓存穿透兜底：Redis 无数据时退化使用已截断的 value
    if not raw_data_str:
        raw_data_str = (
            dependency_payload.get("value")
            if isinstance(dependency_payload, dict)
            else str(dependency_payload)
        )
        logger.warning(
            event="json_extract_cache_miss_fallback",
            variable_name=variable_name,
            note="Redis 缓存已失效，使用已截断数据降级提取，结果可能不完整",
        )

    if not raw_data_str:
        return {"error": f"前置依赖 {dependency_name} 的数据内容为空，json_extract 失败"}

    # 4. 解析 JSON
    try:
        json_data = json.loads(raw_data_str)
    except json.JSONDecodeError as je:
        return {"error": f"依赖变量 {dependency_name} 的输出非合法 JSON: {str(je)}"}

    # 5. 渲染 expression 中的变量占位符
    expression_str = var_def.get("expression", "")
    if not expression_str:
        return {"error": f"变量 {variable_name} 的 json_extract 策略必须指定 expression（JSONPath）"}

    unwrapped_ctx = _unwrap_context_variables(context_variables)
    try:
        expression_str = expression_str.format(**unwrapped_ctx)
    except KeyError as ke:
        return {"error": f"expression 占位符 {ke} 对应的上下文变量未就绪"}

    # 6. JSONPath 匹配
    try:
        jsonpath_expr = jsonpath_parse(expression_str)
    except Exception as parse_err:
        return {"error": f"JSONPath 表达式语法错误: {expression_str} — {str(parse_err)}"}

    matches = [m.value for m in jsonpath_expr.find(json_data)]

    if not matches:
        return {
            "error": "json_extract_no_match",
            "message": (
                f"在 {dependency_name} 的{'完整' if exec_id_for_cache else '截断'}数据中，"
                f"使用 JSONPath `{expression_str}` 未匹配到任何结果。"
                "请检查 node_hostname/disk_name 等过滤变量是否与数据中字段名完全一致。"
            ),
            "expression": expression_str,
        }

    extracted_value = matches[0]
    logger.info(
        event="json_extract_success",
        variable_name=variable_name,
        expression=expression_str,
        extracted_value=str(extracted_value)[:100],
    )
    return {"ok": True, "value": extracted_value, "source": "json_extract"}
```

---

## 5. SOP 变量获取策略规范化与公共解析层设计

为了彻底避免各微服务中对 `acquisition_strategy` 策略名及冒号参数重复编写、难以维护的乱象，我们根据第一性原理将变量获取策略的定义、简写和解析完全归一化。

### 5.1 统一规范："实体_动作" 格式与冒号参数简写

所有的变量获取策略统一归一化为标准的 **`实体_动作` (Entity_Action)** 形式，同时支持携带冒号 `:` 的简写形式。冒号右侧作为策略参数（可存入 `acquisition_tool`、参数名或默认值中）。

| 规范策略名 (Entity_Action) | 允许的简写形式 | 冒号带参示例 (规范名) | 冒号带参示例 (简写名) | 参数语义说明 |
|:---|:---|:---|:---|:---|
| **`sop_default`** | `sop` | `sop_default:default_val` | `sop:default_val` | 变量默认值 |
| **`env_injection`** | `env` | `env_injection:VAR_NAME` | `env:VAR_NAME` | 环境变量名 |
| **`user_input`** | `user` | - | - | 无参，直接请求用户输入 |
| **`user_confirm`** | `confirm` | - | - | 无参，待推荐值就绪后用户确认 |
| **`tool_call`** | `tool` | `tool_call:acli_exec` | `tool:acli_exec` | 绑定的命令/工具名 |
| **`skill_call`** | `skill` | `skill_call:hci-alert-parsing`| `skill:hci-alert-parsing` | 绑定的诊断技能名 |
| **`llm_inference`** | `llm` | `llm_inference:hint_text` | `llm:hint_text` | LLM 推理提示词 |
| **`agent_pass`** | `agent` | `agent_pass:key_name` | `agent:key_name` | 上一阶段透传的键名 |
| **`derived`** | - | - | - | 无参，配合 expression 表达式 |
| **`json_extract`** | - | - | - | 无参，配合 expression 表达式 |

### 5.2 SOP Markdown 变量声明的极简语法

通过统一的公共解析逻辑，SOP Markdown 文档中的变量表格可以直接支持极简的冒号参数写法，**不需要**在下方使用多行甚至多列表格来声明复杂的 `acquisition_tool` 键值。

*   **Markdown 中的写法（推荐的极简写法）：**
    ```markdown
    | 变量名 | 类型 | 来源 | 说明 |
    |---|---|---|---|
    | node_ip | string | env:node_ip | 故障节点 IP |
    | disk_name | string | env:disk_name | 故障磁盘名称 |
    | alert_ip | string | skill:hci-alert-parsing | 告警硬盘所在主机 IP |
    | disk_dev | string | json_extract | 磁盘设备路径 |
    ```

*   在 `kb-service` 解析 SOP Markdown 时，`_parse_variable_section` 内部委托给公共解析层，自动将 `env:node_ip` 映射为 `acquisition_strategy = "env_injection"` 且 `acquisition_tool = "node_ip"`; 将 `skill:hci-alert-parsing` 映射为 `acquisition_strategy = "skill_call"` 且 `acquisition_tool = "hci-alert-parsing"`。

### 5.3 公共解析层 `shared.utils.acquisition_strategy` 实现

我们在 `backend/shared/utils/acquisition_strategy.py` 中实现了统一的解析与校验逻辑，向后兼容旧版的 `env_context` 和 `tool`，并供 `kb-service` 的 parser、`agent-service` 的变量引擎等多个模块共同引用：

```python
# 核心结构体
@dataclass(frozen=True)
class ParsedStrategy:
    strategy: str           # 规范化策略名，如 "skill_call"
    parameter: str | None   # 冒号右侧的参数，如 "hci-alert-parsing"
    raw: str

    @property
    def acquisition_tool(self) -> str | None:
        # 仅对 tool_call / skill_call / agent_pass 抛出 parameter
        if self.strategy in (STRATEGY_TOOL_CALL, STRATEGY_SKILL_CALL, STRATEGY_AGENT_PASS):
            return self.parameter
        return None
```

### 5.4 `hci-alert-parsing` 技能的前置依赖注入

`hci-alert-parsing` 技能在 JIT 执行时，需要 `alert_logs` 作为原始数据输入。我们在 SOP 初始化阶段自动将 `case.metadata` 中的告警原始数据注入变量池，作为 `alert_logs` 以便技能可以无缝调用。

**改造文件**：`backend/conversation-service/app/routes/sop_execution.py` 的 `sop_create_execution` 接口，在构建 `context_variables` 的初始值时增加：

```python
# sop_execution.py — 初始化变量池时注入告警源
if case and case.get("metadata"):
    metadata = case["metadata"]
    alert_data = metadata.get("alerts") or metadata.get("alert_info") or {}
    if alert_data:
        initial_variables["alert_logs"] = {
            "value": alert_data,
            "source": "env_injection",
            "injected_at": datetime.utcnow().isoformat(),
        }
```

---

## 6. 架构改造涉及模块与实施清单

### 6.1 Phase 1：静态 Guardrail（最快落地，1天内）

#### `database/seeds/01_tool_definitions.sql`
- 更新 `acli_exec` 的 `description` 字段，追加 Python 禁用 + jq 推荐约束
- 更新 `bash_exec` 的 `description` 字段，追加 container=host 时无 Python 的限制说明

> **验证方式**：重新运行 db-seed Job，观察 `acli_exec` 工具描述是否更新。

---

### 6.2 Phase 2：动态 Reflection（中期，2~3天）

#### `backend/agent-service/app/tools/acli/executor.py`
1. **纠错 stderr 重写**：在 L627 的 `COMMAND_NOT_FOUND` 分支内，检测 `cleaned_command` 包含 `python` 时重写 `stderr` 为引导性纠错文案
2. **raw_stdout 缓存写入**：在 L604 截断判定后，增加 `await self._redis.client.setex(f"cmd_cache:{exec_id}", 1800, raw_stdout.encode())` 写入逻辑
3. **ExecResult 追加 exec_id 字段**：使变量池能关联 Redis Key

#### `backend/agent-service/app/memory/variable_pool/engine.py`
1. **新增 `json_extract` 策略路由**：在 `strategy == "user_confirm"` 之前插入完整处理分支（见 §4.4）
2. **引入 `jsonpath-ng` 依赖**：在 `backend/agent-service/pyproject.toml` 中添加 `jsonpath-ng>=1.6`

---

### 6.3 Phase 3：变量命名规范化（SOP 层，需逐文档修正）

#### SOP Markdown 文档（所有涉及 `skill:` 前缀的变量声明）
- 全局替换：`skill:hci-alert-parsing` → `acquisition_strategy: skill_call` + `acquisition_tool: hci-alert-parsing`
- 使用脚本扫描 `backend/kb-service/data/sop/` 下所有 `.md` 文件：
  ```bash
  grep -rn "skill:" backend/kb-service/data/sop/
  ```

#### `backend/conversation-service/app/routes/sop_execution.py`
- 在 `sop_create_execution` 初始化时注入 `alert_logs` 到 `context_variables`

---

### 6.4 依赖清单

| 依赖项 | 安装方式 | 说明 |
|--------|---------|------|
| `jsonpath-ng>=1.6` | `uv add jsonpath-ng` (backend/agent-service) | `json_extract` 策略的 JSONPath 解析器 |

---

## 7. 验证计划

### 7.1 单元测试

```bash
# 测试 json_extract 策略路由
uv run pytest backend/agent-service/tests/ -k "json_extract" -v

# 测试 python 纠错 stderr 重写
uv run pytest backend/agent-service/tests/ -k "command_not_found" -v
```

### 7.2 集成验证

1. **Phase 1 验证**：通过新建一个磁盘告警工单，观察 Agent 是否在使用 `acli storage asan disk list` 时主动添加 jq 过滤
2. **Phase 2 验证**：强制让 Agent 生成含 python3 的命令，检查 stderr 是否包含 "⚠️ 自纠错提示"
3. **Phase 3 验证**：在更新 SOP 变量 schema 后，通过会话日志确认 `skill_call:hci-alert-parsing` 策略执行成功（搜索 `sop_request_variable_skill_executed` 日志事件）

### 7.3 效果评估指标

| 指标 | 改造前（Q2026061321687） | 改造后目标 |
|-----|----------------------|---------|
| 磁盘信息获取总步数 | 15 步 | ≤5 步 |
| Python 命令失败重试次数 | 2 次（约 800ms） | 0 次 |
| 多主机 grep 数据污染次数 | 5 次 | 0 次 |
| `node_ip` 技能调用成功率 | 0%（策略名错误） | 100% |

---

## 8. 未解决问题与后续探索

1. **`jq` 的可用性保障**：需要确认 HCI 物理宿主机（容器 host 模式）是否安装了 `jq`。如果没有，`jq` 方案需要退化为 `awk` 或多行 `grep` 复合过滤，需要在工具描述中额外说明兜底方案。

2. **JSONPath expression 的变量动态替换**：当 `expression` 中使用 `{node_hostname}` 占位符时，需要变量池在调用前已解析到 `node_hostname`（节点主机名）。当前架构中 `node_ip` ≠ `node_hostname`，需要额外映射或在 SOP 中声明 `node_hostname` 变量。

3. **`acli storage asan disk list` 的 JSON 结构确认**：`$.data.disks[...]` 路径需要根据实际 HCI 版本 API 输出进行验证。建议在 SOP 文档中内联一段实际输出样例作为 JSONPath 编写参考。

4. **skill_call 的 DynamicSkillRunner 注入**：[engine.py#L600-L650](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/memory/variable_pool/engine.py#L600-L650) 中的 `skill_call` 路由依赖 `skill_runner` 注入，需要确认 `hci-alert-parsing` 技能的 `DynamicSkillRunner` 在实际运行时已正确注入，否则会返回 `sop_dynamic_skill_runner_missing` 错误。
