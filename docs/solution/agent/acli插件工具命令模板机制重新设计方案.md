# ACLI 插件工具命令模板机制重新设计方案

> [!IMPORTANT]
> 本文档基于**第一性原理**和**业界最佳安全实践**，针对系统当前“插件工具 `usage_template` 完全未被底层代码消费，导致插件工具执行空命令”的缺陷，提出了完整的重构与重设计方案。

---

## 一、 背景与根因剖析

在超融合基础设施排障平台 (HCI Troubleshoot Platform) 的工具管理子系统中，工具的设计采用“声明与执行分离”的架构：
* **声明端**：在数据库中为插件工具（如 `acli_plugin_vm_start`）声明了 `usage_template`（例如 `acli plugins vm_start vm_start`）和相关的入参 Schema。
* **执行端**：底层代码通过 `BridgeRelayExecutor` 来执行由 LLM 输出的工具调用。

在当前 v2.0 的实现中，此契约链条存在如下**严重设计与工程漏洞**：
1. **数据模型截断**：内存中代表工具定义的 Pydantic 类 `ToolDefinition` （定义在 [base_tool.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/base_tool.py)）中完全没有定义 `usage_template` 字段。这使得启动加载时，数据库中声明的模板内容直接在反序列化阶段丢失。
2. **参数提取硬编码与命令虚空化**：在 `BridgeRelayExecutor.execute` 中，系统使用 `args.get("command", "")` 提取要执行的命令。但因为插件工具对外声明的 JSON Schema 里只有具体业务参数（例如 `node_ip`）而没有 `command` 字段，大模型在生成 Function Call 时不会也不应该传入 `command` 字段。这就导致底层实际提取到空串 `""`，最终往 HCI 节点发送空命令执行。

为了修复该漏洞，必须在系统底层设计并实现一套**安全、鲁棒的声明式命令模板插值引擎**。

---

## 二、 方案设计原则（第一性原理）

根据第一性原理，重新设计的核心原则包括：

1. **声明式契约完全闭环 (Declarative Contract Loop)**：
   - DB 中定义的 `usage_template` 必须在执行端自动加载并与 LLM 传入的 arguments 发生安全插值，将其转化为底层的实体 Bash 指令。
2. **防注入安全沙箱 (Injection-Proof Sandboxing)**：
   - 大模型传入的参数数据（例如 `vm_id`）属于外部不可信输入。在进行命令插值拼接时，必须防止利用拼接字符注入恶意 Shell 符号（如 `;`, `&&`, `|`）。
   - **防护策略**：在插值替换阶段，对每个参数值强制使用 `shlex.quote()` 进行 Shell 格式转义，并在插值拼装生成完整命令后，再次通过 `CommandSanitizer` 进行静态正则净化检查。
3. **错误即时暴露与强韧性 (Fail-Fast & Transparency)**：
   - 一旦插值参数缺失、类型不匹配，或者发现恶意参数注入，应立即显式抛出结构化异常，将具体错误链条返回至前端或诊断界面，严禁隐式退化为空命令或静默失败。

---

## 三、 业界最佳实践与架构对比

| 维度 | 传统拼接方式 | Langchain / Semantic Kernel 范式 | 本方案设计 |
| :--- | :--- | :--- | :--- |
| **插值机制** | 简单的 Python `f-string` 或 `replace` | 基于模板引擎（如 Jinja2）的动态渲染 | Python 强校验型 `TemplateFormatter` 插值 |
| **防命令注入** | 无，依赖外部正则 | 靠大模型约束，无底层硬防线 | **第一防线**：`shlex.quote()` 单变量 Shell 转义<br>**第二防线**：`CommandSanitizer` 全命令多维扫描 |
| **断言校验** | 运行至物理端报错，返回 500 | LLM 自我纠错（低效且浪费 Token） | **本地断言**：匹配 Schema 必填约束，参数缺失直接 Fail-Fast 阻断 |

---

## 四、 具体重构与代码设计方案

### 4.1 数据模型与注册表加载改造

#### 1. 扩容 Pydantic 实体类
在 [base_tool.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/base_tool.py) 的 `ToolDefinition` 中，新增 `usage_template` 字段，使其与 ORM 实体保持同步：

```python
class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict
    risk_level: int
    policy: str
    category: str
    usage_template: str | None = None  # [NEW] 插件工具命令模板（允许为空）
```

#### 2. 补齐 DB 加载器映射
在 [tool_registry.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/adapters/agents/htp/tool_registry.py) 中，将 ORM 对象的 `usage_template` 属性载入内存：

```python
async def load_tool_registry(db: AsyncSession) -> dict[str, ToolDefinition]:
    result = await db.execute(select(ToolDefinitionORM).where(ToolDefinitionORM.is_active.is_(True)))
    registry: dict[str, ToolDefinition] = {}
    for row in result.scalars():
        registry[row.tool_name] = ToolDefinition(
            name=row.tool_name,
            description=row.description,
            parameters=row.parameters_schema,
            risk_level=row.risk_level,
            policy=risk_to_policy(row.risk_level),
            category=row.category,
            usage_template=row.usage_template,  # [NEW] 映射底层模板
        )
    return registry
```

---

### 4.2 核心插值引擎设计 (`TemplateInterpolator`)

为了确保命令拼接的极度安全，新建一个模板安全插值类，其职责为解析模板、核对参数、自动做 Shell 转义并拼接：

```python
import shlex
import string
from typing import Any

class TemplateInterpolator:
    """ACLI 插件命令安全插值引擎"""

    @classmethod
    def interpolate(cls, template: str, args: dict[str, Any]) -> str:
        """
        根据传入参数和模板生成净化后的 Bash 命令行
        
        Args:
            template: 命令模板，如 "acli plugins vm_start vm_start --vm-id {vm_id}"
            args: 大模型传入的实参，如 {"vm_id": "test-123"}
            
        Raises:
            ValueError: 缺少必要参数或插值计算失败
        """
        if not template:
            return ""

        # 1. 解析模板中所有的占位符
        formatter = string.Formatter()
        placeholders = {field_name for _, field_name, _, _ in formatter.parse(template) if field_name is not None}
        
        # 2. 检查占位符的参数是否在 args 中提供（node_ip 通常用作控制路由，不作为占位符强制校验）
        safe_args = {}
        for placeholder in placeholders:
            if placeholder not in args:
                raise ValueError(f"命令模板插值失败：模板中要求的参数 '{placeholder}' 在 Function Call 参数中缺失")
            
            val = args[placeholder]
            # 对参数值进行严格防注入处理：强转 string 并通过 shlex.quote 进行 Shell 转义
            safe_args[placeholder] = shlex.quote(str(val))
            
        # 3. 渲染模板
        try:
            interpolated_command = template.format(**safe_args)
        except Exception as e:
            raise ValueError(f"格式化命令模板出错: {str(e)}")
            
        return interpolated_command.strip()
```

---

### 4.3 物理执行流的重构 (`BridgeRelayExecutor`)

在 [executor.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py) 中，`BridgeRelayExecutor.execute` 应改造为：优先消费 `usage_template`；如模板为空，则退化至从 `args.get("command")` 提取。这既确保了普通工具的兼容，又打通了插件工具的执行通路。

```python
# app/tools/acli/executor.py 中 BridgeRelayExecutor 的 execute 逻辑改造

async def execute(
    self,
    tool_name: str,
    args: dict,
    *,
    conversation_id: str,
    node_ip: str | None = None,
    risk_level: int | None = None,
    policy: str | None = None,
    usage_template: str | None = None,  # [NEW] 从上层传入模板参数
) -> ExecResult:
    trace_id = get_current_trace_id()
    start_time = time.time()
    exec_id = str(uuid.uuid4())

    # 1. 获取最终执行命令（新插值引擎）
    if usage_template:
        try:
            # 引入模板安全插值逻辑
            command = TemplateInterpolator.interpolate(usage_template, args)
        except ValueError as e:
            # 参数缺失或插值失败，直接 Fail-Fast，暴露详细报错
            logger.error(f"插件工具插值失败: {str(e)}", exc_info=True)
            return ExecResult(
                stdout="",
                stderr=f"[error] 参数校验与插值失败: {str(e)}",
                exit_code=-1,
                command=usage_template,
                node=node_ip or "unknown",
                duration_ms=0,
                truncated=False,
                risk_level=risk_level or 3,
            )
    else:
        # 退化路线：兼容普通的 bash_exec / acli_exec
        command = args.get("command", "")

    reason = args.get("reason", "未提供原因")

    # 2. 命令净化（第二道防线）
    try:
        cleaned_command = CommandSanitizer.sanitize(command, tool_name)
    except ValueError as e:
        logger.warning(f"命令净化被拦截: {str(e)}")
        return ExecResult(
            stdout="",
            stderr=f"[error] 命令被安全沙箱净化器拒绝: {str(e)}",
            exit_code=-1,
            command=command,
            node=node_ip or "unknown",
            duration_ms=0,
            truncated=False,
            risk_level=3,
        )
        
    # [后续执行逻辑保持不变：Redis push -> SSE -> blpop等待 -> 结果处理...]
```

相应的，[main.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/main.py) 中 `CompositeToolExecutor` 传递 `usage_template` 的逻辑为：

```python
# app/main.py L415 +
            result = await self._bridge_executor.execute(
                tool_name,
                args,
                conversation_id=effective_conversation_id,
                node_ip=args.get("node_ip"),
                risk_level=tool_def.risk_level,
                policy=tool_def.policy,
                usage_template=tool_def.usage_template,  # [NEW] 显式传出模板
            )
```

---

## 五、 防御设计：Schema 字段修改校验

为了防御管理员在 UI 界面上随意修改参数 Schema 造成的契约断裂，我们在系统层设计以下**防御校验规则**：

1. **热更与启动自检 (Registry Verification on Load)**：
   在 `load_tool_registry()` 或者是单独的测试套件中，遍历每一个拥有 `usage_template` 且已激活的工具，核对模板中的 `{placeholders}` 是否全部在对应的 `parameters_schema.properties` 键中声明。
   - **校验代码样例**：
     ```python
     def verify_tool_contract(tool: ToolDefinition) -> None:
         if not tool.usage_template:
             return
         formatter = string.Formatter()
         placeholders = {f for _, f, _, _ in formatter.parse(tool.usage_template) if f is not None}
         
         schema_properties = tool.parameters.get("properties", {})
         for p in placeholders:
             if p not in schema_properties:
                 raise ValueError(
                     f"工具契约损坏: {tool.name} 的命令模板中包含占位符 '{p}'，"
                     f"但在 Schema 参数定义中未找到对应属性。"
                 )
     ```
   - **行为规范**：如果后台服务在启动或处理热更新时检测到契约损坏（如 `ValueError`），**立刻触发崩溃(Fail-Fast)或直接拒绝该工具的更新请求**，并将详细的报错日志和追踪堆栈打印，防止错误扩散至会话执行阶段。

---

## 六、 验证与自动化测试计划

在重构代码后，必须通过自动化测试来确保设计的稳健性。

### 6.1 单元测试设计
在 `tests/unit/test_tool_interpolation.py` 中编写如下用例：

```python
import pytest
from app.tools.acli.executor import TemplateInterpolator

def test_successful_interpolation():
    # 测试常规正常插值
    template = "acli plugins vm_start vm_start --vm-id {vm_id}"
    args = {"vm_id": "vm-12345", "node_ip": "10.0.0.1"}
    command = TemplateInterpolator.interpolate(template, args)
    assert command == "acli plugins vm_start vm_start --vm-id vm-12345"

def test_shell_injection_protection():
    # 测试注入攻击逃逸保护
    template = "acli plugins vm_start vm_start --vm-id {vm_id}"
    args = {"vm_id": "vm-12345; rm -rf /"}
    command = TemplateInterpolator.interpolate(template, args)
    # 转义后成为单一字符串，不应产生命令注入
    assert command == "acli plugins vm_start vm_start --vm-id 'vm-12345; rm -rf /'"

def test_missing_parameter_raises_error():
    # 测试必填参数缺失报错
    template = "acli plugins vm_start vm_start --vm-id {vm_id} --disk-id {disk_id}"
    args = {"vm_id": "vm-12345"}
    with pytest.raises(ValueError) as excinfo:
        TemplateInterpolator.interpolate(template, args)
    assert "disk_id" in str(excinfo.value)
```

### 6.2 集成契约测试
- **测试命令**：
  ```bash
  uv run pytest backend/agent-service/tests/ -k "test_tool_interpolation" -vv
  ```
- **手动演练**：
  在控制台的“工具管理页面”中修改 `acli_plugin_vm_start` 的 `usage_template`（如在后面加上 `--verbose`），触发热更新，然后在对话窗口中调用该插件工具，观察 stdout 执行的命令是否带上了 `--verbose` 标记。
