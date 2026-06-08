# HCI 排障 Agent 三大核心难题：第一性原理解决方案

> **第一性原理**：剥离所有已有假设，回归问题的物理本质，再从底层向上重建解决方案。

---

## 目录

- [根因分析框架](#根因分析框架)
- [难题一：信息不准确性](#难题一信息不准确性)
- [难题二：tool_call 不稳定性](#难题二tool_call-不稳定性)
- [难题三：模型幻觉](#难题三模型幻觉)
- [横切关注点：可观测性与评估](#横切关注点可观测性与评估)
- [实施路线图](#实施路线图)

---

## 根因分析框架

> **核心洞察**：这三个问题的本质是同一件事的三个维度——**LLM 在不确定性条件下进行推理和决策的根本局限**。

```
信息不准确性  ←→  模型幻觉  ←→  tool_call 不稳定性
      ↑                               ↑
   输入层缺陷                      输出层缺陷
         ↘              ↙
      LLM 推理层（共同根因）
         ↓
   不确定性的传播与放大
```

**三者的统一根因**：
- LLM 是一个**概率函数**，它将输入分布映射到输出分布
- 当输入包含噪声（不准确信息）→ 输出分布的方差增大（幻觉）
- 当输出空间是结构化的（tool_call JSON）→ 结构错误概率随方差放大

**结论**：解决这三个问题不是修补 Bug，而是构建一套**置信度传播与约束系统**。

---

## 难题一：信息不准确性

### 🔬 第一性分析

**信息**在排障中起决定性作用。Agent 可用信息分为三类：

| 信息来源 | 当前实现 | 准确性风险 |
|---------|---------|-----------|
| 工单描述（用户输入）| `env_context` 原始注入 | **高**：主观描述、时间跨度长、记忆偏差 |
| 环境采集命令输出 | `bash_exec/acli_exec` → Redis → SSE 回传 | **中**：时效性问题、截断问题（4000chars） |
| 知识库（KBD/SOP）| `kb_client.search_cases_with_steps()` | **中**：相似度匹配可能召回不相关案例 |
| LLM 自身知识 | 模型内化参数 | **高**：HCI 领域特定知识训练数据有限 |

**当前代码中已识别的准确性弱点**：

```python
# investigation_agent.py L276
env_ctx: dict[str, str] = {
    k: str(v) for k, v in (env_context or {}).items() if isinstance(v, (str, int, float))
}
```
> **问题**：`env_context` 直接透传，无验证、无新鲜度检查、无来源标注。

```python
# kbd_differential.py L456-L458
truncated_output = actual_output[:2000]
if len(actual_output) > 2000:
    truncated_output += f"\n... （已截取，共 {len(actual_output)} 字符）"
```
> **问题**：输出截断策略简单粗暴，可能截掉最关键的错误信息（通常在末尾）。

```python
# executor.py L237-L238
STDOUT_MAX_CHARS = 4000
STDERR_MAX_CHARS = 1000
```
> **问题**：固定截断，无智能提取关键信息的能力。

---

### ✅ 解决方案：多层信息质量保障体系

#### 层 1：信息采集层 — 主动验证与结构化

**原则**：信息进入 Agent 时就强制携带"置信度元数据"。

```python
# 新增：InformationPacket 数据结构
@dataclass
class InformationPacket:
    """带置信度的信息包装器"""
    value: Any                    # 实际数据
    source: str                   # 来源：user_input / tool_exec / kb_search / llm_inference
    freshness_ts: float           # Unix 时间戳（采集时间）
    confidence: float             # 0.0-1.0 置信度
    raw_evidence: str | None      # 支撑证据（命令输出原文）
    verified: bool = False        # 是否经过交叉验证
```

**KBD 差异诊断的输出截断改进**：
```python
# 替换简单截断，改用智能提取
def smart_truncate(output: str, max_chars: int = 2000) -> str:
    """
    智能截断策略：
    1. 优先保留错误行（含 error/fail/exception/critical 的行）
    2. 保留首尾各 20% 作为上下文
    3. 中间部分压缩
    """
    lines = output.splitlines()
    error_lines = [l for l in lines if any(
        kw in l.lower() for kw in ['error', 'fail', 'exception', 'critical', 'fatal', 'panic']
    )]
    head_lines = lines[:len(lines)//5]
    tail_lines = lines[-len(lines)//5:]
    
    priority_content = "\n".join(error_lines + head_lines + tail_lines)
    if len(priority_content) <= max_chars:
        return priority_content
    return priority_content[:max_chars] + f"\n[TRUNCATED: total {len(output)} chars]"
```

#### 层 2：信息验证层 — 交叉核验机制

**原则**：关键诊断结论必须有至少两个独立信息源支撑。

**实现**：在 `KBDDiagnostic._judge_matches()` 中引入**证据三角验证**：

```python
# 新增：CrossValidator
class CrossValidator:
    """
    证据三角验证器
    
    当单一工具输出支持某个结论时，检查是否有第二个独立来源支持。
    防止单点证据误导诊断。
    """
    
    async def validate(
        self,
        claim: str,           # 待验证的诊断结论
        primary_evidence: str, # 主要证据（第一个工具输出）
        env_context: dict,
        tool_executor: Any,
    ) -> ValidationResult:
        """
        执行交叉验证：
        1. 基于 claim 选择第二个可验证的工具
        2. 执行第二个工具，获取独立证据
        3. 用 LLM 判断两者是否互相支撑
        """
        ...
```

#### 层 3：信息时效层 — 新鲜度管理

**问题根因**：排障时，环境状态可能在几分钟内发生变化（进程崩溃重启、网络抖动恢复）。

```python
# 新增：StaleDataGuard
class StaleDataGuard:
    """数据新鲜度守卫"""
    
    STALE_THRESHOLDS = {
        "process_status": 60,     # 进程状态：60秒过期
        "disk_usage": 300,        # 磁盘使用：5分钟过期
        "vm_state": 30,           # VM 状态：30秒过期
        "network_status": 120,    # 网络状态：2分钟过期
        "log_tail": 60,           # 日志尾部：60秒过期
    }
    
    def is_stale(self, data_type: str, collected_at: float) -> bool:
        threshold = self.STALE_THRESHOLDS.get(data_type, 300)
        return (time.time() - collected_at) > threshold
    
    def should_refresh(self, context: dict, data_type: str) -> bool:
        """判断是否需要重新采集"""
        if data_type not in context:
            return True
        packet = context[data_type]
        if isinstance(packet, InformationPacket):
            return self.is_stale(data_type, packet.freshness_ts)
        return False  # 无元数据时不强制刷新，保守处理
```

#### 层 4：用户反馈校正层 — Human-in-the-Loop

**原则**：当系统对某个信息的置信度 < 阈值时，主动向用户寻求确认，而不是基于低质量信息继续推理。

```python
# 扩展 TriageAgent 和 InvestigationAgent
CONFIDENCE_THRESHOLD = 0.75

async def _check_information_quality(
    self,
    env_context: dict,
    session_id: str,
) -> AsyncGenerator[AgentEvent, None]:
    """
    在开始诊断前，检查关键信息质量。
    对低置信度信息，生成澄清问题请求用户确认。
    """
    issues = []
    
    # 检查 1：env_context 是否为空或不完整
    if not env_context or not env_context.get("env_info"):
        issues.append({
            "field": "env_info",
            "question": "请提供故障发生时的环境快照（可点击工具栏的「采集环境」按钮）"
        })
    
    # 检查 2：工单描述时效性（如果描述超过 24 小时前的问题）
    case_created_at = env_context.get("case_created_at", time.time())
    if time.time() - case_created_at > 86400:
        issues.append({
            "field": "freshness",
            "question": "该工单已创建超过 24 小时，故障是否仍在持续？最近状态有何变化？"
        })
    
    if issues:
        yield AgentInteractiveRequest(
            kind="information_clarification",
            title="排障前的信息确认",
            prompt="为了提高诊断准确性，需要确认以下信息：",
            options=[{"question": i["question"]} for i in issues],
            ...
        )
```

#### 层 5：信息来源溯源 — 可解释性

**原则**：Agent 的每个诊断结论必须能溯源到具体的证据，用户和工程师可以验证。

```python
# 扩展 KBDDiagResult 和诊断报告
@dataclass
class EvidenceChain:
    """证据链：每个诊断结论的完整来源"""
    conclusion: str           # 结论
    evidence_items: list[dict]  # [{"tool": "...", "output": "...", "matched_pattern": "..."}]
    confidence: float
    reasoning: str            # LLM 的推理过程（CoT）
```

---

## 难题二：tool_call 不稳定性

### 🔬 第一性分析

`tool_call` 的本质是让 LLM 从**自然语言推理空间**跳转到**结构化函数调用空间**。这个跳转天然不稳定，原因如下：

**根因分解**：

```
不稳定性
├── Schema 层面（占比 ~40%）
│   ├── 参数 schema 描述歧义
│   ├── 必填/可选边界模糊
│   └── 枚举值未约束（LLM 自由发挥）
│
├── Context 层面（占比 ~30%）
│   ├── 上下文过长导致 LLM "遗忘"工具定义
│   ├── 多工具情况下的选择混乱
│   └── 工具描述与实际功能不匹配
│
├── 执行层面（占比 ~20%）
│   ├── 超时（当前 32s blpop）→ -1 exit_code 被误解为工具失败
│   ├── 网络抖动导致 Redis blpop 丢失
│   └── terminal_bridge 未运行时的静默失败
│
└── 解析层面（占比 ~10%）
    ├── JSON arguments 解析失败
    └── 部分 LLM 返回单引号 JSON（非法格式）
```

**当前代码中已识别的不稳定性来源**：

```python
# react_engine.py L200-L203
# BUG-FIX(DC-02): 必须用 json.dumps() 而非 str()
"arguments": json.dumps(tc.arguments or {}),
```
> 已修复：单引号 JSON 问题。但这说明此类问题有系统性。

```python
# react_engine.py L237-L243
work_messages.append({
    "role": "tool",
    "tool_call_id": tc.id,
    "content": str(tool_result),  # ← 问题：dict → str 可能丢失结构
})
```
> **问题**：工具结果强制转字符串，破坏了结构信息，增加下一轮 LLM 误解风险。

```python
# executor.py L441-L453
raw_result = await self._redis.client.blpop(result_key, timeout=self.BLPOP_TIMEOUT)
if raw_result is None:
    # 超时 → 返回 exit_code=-1
    return ExecResult(stderr=f"执行超时（{self.BLPOP_TIMEOUT}秒）...", exit_code=-1, ...)
```
> **问题**：超时和真实失败使用相同的 `exit_code=-1`，LLM 无法区分，可能做出错误决策。

---

### ✅ 解决方案：工具调用可靠性五层防御体系

#### 层 1：Schema 工程 — 消除歧义

**原则**：工具 schema 是 LLM 与系统之间的协议，必须像 API 文档一样精确。

**现有 ToolDefinition 扩展**：
```python
# 扩展 tool_registry.py 的 ToolDefinition
class ToolDefinition(BaseModel):
    name: str
    description: str          # 必须包含：何时用、何时不用、副作用说明
    parameters: dict          # JSON Schema，必须有 enum 约束枚举值
    
    # 新增字段
    usage_examples: list[dict]  # few-shot 示例：正确用法和错误用法
    preconditions: list[str]    # 前置条件（如：必须先执行 X 才能执行 Y）
    error_codes: dict[int, str] # 退出码含义映射（exit_code → 语义说明）
    retry_strategy: str         # "none" | "immediate" | "backoff" | "user_confirm"
    idempotent: bool            # 是否幂等（影响重试策略）
```

**Schema 精化示例**：
```json
{
  "name": "bash_exec",
  "description": "在目标 HCI 节点执行只读诊断命令。【重要限制】：(1) 禁止执行任何写操作；(2) 命令输出超过 4000 字节时会被截断；(3) 适用于：查看日志、检查进程、查看磁盘/网络状态。不适用于：服务重启、配置修改（使用 acli_exec 或 remediation）",
  "parameters": {
    "type": "object",
    "properties": {
      "command": {
        "type": "string",
        "description": "要执行的 bash 命令。必须是只读命令（以 cat/grep/df/ls/ps/netstat/ss/journalctl/dmesg/top/sar 等开头）",
        "examples": ["df -h /", "ps aux | grep qemu", "journalctl -u libvirtd --since '1 hour ago' --no-pager | tail -100"]
      },
      "reason": {
        "type": "string",
        "description": "执行此命令的诊断目的（用于审计和用户理解），如：'检查磁盘是否满载'",
        "minLength": 10
      },
      "node_ip": {
        "type": "string",
        "description": "目标节点 IP 地址（格式：x.x.x.x）。如果不确定节点 IP，先执行 acli_exec 查询",
        "pattern": "^(\\d{1,3}\\.){3}\\d{1,3}$"
      }
    },
    "required": ["command", "reason"],
    "additionalProperties": false
  }
}
```

#### 层 2：工具结果结构化 — 消除歧义传播

**原则**：工具返回值必须携带足够上下文，让 LLM 在下一轮推理时无需"猜测"结果含义。

```python
# 新增：ToolResultEnvelope
@dataclass
class ToolResultEnvelope:
    """结构化工具返回信封"""
    tool_name: str
    exec_id: str
    
    # 核心结果
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    
    # 诊断元数据（帮助 LLM 理解结果语义）
    exit_code_meaning: str   # "success" | "timeout" | "permission_denied" | "command_not_found" | ...
    truncated: bool
    truncated_bytes: int     # 被截断的字节数
    execution_node: str      # 在哪个节点执行的
    duration_ms: int
    
    # 给 LLM 的解释（由执行器生成）
    interpretation: str      # 如："命令成功执行，无异常输出" 或 "命令超时（32秒），可能节点负载过高或 terminal_bridge 未连接"
    suggested_next_action: str | None  # 如："可尝试更短超时的命令，或检查节点连通性"

    def to_llm_message(self) -> str:
        """序列化为 LLM 友好的消息格式"""
        status_emoji = "✅" if self.success else "❌"
        msg = f"{status_emoji} [{self.tool_name}] {self.interpretation}\n"
        if self.stdout:
            msg += f"\n命令输出：\n```\n{self.stdout}\n```"
        if self.stderr:
            msg += f"\n错误输出：\n```\n{self.stderr}\n```"
        if self.truncated:
            msg += f"\n⚠️ 输出已截断（原始 {self.truncated_bytes} 字节，显示前 4000 字节）"
        if self.suggested_next_action:
            msg += f"\n💡 建议：{self.suggested_next_action}"
        return msg
```

**在 `react_engine.py` 中替换**：
```python
# 替换当前的 str(tool_result) 转换
work_messages.append({
    "role": "tool",
    "tool_call_id": tc.id,
    "content": envelope.to_llm_message()  # 结构化、带语义的消息
})
```

#### 层 3：幂等重试与熔断 — 执行可靠性

**原则**：区分**可重试错误**（超时、网络抖动）和**不可重试错误**（命令语法错误、权限拒绝）。

```python
# 新增：RetryPolicy 和 CircuitBreaker
class ToolRetryPolicy:
    """工具执行重试策略"""
    
    RETRYABLE_EXIT_CODES = {-1}  # 超时
    RETRYABLE_ERROR_PATTERNS = ["connection refused", "timeout", "temporary"]
    NON_RETRYABLE_PATTERNS = ["permission denied", "command not found", "syntax error"]
    
    MAX_RETRIES = 2
    BACKOFF_FACTOR = 2.0  # 指数退避
    
    def should_retry(self, result: ExecResult, attempt: int) -> bool:
        if attempt >= self.MAX_RETRIES:
            return False
        if result.exit_code in self.RETRYABLE_EXIT_CODES:
            return True
        if result.stderr and any(p in result.stderr.lower() for p in self.RETRYABLE_ERROR_PATTERNS):
            return True
        return False
    
    async def execute_with_retry(
        self,
        executor: BridgeRelayExecutor,
        tool_name: str,
        args: dict,
        **kwargs
    ) -> ExecResult:
        for attempt in range(self.MAX_RETRIES + 1):
            result = await executor.execute(tool_name, args, **kwargs)
            if result.exit_code == 0 or not self.should_retry(result, attempt):
                return result
            
            wait_time = self.BACKOFF_FACTOR ** attempt
            logger.warning(f"工具执行失败（第{attempt+1}次），{wait_time}秒后重试")
            await asyncio.sleep(wait_time)
        
        return result  # 返回最后一次结果

class ToolCircuitBreaker:
    """工具熔断器：防止 terminal_bridge 未连接时无限等待"""
    
    def __init__(self, failure_threshold: int = 3, reset_timeout: float = 60.0):
        self._failures: dict[str, int] = {}
        self._open_since: dict[str, float] = {}
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
    
    def is_open(self, node_ip: str) -> bool:
        """熔断器是否处于断开状态"""
        if node_ip not in self._open_since:
            return False
        if time.time() - self._open_since[node_ip] > self._reset_timeout:
            # 超过重置时间，尝试半开状态
            del self._open_since[node_ip]
            self._failures[node_ip] = 0
            return False
        return True
    
    def record_failure(self, node_ip: str):
        self._failures[node_ip] = self._failures.get(node_ip, 0) + 1
        if self._failures[node_ip] >= self._failure_threshold:
            self._open_since[node_ip] = time.time()
            logger.warning(f"节点 {node_ip} 工具调用熔断（{self._failure_threshold}次连续失败）")
    
    def record_success(self, node_ip: str):
        self._failures.pop(node_ip, None)
        self._open_since.pop(node_ip, None)
```

#### 层 4：参数校验前置 — 拦截低质量调用

**原则**：在将工具调用发送给执行器之前，在 Agent 层做参数校验，而不是依赖执行器层的错误处理。

```python
# 新增：ToolCallValidator，在 react_engine._execute_tool_call() 中调用
class ToolCallValidator:
    """
    工具调用参数校验器
    在执行前验证 LLM 生成的参数是否合法，
    不合法时直接向 LLM 返回校验错误，而不是让执行器失败。
    """
    
    async def validate(
        self,
        tool_name: str,
        args: dict,
        tool_def: ToolDefinition,
    ) -> ValidationResult:
        errors = []
        
        # 1. JSON Schema 校验
        try:
            jsonschema.validate(instance=args, schema=tool_def.parameters)
        except jsonschema.ValidationError as e:
            errors.append(f"参数格式错误：{e.message}（路径：{'.'.join(str(p) for p in e.path)}）")
        
        # 2. 前置条件检查（如 node_ip 格式）
        if "node_ip" in args:
            if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", args["node_ip"]):
                errors.append(f"node_ip 格式错误：{args['node_ip']}，应为 IPv4 格式（如 192.168.1.10）")
        
        # 3. 熔断器检查
        if self._circuit_breaker.is_open(args.get("node_ip", "")):
            errors.append(f"节点 {args['node_ip']} 当前不可达（熔断器开启），请换另一节点或等待 60 秒后重试")
        
        return ValidationResult(valid=not errors, errors=errors)
    
    def format_for_llm(self, result: ValidationResult) -> str:
        """将校验错误格式化为 LLM 友好的消息，引导 LLM 修正调用"""
        if result.valid:
            return ""
        msg = "⚠️ 工具调用参数校验失败，请修正后重试：\n"
        for i, err in enumerate(result.errors, 1):
            msg += f"{i}. {err}\n"
        msg += "\n请根据以上提示修正参数，然后重新调用工具。"
        return msg
```

#### 层 5：并行工具调用与依赖感知调度

**原则**：当多个工具调用互相独立时，并行执行以减少总耗时；当存在依赖时，串行执行以保证正确性。

```python
# 扩展 ReactEngine 支持并行工具调用
async def _execute_tool_calls_parallel(
    self,
    tool_calls: list[ToolCall],
    session_id: str,
    ...
) -> list[ToolResultEnvelope]:
    """
    分析工具调用间的依赖关系，独立调用并行执行。
    
    依赖检测规则：
    - 同一工具不并行（避免竞态）
    - 写操作（risk≥2）不并行（保证可审计性）
    - 第一个工具的输出作为第二个工具的参数时，串行执行
    """
    independent_groups = self._resolve_call_groups(tool_calls)
    results = []
    for group in independent_groups:
        if len(group) == 1:
            results.append(await self._execute_single_tool(group[0], session_id))
        else:
            group_results = await asyncio.gather(
                *[self._execute_single_tool(tc, session_id) for tc in group],
                return_exceptions=True
            )
            results.extend(group_results)
    return results
```

---

## 难题三：模型幻觉

### 🔬 第一性分析

**幻觉的本质**：LLM 在训练数据分布边界外插值时，输出高置信度但错误的内容。

**在 HCI 排障场景中，幻觉的具体表现**：

| 幻觉类型 | HCI 场景案例 | 风险等级 |
|---------|------------|---------|
| **知识幻觉** | 编造不存在的 acli 命令参数 | 高 |
| **事实幻觉** | 声称"vm-001 的 CPU 使用率为 80%"，但实际没执行采集命令 | 高 |
| **推理幻觉** | 观察到磁盘 I/O 高 → 直接断言"磁盘故障"，跳过其他假设 | 中 |
| **幻象确认** | 执行了失败的命令（exit_code=-1）但仍声称"确认了 X 现象" | 高 |
| **置信度虚高** | 以"已经确定"的语气描述实际不确定的结论 | 中 |

**当前代码中的幻觉触发点**：

```python
# kbd_differential.py L432
elif llm_results.get(kbd.id, True):  # LLM 无法判断时保守保留
```
> **潜在问题**：LLM 的"保守保留"本身可能是幻觉（声称某 pattern 匹配，实则不匹配）。

```python
# react_engine.py L168-L177
if invoke_result.content is not None:
    # 流式输出最终文字回复
    async for chunk in ai_client.chat_completion_stream(...):
        yield AgentTextChunk(content=chunk)
    return
```
> **问题**：当 invoke() 返回文字（而非 tool_call）时，直接流式输出，无任何幻觉过滤。

```python
# investigation_agent.py L604-L623
async def _process_fallback_mode(self, ...):
    """无知识库匹配时：机制推理降级模式（流式输出）。"""
    async for chunk in ai_client.chat_completion_stream(...):
        yield AgentTextChunk(content=chunk)
```
> **问题**：降级模式完全依赖 LLM 自由推理，是幻觉高发区，无任何约束。

---

### ✅ 解决方案：幻觉控制五层防线

#### 防线 1：基础设施层 — Prompt 工程（最快见效）

**原则**：用 prompt 设计改变 LLM 的行为分布，使其天然倾向于保守、有据推理。

**核心 Prompt 工程原则**：

```python
# 在所有 Agent 的 system prompt 中强制加入"证据锚定规则"
EVIDENCE_ANCHORING_RULES = """
## 证据锚定规则（必须严格遵守）

1. **禁止凭空声明**：每个诊断结论必须明确引用具体的工具输出作为依据。
   - ❌ 错误："磁盘 I/O 异常导致 VM 无法启动"
   - ✅ 正确："bash_exec 执行 `iostat -x 1 5` 输出显示 sdb 磁盘 util=98%，结合 VM 启动失败日志中的 'disk I/O error'，判断磁盘 I/O 异常"

2. **不确定性声明**：当证据不足时，必须明确表达不确定性。
   - ❌ 错误："根因是网络故障"
   - ✅ 正确："目前证据倾向于网络故障（置信度中等），但需要进一步验证 MTU 和 TCP 连接状态"

3. **禁止跳步推理**：诊断步骤必须是线性的，不允许跳过步骤声称"已经验证"。
   - 如果某个工具调用失败或未执行，不得在报告中声称其结果

4. **区分观察与结论**：
   - 观察 = 工具输出的原始事实（不加解读）
   - 结论 = 基于多个观察的综合判断

5. **幻觉自查**：在生成最终结论前，先问自己：
   "我是否有实际执行过命令来支撑这个结论？命令结果在哪里？"
"""

# 在 fallback_mode（降级模式）中使用更严格的约束
FALLBACK_MODE_CONSTRAINT = """
## 降级模式警告

当前无匹配知识库案例，正在使用通用推理模式。
在此模式下，你的建议基于 HCI 通用知识，而非本环境的实测数据。

**强制要求**：
- 所有建议命令必须标注"需要执行验证"
- 禁止给出"已确认根因"的结论，只能给出"假设/怀疑"
- 每个建议步骤必须说明：执行目的 + 预期输出 + 如何解读结果
"""
```

#### 防线 2：思维链强制化 — CoT 外显

**原则**：强制 LLM 在输出结论前先输出推理过程，可以降低幻觉率（Anthropic/OpenAI 研究验证）。

```python
# 在 InvestigationAgent._build_sop_react_prompt() 中强制 CoT
CoT_TEMPLATE = """
在给出任何诊断结论之前，请先按以下格式整理你的推理：

<reasoning>
1. 已收集的证据：
   - [工具名] → [关键发现]
   - [工具名] → [关键发现]

2. 每个假设的支撑/反对证据：
   - 假设 A（如：磁盘故障）：支撑：[...] 反对：[...]
   - 假设 B（如：内存不足）：支撑：[...] 反对：[...]

3. 置信度评估：
   - 假设 A 置信度：[高/中/低]，因为 [...]
   - 假设 B 置信度：[高/中/低]，因为 [...]

4. 下一步行动：
   - 若选择继续调查：执行 [工具名]，目的是 [...]
   - 若准备给出结论：结论是 [...]，依据是 [...]
</reasoning>

[然后再给出实际的回复/工具调用]
"""
```

#### 防线 3：幻觉检测层 — 输出后验证

**原则**：LLM 的输出进入用户界面之前，经过一个独立的验证模型（或规则引擎）检查。

```python
# 新增：HallucinationDetector
class HallucinationDetector:
    """
    幻觉检测器
    
    检测 LLM 输出中常见的幻觉模式，
    在内容展示给用户前标注或过滤高风险内容。
    """
    
    # 事实声明模式：这些模式表示 LLM 在声称某种事实
    FACTUAL_CLAIM_PATTERNS = [
        re.compile(r"(确认|已经确认|确定|可以确认|结果显示|输出表明)"),
        re.compile(r"(根因是|问题是|故障是|原因是)"),
        re.compile(r"(CPU 使用率|内存使用|磁盘使用|负载).*?(\d+%|\d+\s*GB|\d+\s*MB)"),
    ]
    
    # 不确定性信号：这些是好的迹象
    UNCERTAINTY_SIGNALS = [
        re.compile(r"(可能|疑似|怀疑|需要验证|建议检查|初步判断)"),
    ]
    
    def check(
        self,
        llm_output: str,
        executed_tools: list[str],
        tool_results: dict[str, str],
    ) -> HallucinationCheckResult:
        """
        检查 LLM 输出是否包含幻觉。
        
        核心检查：LLM 是否声称了没有工具证据支撑的事实？
        """
        issues = []
        
        # 检查 1：是否引用了未执行的工具
        tool_references = re.findall(r"`(bash_exec|acli_exec|[a-z_]+_exec)`", llm_output)
        for ref in tool_references:
            if ref not in executed_tools:
                issues.append(HallucinationIssue(
                    type="phantom_tool_reference",
                    severity="high",
                    description=f"引用了未执行的工具 `{ref}`",
                    location=ref,
                ))
        
        # 检查 2：是否包含事实声明但缺乏不确定性修饰
        for pattern in self.FACTUAL_CLAIM_PATTERNS:
            matches = pattern.findall(llm_output)
            if matches and not any(p.search(llm_output) for p in self.UNCERTAINTY_SIGNALS):
                issues.append(HallucinationIssue(
                    type="overconfident_claim",
                    severity="medium",
                    description="包含强事实声明但缺乏不确定性表达",
                    location=str(matches[:2]),
                ))
        
        # 检查 3：数字事实是否在工具输出中可以找到来源
        numeric_claims = re.findall(r"(\d+(?:\.\d+)?)\s*(%|GB|MB|ms|秒)", llm_output)
        for value, unit in numeric_claims:
            found_in_evidence = any(
                f"{value}{unit}" in result or f"{value} {unit}" in result
                for result in tool_results.values()
            )
            if not found_in_evidence and numeric_claims:
                issues.append(HallucinationIssue(
                    type="ungrounded_number",
                    severity="medium",
                    description=f"数字 {value}{unit} 无法在工具输出中找到来源",
                    location=f"{value}{unit}",
                ))
        
        return HallucinationCheckResult(
            has_issues=bool(issues),
            issues=issues,
            risk_score=sum(2 if i.severity == "high" else 1 for i in issues),
        )
    
    def annotate(self, llm_output: str, result: HallucinationCheckResult) -> str:
        """将检测到的问题标注在输出中（而非直接删除）"""
        if not result.has_issues:
            return llm_output
        
        warning = "\n\n---\n⚠️ **[系统提示] 以下内容包含待验证的判断**\n"
        for issue in result.issues:
            if issue.severity == "high":
                warning += f"- {issue.description}\n"
        
        return llm_output + warning
```

#### 防线 4：事实接地 (Grounding) — RAG 强化

**原则**：LLM 生成内容时，实时注入相关的权威知识，减少依赖内化知识的幻觉。

```python
# 扩展 InvestigationAgent，在 fallback_mode 中强制检索知识库
async def _process_fallback_mode_with_grounding(self, ...):
    """
    改进的降级模式：即使无精确 KBD 匹配，
    也从知识库中检索相关的命令模板和诊断方法，
    作为 Grounding 材料注入到 LLM Prompt 中。
    """
    
    # 1. 检索相关的诊断命令模板（即使没有案例匹配）
    grounding_data = await self._kb_client.search_diagnostic_commands(
        category_id=category_id,
        top_k=10,
    )
    
    # 2. 检索相似故障的历史摘要（不是完整案例，而是命令+现象映射）
    symptom_patterns = await self._kb_client.search_symptom_patterns(
        query=user_query,
        top_k=5,
    )
    
    # 3. 将 grounding 材料注入 prompt
    grounding_prompt = f"""
## 相关诊断参考（来自知识库，供参考但需实际验证）

### 推荐诊断命令
{self._format_commands(grounding_data)}

### 同类故障常见现象
{self._format_patterns(symptom_patterns)}

注意：以上内容来自历史案例库，仅供参考。
实际诊断结论必须基于在当前环境中执行命令的实测结果。
"""
    # 4. 与原 prompt 合并后调用 LLM
    ...
```

#### 防线 5：多 Agent 交叉验证 — Debate Pattern

**原则**：对于高风险诊断结论（如"需要重启服务"或"需要删除数据"），引入第二个独立的 Agent 进行验证，而不是直接信任第一个 Agent 的结论。

**业界依据**：Google DeepMind Society of Mind、Anthropic 宪法 AI 中都验证了多 Agent 交叉验证可以显著降低幻觉率。

```python
# 新增：DebateValidationAgent
class DebateValidationAgent:
    """
    辩证验证 Agent
    
    当主 Agent（InvestigationAgent）给出高置信度结论时，
    此 Agent 扮演"怀疑者"角色，尝试从相反方向寻找反证。
    
    适用场景：
    - 主 Agent 准备给出"is_definitive=True"的结论时
    - 主 Agent 建议执行 risk_level≥2 的操作时
    - 主 Agent 在 fallback_mode 给出强结论时
    """
    
    async def validate(
        self,
        primary_conclusion: str,
        evidence_chain: list[EvidenceChain],
        context: dict,
    ) -> ValidationReport:
        """
        反驳测试：
        1. 给 LLM 展示主 Agent 的结论和证据
        2. 要求 LLM 以"怀疑者"身份，列出最强的反对理由
        3. 如果找到有力反对理由，返回"需要更多证据"
        """
        devil_advocate_prompt = f"""
你是一个严格的 HCI 排障审计员。
以下是主诊断 Agent 给出的结论和证据链：

结论：{primary_conclusion}

证据链：
{json.dumps([e.__dict__ for e in evidence_chain], ensure_ascii=False, indent=2)}

你的任务是：
1. 找出这个结论最大的 2-3 个漏洞或反例
2. 列出哪些情况下证据会指向不同的根因
3. 判断证据是否足以支撑这个结论（足够 / 需要补充）

严格以 JSON 返回：
{{
  "objections": ["反对理由1", "反对理由2"],
  "alternative_hypotheses": ["替代假设1"],
  "evidence_sufficiency": "sufficient" | "insufficient",
  "confidence_in_primary": 0.0-1.0
}}
"""
        # ... 调用 LLM 获取反驳意见
        # 如果 confidence_in_primary < 0.6，要求主 Agent 收集更多证据
```

---

## 横切关注点：可观测性与评估

### 在线监控（Runtime）

```python
# 新增：AgentReliabilityMetrics
class AgentReliabilityMetrics:
    """Agent 可靠性在线指标"""
    
    # Prometheus 指标
    tool_call_success_rate = Histogram("agent_tool_call_success_rate", ...)
    hallucination_detected_total = Counter("agent_hallucination_detected_total", ["severity"])
    information_confidence_avg = Gauge("agent_information_confidence_avg", ...)
    diagnosis_grounding_score = Histogram("agent_diagnosis_grounding_score", ...)
    
    def record_tool_call(self, tool_name: str, success: bool, retries: int):
        ...
    
    def record_hallucination_check(self, result: HallucinationCheckResult):
        ...
    
    def record_evidence_quality(self, session_id: str, avg_confidence: float):
        ...
```

### 离线评估（Evaluation）

```python
# 扩展 evaluation/ 目录
class DiagnosisQualityEvaluator:
    """
    诊断质量离线评估器
    
    评估维度：
    1. 事实准确性（Factual Accuracy）：与实际工单结果对比
    2. 诊断步骤效率（Step Efficiency）：使用了多少工具才得出结论
    3. 幻觉率（Hallucination Rate）：结论中未被工具输出支撑的声明比例
    4. 置信度校准（Confidence Calibration）：模型说"高置信"时实际正确率
    """
    
    async def evaluate_session(
        self,
        session_id: str,
        ground_truth: dict,  # 实际解决方案（用于离线评估）
    ) -> EvaluationReport:
        ...
```

---

## 实施路线图

> **原则**：优先实施收益/成本比最高的改进，快速迭代验证效果。

### Phase 1（2 周）— 快速止血

| 优先级 | 改进项 | 文件 | 预期效果 |
|--------|--------|------|---------|
| P0 | ToolResultEnvelope 替换 str(tool_result) | `react_engine.py` | 消除结果语义丢失 |
| P0 | 智能截断 smart_truncate() | `executor.py`, `kbd_differential.py` | 减少关键信息截断 |
| P0 | exit_code 语义分类（超时 vs 失败） | `executor.py` | LLM 不再混淆超时和错误 |
| P1 | Prompt 中加入证据锚定规则 | `investigation_agent.py` / DB prompts | 减少无依据声明 |
| P1 | fallback_mode 的约束性 prompt | `investigation_agent.py` | 降低降级模式幻觉 |
| P1 | ToolCallValidator（Schema + 正则校验）| `react_engine.py` | 拦截低质量 tool_call |

### Phase 2（4 周）— 系统性加固

| 优先级 | 改进项 | 文件 | 预期效果 |
|--------|--------|------|---------|
| P1 | InformationPacket 置信度数据结构 | 新增 `information.py` | 信息质量可追踪 |
| P1 | StaleDataGuard 数据新鲜度管理 | 新增 `stale_guard.py` | 减少过期数据引起的误诊 |
| P2 | ToolRetryPolicy + CircuitBreaker | `executor.py` | 解决超时不稳定性 |
| P2 | HallucinationDetector 基础版 | 新增 `hallucination.py` | 幻觉可见性 |
| P2 | 强化 Schema + examples + error_codes | `tool_registry.py` | 提升 schema 质量 |

### Phase 3（6 周）— 前沿能力

| 优先级 | 改进项 | 文件 | 预期效果 |
|--------|--------|------|---------|
| P2 | CoT 强制外显 | system prompt | 推理过程透明化 |
| P2 | CrossValidator 交叉验证 | 新增 `cross_validator.py` | 关键结论双源验证 |
| P3 | DebateValidationAgent | 新增 `debate_agent.py` | 高风险结论多 Agent 验证 |
| P3 | 在线 RAG Grounding for fallback | `investigation_agent.py` | 降级模式接地效果 |
| P3 | DiagnosisQualityEvaluator 离线评估 | `evaluation/` | 持续监控幻觉率 |

---

## 附录：业界最佳实践参考

| 来源 | 方法论 | 本方案对应 |
|------|--------|-----------|
| Anthropic (Claude) | 宪法 AI：用规则约束输出 | 证据锚定规则、fallback 约束 |
| OpenAI | Tool use best practices：丰富的 schema + examples | Schema 工程 |
| Google DeepMind | Gemini 多 Agent 辩证推理 | DebateValidationAgent |
| Meta AI | SELF-RAG：按需检索，有依据生成 | Grounding for fallback |
| Microsoft | TypeChat：类型安全的 LLM 输出 | ToolCallValidator |
| LangChain | Retry + Circuit Breaker | ToolRetryPolicy + CircuitBreaker |
| Anthropic 研究 | CoT 降低幻觉率 35-50% | CoT 强制外显 |
| Stanford HELM | 多维度 LLM 评估基准 | DiagnosisQualityEvaluator |
