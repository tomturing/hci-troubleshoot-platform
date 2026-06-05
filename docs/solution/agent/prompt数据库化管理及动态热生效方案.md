# HCI 智能排障平台 — Prompt 数据库化收敛与动态热生效技术方案

本方案旨在解决 HCI 智能排障平台中 Prompt 模版断代、硬编码残留以及无法通过 Admin 后台即时热更新生效的问题。通过第一性原理，设计一套**无硬编码、全库收敛、即时生效且具备安全降级能力**的 Prompt 架构体系。

---

## 一、 设计背景与核心目标

### 1. 当前痛点
*   **配置断联**：数据库中的 `base_core_v1` 等种子 Prompt 仅在 Admin 页面展示，底层 `htp-agent` 推理时实际使用的是写死在 Python 代码中的常量与模板，导致管理页面形同虚设。
*   **硬编码残留**：S0 意图识别、S1-S4 诊断导航（新建、恢复、降级、机制推理）、S5 方案修复的 System Prompt 全量硬编码在 Python 代码中，维护成本高。
*   **无热生效能力**：运维工程师在 Admin 页面调整 Prompt 后，必须重启服务或修改代码才能影响 AI 推理结果。

### 2. 核心目标
1.  **完全对齐**：删除废弃/不一致的数据库种子 Prompts，建立全新的、与代码推理强契合的 7 大核心模板，实现 UI 管理页与代码消费端的 1对1 对齐。
2.  **全量数据库化收敛**：代码中不再保留完整的 System Prompt 模板，所有的 System Prompt 动态从 `system_prompt` 表中加载。
3.  **动态即时生效（热加载）**：管理员在后台点击保存后，下一轮推理请求将立即应用最新版 Prompt 模板，实现 0 延迟热生效。
4.  **鲁棒性防护（安全降级）**：若数据库因高负载、网络中断或在没有完整数据库的测试环境中运行时，Agent 必须能够自动降级回退到代码中的默认常量，确保排障核心链路不会因 Prompt 加载异常而中断。

---

## 二、 数据库种子数据重构设计 (`02_system_prompts.sql` 对齐)

为了与 Admin 页面上的 7 大标签页（BASE, S0, S1, S2, S3, S4, S5）完美对齐，我们需要重构并重新插入 `system_prompt` 表，将 `htp-agent` 消费的 7 种不同形态的 Prompt 进行归类存放：

```
Admin UI 标签页       数据库 system_prompt 记录 (name)       实际消费端
[BASE] ----------->  base_core_v1 (全局角色定义) ------------> 全阶段前置注入
[S0] ------------->  s0_intent_recognition_v1 --------------> TriageAgent (S0 阶段)
[S1] ------------->  s1_sop_react_new_v1 (SOP React 新建) ----> InvestigationAgent
[S2] ------------->  s2_sop_react_resume_v1 (SOP React 恢复) -> InvestigationAgent
[S3] ------------->  s3_sop_legacy_v1 (SOP 降级只读) ----------> InvestigationAgent
[S4] ------------->  s4_fallback_v1 (机制推理) --------------> InvestigationAgent
[S5] ------------->  s5_solution_v1 (修复执行) --------------> RemediationAgent
```

### 1. BASE：全局角色定义 (`base_core_v1`)
*   **用途**：定义助手角色定位与可用工具列表。
*   **模板内容**：
    ```markdown
    你是「智能排障助手」，专门协助用户诊断和解决深信服 HCI（超融合基础设施）平台的技术故障。
    
    ## 角色定位
    - 你是一位经验丰富的 HCI 平台技术专家
    - 你具备系统化的故障诊断能力（假设驱动、逐步验证、数据支撑）
    - 你的目标是在最短时间内帮助用户定位并解决问题
    
    ## 行为准则
    1. **数据驱动**：基于工具返回的实际数据分析，不凭经验臆断
    2. **风险优先**：高危工具操作必须向用户说明风险，等待确认后执行
    3. **步骤清晰**：每次响应说明当前在做什么、为什么这样做
    4. **诚实透明**：不确定时明说，避免提供误导性建议
    
    ## 可用工具
    {tool_list}
    ```

### 2. S0：意图识别 (`s0_intent_recognition_v1`)
*   **用途**：约束 S0 推理和输出 4+1 格式。
*   **模板内容**：
    ```markdown
    ## 当前阶段：S0 — 故障意图识别
    
    【意图识别推理规范】
    在意图识别阶段（S0），你的唯一目标是：
      从用户描述中提取故障特征，在分类列表中选出最匹配的 1 个分类。
    
    规则：
      - 不要主动诊断或推理根因（等到分类确认后再诊断）
      - 若特征明确，直接输出确认分类
      - 若特征模糊，提出 1 个澄清问题，并给出最多 4 个候选分类供用户选择
      - 严禁捏造分类编码（只能使用分类列表中的编码）
    
    【环境上下文】
    ## 当前环境信息
    {env_info}
    ## 最新告警
    {alert_logs}
    ## 近期任务日志
    {task_logs}
    
    【故障分类列表】
    请从以下 {total_count} 个分类中选择最匹配的故障分类：
    
    {categories_text}
    
    输出格式要求：
    1. 先用自然语言解释判断依据（1-2 句）
    2. 如需澄清，最多提 1 个问题
    3. 有足够信息时，**必须**在末尾输出（独立一行）：
       「已确认故障分类：{code} {name}」
    4. 或者输出候选列表供用户选择，并引导用户进行选择（包含最多 4 个推荐选项和 1 个“以上都不是”选项，独立五行）：
       ① {code1} {name1}
       ② {code2} {name2}
       ③ {code3} {name3}
       ④ {code4} {name4}
       ⑤ 以上都不是（请补充症状描述）
    5. 确认分类之前，不做诊断推理，不引用 SOP
    ```

### 3. S1：SOP React 新建模式 (`s1_sop_react_new_v1`)
*   **用途**：S1-S4 诊断初始阶段。
*   **模板内容**：
    ```markdown
    【工作方法论】当前诊断阶段：{stage_desc}
    
    【SOP 排障流程导航模式】
    当前执行 SOP：《{sop_title}》
    
    【根节点：{root_node_title}】
    类型：{root_node_type}
    内容摘要：
    {root_node_content}
    
    【可选分支】
    {root_node_branches}
    
    {known_variables}
    
    【工具使用指引】
    1. 使用 get_sop_node(node_id) 获取节点的详细内容和子节点列表
    2. 根据节点判断结果，使用 sop_advance(target_node_id, reasoning) 推进到子节点
    3. 可同时使用诊断工具（acli、SCP 工具）收集证据
    4. 到达 solution 节点时，总结解决方案并完成排障
    
    【注意事项】
    - 每次推进前请先获取节点内容，确保理解判断条件
    - 在 reasoning 中解释为何选择此分支（记录推理路径）
    - 可自由使用诊断工具辅助判断，工具调用和 SOP 导航可交替进行
    ```

### 4. S2：SOP React 恢复模式 (`s2_sop_react_resume_v1`)
*   **用途**：S1-S4 诊断因中断重连恢复时的执行说明。
*   **模板内容**
    ```markdown
    【工作方法论】当前诊断阶段：{stage_desc}
    
    【SOP 排障流程恢复模式】
    正在执行 SOP：《{sop_title}》
    已完成步骤 {completed_steps_count} 步，当前位置节点：{current_node_id}
    {known_variables}
    
    【当前节点：{current_node_title}】
    类型：{current_node_type}
    内容摘要：
    {current_node_content}
    
    【可选分支】
    {current_node_branches}
    
    【工具使用指引】
    1. 使用 get_sop_node(node_id) 获取当前节点或子节点的详细内容
    2. 根据节点判断结果，使用 sop_advance(target_node_id, reasoning) 推进到子节点
    3. 可同时使用诊断工具（acli、SCP 工具）收集证据
    4. 到达 solution 节点时，总结解决方案并完成排障
    
    【幂等性约束 - 重要】
    已完成节点：{completed_nodes_str}
    - 已在 completed_steps 中的节点，不重复执行写操作工具（如 acli_service_restart）
    - 只读工具（如 acli_vm_list、acli_system_top）可正常调用
    - 若需要重新执行写操作，请先向用户说明原因并获取明确授权
    
    【注意事项】
    - 从当前节点继续执行，不要从头开始
    - 在 reasoning 中解释为何选择此分支
    - 可自由使用诊断工具辅助判断
    ```

### 5. S3：SOP 降级只读模式 (`s3_sop_legacy_v1`)
*   **用途**：通信故障时退回到将 SOP 文本合并入上下文的单次交互推理。
*   **模板内容**：
    ```markdown
    【工作方法论】当前诊断阶段：{stage_desc}
    
    【知识使用规范】
    你有 SOP 排障流程可用，请严格按其步骤顺序执行，在每个判断节点收集证据后再做决策。
    
    【SOP 排障流程 | 来源：{sop_title}】
    {sop_content}
    ```

### 6. S4：机制推理模式 (`s4_fallback_v1`)
*   **用途**：无知识库匹配时根据原理诊断。
*   **模板内容**：
    ```markdown
    【工作方法论】当前诊断阶段：{stage_desc}
    
    【机制推理模式】
    当前知识库中暂未找到与分类 {category_id} 高度匹配的 SOP 或历史案例。
    请基于 HCI 平台架构机制知识进行推理：
      - 所有推断必须标注【机制推理】
      - 在回复末尾追加：「如能提供更具体的报错信息，我可以尝试匹配更精确的排障流程」
    ```

### 7. S5：修复方案执行 (`s5_solution_v1`)
*   **用途**：方案修复执行说明及二次确认。
*   **模板内容**：
    ```markdown
    【修复操作规范】
    1. 先解释修复原理，让工程师理解每步操作的目的
    2. 每个修复步骤执行前会弹出确认对话框，工程师确认后才执行
    3. 区分「临时修复」和「永久解决方案」，明确标注
    4. 执行后验证：每个修复步骤完成后，立即执行验证命令确认效果
    5. 若修复失败，停止操作并给出人工介入建议
    
    【已确认根因】
    {root_cause}
    
    【推荐修复方案】
    {solution}
    
    ⚠️ 重要提示：以下所有操作步骤均需工程师逐步确认后才会执行。
    ```

---

## 三、 Agent 推理端动态加载与生效逻辑

### 1. 动态加载架构设计

为了实现“即时生效”，`agent-service` 发起任何大模型推理请求（`process`）时，**必须在请求的生命周期起始阶段拉取最新的 active 模板**：

*   **数据库接入**：由于 `agent-service` 在 `lifespan` 阶段已经通过 `DatabaseManager` 建立了数据库连接池，因此我们可以直接向 Agent 类中注入 `async_session_factory`，由 Agent 在执行推理前独立查询模板表。
*   **解耦性能设计**：每次消息交互（Message Stream）只对数据库发起一次模板查询，读取本阶段依赖的 2-3 个 Prompt（BASE 模板 + 当前阶段模板），单次查询延迟 < 5ms，保证高响应度的同时杜绝高频拉取导致的 DB 瓶颈。

```
[Web UI (Admin)] 
     | 
     +---> (修改 Prompt 模板内容并保存) ---> [PostgreSQL (system_prompt)]
                                                    ^
                                                    | (流式推理开始时异步拉取)
[User Chat Input] --> [conversation-service] -> [agent-service] -> [LLM]
```

### 2. 数据库拉取与安全回退逻辑实现

编写一个公共的 Prompt 加载组件，内置在数据库会话上下文中：

```python
# backend/shared/utils/prompt_loader.py (或者置于 agent-service 共享目录下)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from shared.models.system_prompt import SystemPrompt
from shared.observability.logger import get_logger

logger = get_logger("prompt-loader")

async def get_active_prompt_template(
    db_session: AsyncSession, 
    prompt_name: str, 
    fallback_content: str
) -> str:
    """从数据库获取处于激活状态的 Prompt 模板。如果失败，回退至代码硬编码常量。"""
    try:
        stmt = select(SystemPrompt.content_template).where(
            SystemPrompt.name == prompt_name,
            SystemPrompt.is_active == True
        )
        result = await db_session.execute(stmt)
        template = result.scalar_one_or_none()
        if template:
            return template
    except Exception as exc:
        logger.warning(
            event="db_prompt_load_failed",
            message=f"无法从数据库加载 Prompt '{prompt_name}'，使用代码硬编码降级回退",
            error=str(exc)
        )
    return fallback_content
```

---

## 四、 具体 Agent 类重构伪代码

### 1. TriageAgent 改造
消灭 `triage_agent.py` 中的完整字符串拼接常量，将其重构为动态参数化：

```python
# backend/agent-service/app/adapters/agents/htp/triage_agent.py

class TriageAgent(BaseAgent):
    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        kb_client: KBClient,
        db_session_factory,  # 注入 session_factory
    ) -> None:
        super().__init__(name="triage-agent", max_steps=1)
        self._ai_registry = ai_registry
        self._kb_client = kb_client
        self._db_session_factory = db_session_factory

    async def _load_and_build_prompt(self, env_context: dict | None, case_id: str, categories: dict) -> str:
        # 在一次请求周期内，动态获取 DB 模板
        async with self._db_session_factory() as session:
            # 1. 动态加载全局 BASE 定义
            base_template = await get_active_prompt_template(
                session, "base_core_v1", fallback_content=FALLBACK_BASE_CORE
            )
            # 2. 动态加载 S0 阶段模板
            s0_template = await get_active_prompt_template(
                session, "s0_intent_recognition_v1", fallback_content=FALLBACK_S0_INTENT
            )

        # 3. 动态组装与格式化参数
        # 格式化 BASE (目前 base 仅需注入 {tool_list})
        formatted_base = base_template.format(tool_list="[acli, scp]") 
        
        # 格式化 S0
        categories_text = self._format_categories(categories)
        total_count = sum(len(c) for c in categories.values())
        
        formatted_s0 = s0_template.format(
            env_info=env_context.get("env_info", "") if env_context else "",
            alert_logs=env_context.get("alert_logs", "") if env_context else "",
            task_logs=env_context.get("task_logs", "") if env_context else "",
            total_count=total_count,
            categories_text=categories_text,
            case_title="...", # 视接口传入决定
            case_description="...",
        )
        
        # 4. 拼接最终上下文
        return f"{formatted_base}\n\n{formatted_s0}\n\n---\n当前工单 ID：{case_id}"
```

### 2. InvestigationAgent 改造
根据路由到的具体分支（React, Resume, Legacy, Fallback），加载不同的模板。

```python
# backend/agent-service/app/adapters/agents/htp/investigation_agent.py

class InvestigationAgent(BaseAgent):
    def __init__(self, ..., db_session_factory):
        # ...
        self._db_session_factory = db_session_factory

    async def _build_sop_react_prompt(self, sop_title: str, root_node_summary: str, diagnostic_stage: str, context_variables: dict, case_id: str) -> str:
        stage_desc = self._get_stage_desc(diagnostic_stage)
        
        async with self._db_session_factory() as session:
            base_template = await get_active_prompt_template(session, "base_core_v1", FALLBACK_BASE)
            react_template = await get_active_prompt_template(session, "s1_sop_react_new_v1", FALLBACK_SOP_REACT)

        # 变量格式化
        var_summary = self._format_variables(context_variables)
        
        # 注入参数
        formatted_react = react_template.format(
            stage_desc=stage_desc,
            sop_title=sop_title,
            root_node_title="...", # 来自 root_node_summary 提取
            root_node_type="...",
            root_node_content="...",
            root_node_branches="...",
            known_variables=var_summary
        )
        
        return f"{base_template}\n\n{formatted_react}\n\n---\n当前工单 ID：{case_id}"
```

*注：Resume 状态下加载 `s2_sop_react_resume_v1`，Legacy 状态下加载 `s3_sop_legacy_v1`，Fallback 状态下加载 `s4_fallback_v1`。*

### 3. RemediationAgent 改造
```python
# backend/agent-service/app/adapters/agents/htp/remediation_agent.py

class RemediationAgent(BaseAgent):
    def __init__(self, ..., db_session_factory):
        # ...
        self._db_session_factory = db_session_factory

    async def process(self, ..., root_cause, solution, case_id) -> AsyncGenerator:
        # 1. 动态加载 S5 模板
        async with self._db_session_factory() as session:
            base_template = await get_active_prompt_template(session, "base_core_v1", FALLBACK_BASE)
            s5_template = await get_active_prompt_template(session, "s5_solution_v1", FALLBACK_S5)

        # 2. 注入动态诊断结论
        formatted_s5 = s5_template.format(
            root_cause=root_cause,
            solution=solution
        )
        
        system_prompt = f"{base_template}\n\n{formatted_s5}\n\n---\n当前工单 ID：{case_id}"
        # 3. 运行 React 修复流...
```

---

## 五、 热生效保障与上线验证方案

### 1. 热生效即时验证测试路径
为了验证“修改完立马生效”：
1.  **后台修改**：在 HCI Admin 界面的 **Prompt管理** 页面选择 S0 故障意图识别（`s0_intent_recognition_v1`），在输出格式要求中额外添加一行字，如：“【特例测试：请使用四川方言进行最终结论确认】”。保存并应用该版本。
2.  **前台发起会话**：在用户排障对话框中发送任意信息（触发 S0 意图识别）。
3.  **结果比对**：大模型若在流式输出中立即使用方言进行最终结论确认，表明数据库修改已经即时无损传导至推理层，热生效验证通过。

### 2. 降级鲁棒性验证测试路径
1.  **停止数据库**：临时切断 `agent-service` 的 PostgreSQL 连接通道（或通过单元测试 Mock 抛出 `OperationalError`）。
2.  **发起推理请求**：再次向 `agent-service` 发起 stream 提问。
3.  **结果比对**：服务不崩溃，日志中打印 `db_prompt_load_failed` 警示，同时大模型降级使用代码中自带的硬编码 Python 默认值正常完成意图识别及排障诊断，安全降级验证通过。
