# HCI 智能排障平台 — Prompt 数据库化收敛与动态热生效技术方案

本方案基于**第一性原理**与**代码结构主导原则**，重新设计平台 Prompt 的数据库化收敛及热生效机制。本设计将**运行中且已验证的 Python 代码 Prompts 作为唯一的“真理源”**，以此重构数据库种子，并实现严密的占位符静态验证与“喧闹式报错”（Loud Failing）机制，彻底消除开发与调试中的配置吞噬与隐藏缺陷。

---

## 一、 设计原则

### 1. 代码结构主导原则 (Code-Driven Database Schema)
不为了适配原有的不合理数据库 Prompt 而强行修改代码。相反，**将代码中正在生效、且已在生产中被验证的 Prompt 硬编码作为“第一真理源”，按此结构重构数据库中的模板库**。

### 2. UI 界面 1对1 映射原则
排障流中的 S1-S4 属于 `InvestigationAgent` 控制的诊断调查阶段，其实质包含 4 种不同的底层决策模式（SOP React模式、SOP恢复模式、SOP降级模式、机制推理模式）。本设计将这 4 种决策模式的 Prompt 分别映射到 Admin 后台的 S1, S2, S3, S4 标签页中，从而使 UI 管理页面与底层代码的逻辑结构形成严密的 1对1 契合。

### 3. “强校验与喧闹报错”原则 (Loud Failing & Zero Silent recovery)
在调试与热更新生效阶段，**禁止任何隐式回退（Silent Fallback）或错误掩盖**：
*   **格式安全校验**：模板加载时需通过语法解析器静态提取其中的占位符，必须与代码运行时注入的参数（如 `{categories_text}` 等）完全契合。如果管理员修改时误删、误加、拼错占位符，系统必须**直接阻断并抛出详细的格式错误**。
*   **热加载报错传导**：若数据库拉取失败或版本解析出错，AI 推理流必须即时中断，并直接将底层堆栈异常抛出到前端 SSE 错误气泡或控制台，防止因静默回退到旧版代码常量而导致“管理员修改后台却没有任何感知”的情况。

---

## 二、 数据库模板重构设计 (Seeds SQL 重构)

基于以上原则，将原有 `seeds/02_system_prompts.sql` 全量推倒重来。模板内容直接提取自 Python 源代码，并按代码决策模式分布在 7 个标签页：

```
Admin 标签页      数据库 system_prompt (name)                参数占位符要求
[BASE] --------> base_identity_v1 (专家身份定义) ----------- 无
                 base_methodology_v1 (标准方法论) --------- {stage_desc}
                 base_case_context_v1 (工单页脚) ----------- {case_id}
[S0] ----------> s0_intent_recognition_v1 (意图识别) -------- {env_info}, {alert_logs}, {task_logs}, {total_count}, {categories_text}
[S1] ----------> s1_sop_react_new_v1 (SOP React 新运行) ---- {sop_title}, {root_node_title}, {root_node_type}, {root_node_content}, {root_node_branches}, {known_variables}
[S2] ----------> s2_sop_react_resume_v1 (SOP React 恢复) --- {sop_title}, {completed_steps_count}, {current_node_id}, {known_variables}, {current_node_title}, {current_node_type}, {current_node_content}, {current_node_branches}, {completed_nodes_str}
[S3] ----------> s3_sop_legacy_v1 (SOP 文本降级) ----------- {sop_title}, {sop_content}
[S4] ----------> s4_fallback_v1 (Fallback 机制推理) -------- {category_id}
[S5] ----------> s5_solution_v1 (S5 修复执行) -------------- {root_cause}, {solution}
```

### 1. BASE 分类（全局定义对齐）
包含三个核心组件，由底层 Agent 拼装到所有的 System Prompt 头尾部：
*   **`base_identity_v1`**：
    ```markdown
    你是深信服超融合基础设施（HCI）智能排障专家助手。
    你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、
    IPMI硬件管理、acli诊断工具集的完整用法。
    你的目标是协助现场工程师快速定位和解决 HCI 平台故障。
    ```
*   **`base_methodology_v1`**：
    ```markdown
    【工作方法论】
    当前诊断阶段：{stage_desc}
    
    标准诊断流程：
    S0 意图识别：从客户描述提取关键实体（虚拟机名/集群/时间点），同时查看告警日志和操作日志，确认客户真实问题
    S1 故障定位：向客户提出 1-3 个精准确认问题，定位到最小故障分类
    S2 假设生成：列出 2-3 个最可能的根因假设，按概率排序
    S3 验证执行：逐一执行诊断命令，收集系统状态证据
    S4 根因确认：根据证据确定根因
    S5 方案输出：提供明确可执行的修复步骤
    S6 验证闭环：确认问题已解决，记录知识
    ```
*   **`base_case_context_v1`**：
    ```markdown
    ---
    当前工单 ID：{case_id}
    ```

### 2. S0 分类（意图识别对齐）
*   **`s0_intent_recognition_v1`**：
    ```markdown
    【知识使用规范】
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

### 3. S1 分类（SOP React 新建模式）
*   **`s1_sop_react_new_v1`**：
    ```markdown
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

### 4. S2 分类（SOP React 恢复模式）
*   **`s2_sop_react_resume_v1`**：
    ```markdown
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

### 5. S3 分类（SOP 降级只读模式）
*   **`s3_sop_legacy_v1`**：
    ```markdown
    【知识使用规范】
    你有 SOP 排障流程可用，请严格按其步骤顺序执行，在每个判断节点收集证据后再做决策。
    
    【SOP 排障流程 | 来源：{sop_title}】
    {sop_content}
    ```

### 6. S4 分类（机制推理降级模式）
*   **`s4_fallback_v1`**：
    ```markdown
    【机制推理模式】
    当前知识库中暂未找到与分类 {category_id} 高度匹配的 SOP 或历史案例。
    请基于 HCI 平台架构机制知识进行推理：
      - 所有推断必须标注【机制推理】
      - 在回复末尾追加：「如能提供更具体的报错信息，我可以尝试匹配更精确的排障流程」
    ```

### 7. S5 分类（S5 修复方案与执行说明）
*   **`s5_solution_v1`**：
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

## 三、 强验证热加载引擎实现设计

为了确保管理员在 UI 界面修改完 Prompt 后**能即时生效且有错必报**，底层加载组件必须在查询 DB 模板后，先运行严格的**静态占位符安全比对**。

### 1. 强验证模板加载器实现

设计 `StrictPromptLoader` 类，利用 Python 的 `string.Formatter` 分析模板中的占位符，发现不匹配直接向外抛出异常，阻止 AI 推理：

```python
# backend/shared/utils/prompt_loader.py

import string
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from shared.models.system_prompt import SystemPrompt
from shared.observability.logger import get_logger

logger = get_logger("prompt-loader")

class PromptLoadError(Exception):
    """Prompt 从数据库加载失败异常"""
    pass

class PromptValidationError(Exception):
    """Prompt 模板占位符校验不匹配异常"""
    pass

class StrictPromptLoader:
    
    @staticmethod
    def get_template_placeholders(template_str: str) -> set[str]:
        """提取模板中所有的 {placeholder} 占位符名称"""
        try:
            return {name for _, name, _, _ in string.Formatter().parse(template_str) if name is not None}
        except ValueError as exc:
            raise PromptValidationError(f"Prompt 模板解析错误，存在无效的占位符括号语法: {exc}")

    @classmethod
    async def load_and_validate(
        cls, 
        db_session: AsyncSession, 
        prompt_name: str, 
        expected_placeholders: list[str]
    ) -> str:
        """
        从数据库加载 Prompt 并强行进行占位符契约验证。
        若有任何异常，绝不静默降级，直接报错。
        """
        # 1. 数据库检索
        try:
            stmt = select(SystemPrompt.content_template).where(
                SystemPrompt.name == prompt_name,
                SystemPrompt.is_active == True
            )
            result = await db_session.execute(stmt)
            content_template = result.scalar_one_or_none()
        except Exception as exc:
            # 数据库访问异常，直接抛出，阻断逻辑
            raise PromptLoadError(
                f"数据库查询异常，无法加载 Prompt 模板 '{prompt_name}': {exc}"
            ) from exc

        if not content_template:
            raise PromptLoadError(
                f"在 system_prompt 表中未找到处于激活状态且名称为 '{prompt_name}' 的 Prompt 模板"
            )

        # 2. 占位符比对校验
        actual_placeholders = cls.get_template_placeholders(content_template)
        expected_set = set(expected_placeholders)
        
        # 校验：检查是否有代码运行时必填的参数，在数据库模板中缺失
        missing_placeholders = expected_set - actual_placeholders
        if missing_placeholders:
            raise PromptValidationError(
                f"Prompt 模板 '{prompt_name}' 校验不通过！"
                f"缺少运行时必需的占位符: {missing_placeholders}。请检查并修改数据库配置。"
            )

        # 校验：检查模板里是否有代码运行时无法提供赋值的非法占位符（避免 .format 报 KeyError）
        redundant_placeholders = actual_placeholders - expected_set
        if redundant_placeholders:
            raise PromptValidationError(
                f"Prompt 模板 '{prompt_name}' 校验不通过！"
                f"包含运行时无法识别的非法占位符: {redundant_placeholders}。"
            )

        return content_template
```

### 2. 微服务间错误冒泡与前端提示路径
在 `agent-service` 侧发生 `PromptLoadError` 或 `PromptValidationError` 时，系统应当做如下处理：
1.  **停止生成流**：终止 LLM 管道，阻止生成请求。
2.  **SSE 管道输出错误事件**：
    在 `app/routes/agent.py` 的 `_event_stream` 中捕获此类异常，并以 `type: "error"` 序列化为标准的 SSE 事件投递给 `conversation-service`：
    ```python
    except (PromptLoadError, PromptValidationError) as exc:
        logger.error(event="prompt_engine_failure", message=str(exc))
        yield _sse({"type": "error", "message": f"[Prompt配置错误] {str(exc)}"})
        return
    ```
3.  **前端直接弹框拦截**：
    `conversation-service` 接收到 `error` 事件后，会传给前端对话卡片。前端对话卡片会弹窗提示：
    > **[AI 系统异常]**  
    > 数据库模板 `s0_intent_recognition_v1` 校验不通过：缺少运行时必需的占位符 `{categories_text}`。请联系管理员进入“Prompt管理后台”修复。

这使得 Prompt 设计不仅能在修改后秒级热生效，而且配置错误时能第一时间在会话流中以清晰的界面错误暴露出来，极易调试。

---

## 四、 具体 Agent 类加载流重构伪代码

### 1. TriageAgent 改造
```python
# backend/agent-service/app/adapters/agents/htp/triage_agent.py
from shared.utils.prompt_loader import StrictPromptLoader

class TriageAgent(BaseAgent):
    def __init__(self, ai_registry, kb_client, db_session_factory):
        super().__init__(name="triage-agent", max_steps=1)
        self._ai_registry = ai_registry
        self._kb_client = kb_client
        self._db_session_factory = db_session_factory

    async def process(self, session_id, messages, env_context, case_id, ...) -> AsyncGenerator:
        await self._ensure_categories_loaded()
        
        # 1. 动态获取会话级别的 AsyncSession
        async with self._db_session_factory() as session:
            # 2. 强校验并加载各个模板片段（缺少或多余占位符直接抛出异常，流中断报错）
            base_identity = await StrictPromptLoader.load_and_validate(
                session, "base_identity_v1", expected_placeholders=[]
            )
            base_methodology = await StrictPromptLoader.load_and_validate(
                session, "base_methodology_v1", expected_placeholders=["stage_desc"]
            )
            s0_rules = await StrictPromptLoader.load_and_validate(
                session, "s0_intent_recognition_v1", 
                expected_placeholders=["env_info", "alert_logs", "task_logs", "total_count", "categories_text"]
            )
            base_context = await StrictPromptLoader.load_and_validate(
                session, "base_case_context_v1", expected_placeholders=["case_id"]
            )

        # 3. 参数格式化装配
        formatted_methodology = base_methodology.format(stage_desc="S0 - 意图识别")
        
        total_count = sum(len(c) for c in self._categories_cache.values())
        categories_text = self._format_categories(self._categories_cache)
        
        formatted_s0_rules = s0_rules.format(
            env_info=env_context.get("env_info", "") if env_context else "",
            alert_logs=env_context.get("alert_logs", "") if env_context else "",
            task_logs=env_context.get("task_logs", "") if env_context else "",
            total_count=total_count,
            categories_text=categories_text
        )
        
        formatted_context = base_context.format(case_id=case_id)
        
        # 4. 最终 System Prompt 渲染
        system_prompt = "\n\n".join([
            base_identity,
            formatted_methodology,
            formatted_s0_rules,
            formatted_context
        ])

        # 5. 调用 LLM
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        # streaming ...
```

### 2. InvestigationAgent 改造
```python
# backend/agent-service/app/adapters/agents/htp/investigation_agent.py
from shared.utils.prompt_loader import StrictPromptLoader

class InvestigationAgent(BaseAgent):
    def __init__(self, ..., db_session_factory):
        # ...
        self._db_session_factory = db_session_factory

    async def _build_sop_react_prompt(
        self, 
        sop_title: str, 
        root_node: dict, 
        diagnostic_stage: str, 
        context_variables: dict, 
        case_id: str
    ) -> str:
        stage_desc_map = {"S1": "S1 - 故障定位", "S2": "S2 - 假设生成", "S3": "S3 - 证据验证", "S4": "S4 - 根因确认"}
        stage_desc = stage_desc_map.get(diagnostic_stage, diagnostic_stage)

        # 获取根节点数据摘要参数
        root_node_title = root_node.get("title", sop_title)
        root_node_type = root_node.get("type", "branch")
        root_node_content = root_node.get("content", "")[:500]
        root_node_branches = "\n".join([f"- {c['node_id']}: {c['title']}" for c in root_node.get("children", [])[:5]])
        var_summary = self._format_variables(context_variables)

        # 强加载 DB 模板
        async with self._db_session_factory() as session:
            base_identity = await StrictPromptLoader.load_and_validate(
                session, "base_identity_v1", []
            )
            base_methodology = await StrictPromptLoader.load_and_validate(
                session, "base_methodology_v1", ["stage_desc"]
            )
            react_template = await StrictPromptLoader.load_and_validate(
                session, "s1_sop_react_new_v1",
                ["sop_title", "root_node_title", "root_node_type", "root_node_content", "root_node_branches", "known_variables"]
            )
            base_context = await StrictPromptLoader.load_and_validate(
                session, "base_case_context_v1", ["case_id"]
            )

        # 格式化组装
        formatted_methodology = base_methodology.format(stage_desc=stage_desc)
        formatted_react = react_template.format(
            sop_title=sop_title,
            root_node_title=root_node_title,
            root_node_type=root_node_type,
            root_node_content=root_node_content,
            root_node_branches=root_node_branches,
            known_variables=var_summary
        )
        formatted_context = base_context.format(case_id=case_id)

        return "\n\n".join([base_identity, formatted_methodology, formatted_react, formatted_context])
```

---

## 五、 本地与上线调试验证方案

### 1. 模板校验错时即时验证（防掩盖验证）
*   **测试动作**：修改后台数据库中 `base_methodology_v1` 的模板内容，故意把 `{stage_desc}` 改为拼错的 `{stage_desc_error}`，点击保存生效。
*   **期望结果**：在前台发送消息后，排障对话立即报错，大模型不进行响应，控制台/前端明确弹出：`[Prompt配置错误] Prompt 模板 'base_methodology_v1' 校验不通过！缺少运行时必需的占位符: {'stage_desc'}。包含运行时无法识别的非法占位符: {'stage_desc_error'}`。确认配置异常无法被掩盖。

### 2. 热加载秒级热生效验证
*   **测试动作**：在 Admin UI 的 S4（无SOP机制推理）的 `s4_fallback_v1` 中修改文案，将末尾要求追加的文案变更为：“如能提供更具体的 HCI 报错信息，我能立刻帮您诊断”。
*   **期望结果**：保存后，立即发起一个无 SOP 匹配的故障排障会话，大模型的输出末尾立刻完美呈现刚刚修改的这行中文文案，无需重启 `agent-service`，热加载生效。
