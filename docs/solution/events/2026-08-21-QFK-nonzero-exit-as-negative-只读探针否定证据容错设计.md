# QFK 只读探针否定证据容错机制设计

> **工单关联**：Q2026082095867
> **问题发现时间**：2026-08-21
> **归档时间**：2026-08-21
> **影响范围**：agent-service / QFK 引擎 / KBD 知识库案例 30880

---

## 一、问题描述

### 现象

在排查工单 `Q2026082095867`（虚拟机开机失败，报错虚拟机镜像忙）时，出现如下诊断输出：

```
#### 2. ⚠️ 检查GPU配置文件中是否存在gpu_type字段
- **状态**：执行失败
- **检查结果**：QFK_COMMAND_FAILED: 命令退出码为 1，不执行判定或变量写入
```

该错误导致门禁（Conclusion Gate）死锁：**分类 `虚拟机-003` 下的候选案例 30880 标记为 ERROR（未决），使得已有所有必要证据的主案例 27123 无法输出根因和解决方案。**

### 根因（物理层面）

```
目标主机 172.28.25.4 是一台普通无 GPU 节点
   ↓
物理上不存在 /sf/cfg/gpu_info.ini 文件
   ↓
Linux POSIX 标准：cat 不存在的文件 → exit_code = 1
   ↓
QFK 引擎：exit_code != 0 → QFK_COMMAND_FAILED（执行系统故障）
   ↓
Conclusion Gate 门禁：候选集中存在 ERROR 状态 → 禁止输出任何根因
   ↓
最终：已有 3/3 必要证据的案例 27123 根因被封锁
```

---

## 二、第一性原理分析

### 硬性约束（不可否认的物理事实）

1. **POSIX 不变量**：`cat`/`grep`/`pgrep`/`ls` 等只读探针在目标对象不存在时天然返回 `exit 1`，这是操作系统规范，永远不会改变。
2. **语义层不同**：
   - `exit 1`（文件不存在）= **确定性否定证据（Negative Evidence）**：命令真实落到主机，执行完成，仅业务结论为"否"。
   - `exit 1`（SSH 会话不存在）= **系统执行故障**：命令根本未落到主机，结果无效。
3. **安全门禁不可拆除**：QFK 引擎的非零退出门禁的核心价值是防止"会话断开/桥未运行"的 stderr 内容被误送入 Matcher，引发 `match_mode="not"` 信号的假阳性命中。

### 架构断裂点

| 层级 | 问题 |
|------|------|
| **命令语义层** | KBD 编写者无法预知底层引擎会将"文件不存在"等价于"系统崩溃" |
| **引擎语义层** | `exit_code != 0` 一刀切判定，混淆了"否定证据"与"执行故障"两种不同语义 |
| **门禁层** | ERROR 状态级联传播，单个探针失败导致整个分类死锁 |

---

## 三、对抗性审查（方案比较）

### 方案 A：KBD 命令容错化（`|| true`）

```bash
cat /sf/cfg/gpu_info.ini 2>/dev/null || true
```

**被攻击点**：
- 需要 KBD 编写者对每个命令都知道要加 shell 容错语法（认知负担大）
- 历史案例难以批量修正（存量问题）
- 与 acli 封装接口有潜在冲突

### 方案 B：引擎全局宽容 exit 1

**被攻击点**：
- **破坏安全门禁**：真实命令执行崩溃（如 acli 本身权限不足返回 1）时，stderr 将被错误送入 Matcher
- 引发假阴性：`match_mode="not"` 信号将把"系统崩溃的空输出"误判为符合排查判定

### ✅ 方案 C（采用）：信号级声明 `nonzero_exit_as_negative`

**设计原则来源**：
- **Ansible `failed_when`**：允许任务级声明"什么条件才算失败"
- **Prometheus Blackbox Exporter `valid_status_codes`**：探针级声明合法状态码域
- **Nagios `check_command` 退出码语义**：OK/WARNING/CRITICAL/UNKNOWN 分层语义

**优势**：
- 零破坏原有安全门禁（二次检查哨兵）
- KBD 编写者只需加一个字段声明，无需改动 shell 命令本身
- 完全向后兼容（默认 `False`，旧行为不变）
- 可精确选择适用范围（仅只读探针）

---

## 四、实施方案

### 4.1 BackendSignal 新增字段

### 4.1 BackendSignal 新增字段与自动推导

**文件**：`backend/agent-service/app/tools/qfk/signal.py`

```python
POSIX_READONLY_PROBE_COMMANDS = frozenset(
    {"cat", "grep", "egrep", "pgrep", "test", "ls", "which", "find", "head", "tail"}
)

# 自动推导逻辑（Zero-Config 零配置）：
# 凡是纯只读命令（cat/grep/pgrep等）且为正向预期（expected=True），
# 默认自动开启 nonzero_exit_as_negative，使存量与增量 KBD 探针在对象不存在时
# 均能正确作为否定证据（matched=False）流转，无需人工逐条配置。
```

### 4.2 QFK 引擎容错路径（engine.py）

执行流程（`nonzero_exit_as_negative=True` 时）：

```
exit_code != 0
   ↓
[二次安全门] 检查 combined 是否包含 terminal_failure_sentinels
   ├─ 包含哨兵 → 仍然 QFK_COMMAND_FAILED（命令未落到主机）
   └─ 不含哨兵 → 记录 qfk_nonzero_as_negative 日志
                  继续进入 Matcher 流程
                  → 自然产出 matched=False（否定证据）
```

### 4.3 KBD 案例 30880 信号行为

- **默认零配置**：案例 30880 的 `cat /sf/cfg/gpu_info.ini` 信号自动被识别为只读探针，自动生效容错，无需修改存量 JSON。
- **显式覆盖（可选）**：若需显式声明，可在 JSON 中配置 `"nonzero_exit_as_negative": true`。

---

## 五、执行结果对比

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| 非 GPU 主机，文件不存在 | `QFK_COMMAND_FAILED` → 门禁死锁 → **27123 无法定论** | `matched=False` → 30880 排除 → **27123 输出根因** |
| GPU 主机，文件存在且含 `gpu_type` | 正常（exit=0） | 不变 |
| SSH 会话未建立 | `QFK_COMMAND_FAILED`（含哨兵） | **二次安全门保护**，行为不变 |
| 默认未声明（旧 KBD 信号） | `QFK_COMMAND_FAILED` | 完全向后兼容 |

---

## 六、测试覆盖

新增 3 个单元测试（`tests/unit/test_qfk.py`）：

| 测试名称 | 验证目标 |
|----------|----------|
| `test_nonzero_exit_as_negative_file_not_found_becomes_matched_false` | 核心：exit=1+文件不存在 → matched=False，无 error |
| `test_nonzero_exit_as_negative_with_terminal_sentinel_still_fails` | 安全门：哨兵存在时不被绕过 |
| `test_default_behavior_nonzero_exit_still_fails_without_flag` | 向后兼容：默认行为不变 |

---

## 七、使用规范

### ✅ 允许使用

| 命令 | 场景 |
|------|------|
| `cat <path>` | 文件可能不存在 |
| `grep <pattern> <file>` | 关键词可能不存在 |
| `pgrep <process>` | 进程可能不存在 |
| `ls <path>` | 目录/文件可能不存在 |
| `systemctl is-active <service>` | 服务可能未启动 |

### ❌ 严禁使用

| 命令 | 原因 |
|------|------|
| `rm`、`kill`、`sed -i` | 有副作用的写命令，exit 1 代表真实失败 |
| `acli vm start/stop` | 操作类命令，失败必须暴露 |

---

## 八、关联文档

- [KBD 主动诊断信号设计](../../solution/knowledge-base/KBD主动诊断信号设计.md)
- [命令确认执行下发 terminal_bridge 修复验证](./2026-08-21-Q2026082095867-命令确认执行下发terminal_bridge修复验证.md)
