"""
StrictPromptLoader - Prompt 强验证热加载引擎
"""

import string
from inspect import isawaitable

from shared.dynamic_resource.adapters import prompt_resource_payload, prompt_slot_resource_payload
from shared.dynamic_resource.loader import DynamicResourceLoader
from shared.dynamic_resource.models import UsageRecord
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.dynamic_resource.validator import DynamicResourceValidator
from shared.models.dynamic_resource import PromptSlot
from shared.models.system_prompt import SystemPrompt
from shared.observability.logger import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger("prompt-loader")


class PromptLoadError(Exception):
    """Prompt 从数据库加载失败异常"""

    pass


class PromptValidationError(Exception):
    """Prompt 模板占位符校验不匹配异常"""

    pass


class StrictPromptLoader:
    """带有静态占位符强契约校验的 Prompt 加载器"""

    @staticmethod
    def get_template_placeholders(template_str: str) -> set[str]:
        """提取模板中所有的 {placeholder} 占位符名称"""
        try:
            return {name for _, name, _, _ in string.Formatter().parse(template_str) if name is not None}
        except ValueError as exc:
            raise PromptValidationError(f"Prompt 模板解析错误，存在无效的占位符括号语法: {exc}") from exc

    @classmethod
    async def load_and_validate(
        cls,
        db_session: AsyncSession,
        prompt_name: str,
        expected_placeholders: list[str],
        *,
        consumer: str = "agent-service.prompt_loader",
        conversation_id: str | None = None,
        case_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        """
        从数据库加载 Prompt 并强行进行占位符契约验证。
        若有任何异常，绝不静默降级，直接抛出，阻断推理进行。
        """
        # 1. Slot 解析：prompt_name 可以是逻辑槽位，也可以是具体 system_prompt.name。
        effective_prompt_name = prompt_name
        effective_expected = list(expected_placeholders)
        slot_snapshot = None
        try:
            slot_result = await db_session.execute(
                select(PromptSlot).where(PromptSlot.slot_name == prompt_name, PromptSlot.is_active.is_(True))
            )
            slot = slot_result.scalar_one_or_none()
            if slot is not None and hasattr(slot, "active_prompt_name"):
                effective_prompt_name = slot.active_prompt_name
                slot_expected = slot.expected_placeholders or []
                if slot_expected:
                    effective_expected = [str(item) for item in slot_expected]
                slot_payload = prompt_slot_resource_payload(
                    slot.slot_name,
                    slot.active_prompt_name,
                    effective_expected,
                    slot.consumer,
                )
                slot_snapshot = await DynamicResourcePublisher(db_session).ensure_published(**slot_payload)

            stmt = select(SystemPrompt).where(
                SystemPrompt.name == effective_prompt_name, SystemPrompt.is_active.is_(True)
            )
            result = await db_session.execute(stmt)
            prompt = result.scalar_one_or_none()
        except Exception as exc:
            # 数据库访问异常，直接抛出，阻断逻辑
            raise PromptLoadError(f"数据库查询异常，无法加载 Prompt 模板 '{prompt_name}': {exc}") from exc

        if not prompt:
            raise PromptLoadError(
                f"在 system_prompt 表中未找到处于激活状态且名称为 '{effective_prompt_name}' 的 Prompt 模板"
            )

        # 2. 占位符比对校验
        content_template = prompt.content_template if hasattr(prompt, "content_template") else str(prompt)
        actual_placeholders = cls.get_template_placeholders(content_template)
        expected_set = set(effective_expected)
        validation = DynamicResourceValidator.validate_prompt_placeholders(
            actual_placeholders,
            expected_set,
            resource_name=effective_prompt_name,
        )
        if validation.status == "error":
            issue_text = "; ".join(issue.message for issue in validation.issues)
            raise PromptValidationError(
                f"Prompt 模板 '{effective_prompt_name}' 校验不通过！{issue_text}。请检查并修改数据库配置。"
            )

        if hasattr(prompt, "name"):
            prompt_snapshot = await DynamicResourcePublisher(db_session).ensure_published(
                **prompt_resource_payload(prompt)
            )
            loader = DynamicResourceLoader(db_session)
            if slot_snapshot is not None:
                await loader.audit_usage(
                    slot_snapshot,
                    UsageRecord(
                        consumer=consumer,
                        status="success",
                        conversation_id=conversation_id,
                        case_id=case_id,
                        trace_id=trace_id,
                        input_payload={"requested_prompt": prompt_name, "expected_placeholders": effective_expected},
                        output_payload={"active_prompt_name": effective_prompt_name},
                    ),
                )
            await loader.audit_usage(
                prompt_snapshot,
                UsageRecord(
                    consumer=consumer,
                    status="success",
                    conversation_id=conversation_id,
                    case_id=case_id,
                    trace_id=trace_id,
                    input_payload={"expected_placeholders": effective_expected},
                    output_payload={"placeholder_count": len(actual_placeholders)},
                    metadata={"requested_prompt": prompt_name},
                ),
            )
            if hasattr(db_session, "commit"):
                maybe_commit = db_session.commit()
                if isawaitable(maybe_commit):
                    await maybe_commit

        return content_template


class MockSession:
    """单元测试中模拟的 SQLAlchemy Session"""

    def __init__(self, templates: dict[str, str]):
        self.templates = templates

    async def execute(self, stmt):
        compiled = stmt.compile()
        if "prompt_slot" in str(compiled):

            class EmptyResult:
                def scalar_one_or_none(self):
                    return None

            return EmptyResult()
        prompt_name = compiled.params.get("name_1")
        if not prompt_name:
            for val in compiled.params.values():
                if val in self.templates:
                    prompt_name = val
                    break

        class MockResult:
            def __init__(self, val):
                self.val = val

            def scalar_one_or_none(self):
                return self.val

        return MockResult(self.templates.get(prompt_name))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def create_mock_session_factory(custom_templates: dict[str, str] = None):
    """创建一个模拟的 DB Session Factory 用于单元测试"""
    templates = {
        "base_identity_v1": '你是深信服超融合基础设施（HCI）智能排障专家助手。\n你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、\nIPMI硬件管理、acli诊断工具集的完整用法。\n你的目标是协助现场工程师快速定位和解决 HCI 平台故障。\n\n【证据锚定规则】\n1. 禁止凭空声明：你的所有排障结论、猜测和分析，必须有明确的工具输出（如 acli_exec/bash_exec 的返回结果）作为直接证据。严禁在无证据支持的情况下直接给出确定性的根因结论。\n2. 声明不确定性：当证据不足或工具执行未返回预期数据时，必须明确向用户声明当前结论的「不确定性」，并指出需要补充哪些维度的证据。\n3. 禁止跳步推理：每次推理必须循序渐进，不允许在未进行前置检查的情况下直接跳跃到后续修复或深层结论。\n4. 区分观察与结论：在回复中，必须清晰区分「观察到的原始事实」（工具输出）与「你的推断/结论」。\n5. 幻觉自查：在生成最终诊断结论前，进行一步幻觉自查（检查引用的命令是否真实执行过，引用的数字、状态是否与实际输出完全一致）。\n\n[正确示例]\n观察：工具 acli_vm_list 输出显示 VM "prod-vm" 状态为 "stopped"。\n结论：该 VM 目前处于停止状态，可能是由于管理员手动关闭或底层宿主机异常关机。\n\n[错误示例]\n结论：VM "prod-vm" 已经崩溃，因为存储连接断开了。（在未执行存储检查工具的情况下凭空猜测结论）',
        "base_methodology_v1": "【工作方法论】\n当前诊断阶段：{stage_desc}\n\n标准诊断流程：\nS0 意图识别：从客户描述提取关键实体（虚拟机名/集群/时间点），同时查看告警日志和操作日志，确认客户真实问题\nS1 故障定位：向客户提出 1-3 个精准确认问题，定位到最小故障分类\nS2 假设生成：列出 2-3 个最可能的根因假设，按概率排序\nS3 验证执行：逐一执行诊断命令，收集系统状态证据\nS4 根因确认：根据证据确定根因\nS5 方案输出：提供明确可执行的修复步骤\nS6 验证闭环：确认问题已解决，记录知识",
        "base_case_context_v1": "---\n当前工单 ID：{case_id}",
        "s0_intent_recognition_v1": "【知识使用规范】\n在意图识别阶段（S0），你的唯一目标是：\n  从用户描述中提取故障特征，在分类列表中选出最匹配的 1 个分类。\n\n规则：\n  - 不要主动诊断或推理根因（等到分类确认后再诊断）\n  - 若特征明确，直接输出确认分类\n  - 若特征模糊，提出 1 个澄清问题，并给出最多 4 个候选分类供用户选择\n  - 严禁捏造分类编码（只能使用分类列表中的编码）\n\n【环境上下文】\n## 当前环境信息\n{env_info}\n## 最新告警\n{alert_logs}\n## 近期任务日志\n{task_logs}\n\n【故障分类列表】\n请从以下 {total_count} 个分类中选择最匹配的故障分类：\n\n{categories_text}\n\n输出格式要求：\n1. 先用自然语言解释判断依据（1-2 句）\n2. 如需澄清，最多提 1 个问题\n3. 有足够信息时，**必须**在末尾输出（独立一行）：\n   「已确认故障分类：{{code}} {{name}}」\n4. 或者输出候选列表供用户选择，并引导用户进行选择（包含最多 4 个推荐选项和 1 个“以上都不是”选项，独立五行）：\n   ① {{code1}} {{name1}}\n   ② {{code2}} {{name2}}\n   ③ {{code3}} {{name3}}\n   ④ {{code4}} {{name4}}\n   ⑤ 以上都不是（请补充症状描述）\n5. 确认分类之前，不做诊断推理，不引用 SOP",
        "s1_sop_react_new_v1": "【SOP 排障流程导航模式】\n当前执行 SOP：《{sop_title}》\n\n【根节点：{root_node_title}】\n类型：{root_node_type}\n内容摘要：\n{root_node_content}\n\n【可选分支】\n{root_node_branches}\n\n{known_variables}\n\n【工具使用指引】\n1. 使用 get_sop_node(node_id) 获取节点的详细内容和子节点列表\n2. get_sop_node 返回 tool_calls 时，优先按 tool_calls 中的 tool_name/args 调用工具，不要从 commands 原文重新猜测工具参数\n3. 若节点或分支依赖变量且变量未出现在【已知变量】中，必须先调用 sop_request_variable(variable_name, reason)\n4. 变量来源为 user_input/user_confirm 时，禁止用命令自行替代用户输入或确认\n5. 根据节点判断结果，使用 sop_advance(target_node_id, reasoning) 推进到子节点\n6. 可同时使用诊断工具（acli、SCP 工具）收集证据\n7. 到达 solution 节点时，总结解决方案并完成排障\n\n【注意事项】\n- 每次推进前请先获取节点内容，确保理解判断条件\n- 在 reasoning 中解释为何选择此分支（记录推理路径）\n- 诊断工具只能补充证据，不得覆盖 SOP 变量声明中的来源策略",
        "s2_sop_react_resume_v1": "【SOP 排障流程恢复模式】\n正在执行 SOP：《{sop_title}》\n已完成步骤 {completed_steps_count} 步，当前位置节点：{current_node_id}\n{known_variables}\n\n【当前节点：{current_node_title}】\n类型：{current_node_type}\n内容摘要：\n{current_node_content}\n\n【可选分支】\n{current_node_branches}\n\n【工具使用指引】\n1. 使用 get_sop_node(node_id) 获取当前节点或子节点的详细内容\n2. 根据节点判断结果，使用 sop_advance(target_node_id, reasoning) 推进到子节点\n3. 可同时使用诊断工具（acli、SCP 工具）收集证据\n4. 到达 solution 节点时，总结解决方案并完成排障\n\n【幂等性约束 - 重要】\n已完成节点：{completed_nodes_str}\n- 已在 completed_steps 中的节点，不重复执行写操作命令（如 acli_exec 执行 restart/start/stop 等有副作用的命令）\n- 只读命令（如 acli_exec 执行 acli --formatter json vm list 或 acli --formatter json platform info get 等只读命令）可正常调用\n- 若需要重新执行写操作，请先向用户说明原因并获取明确授权\n\n【注意事项】\n- 从当前节点继续执行，不要从头开始\n- 在 reasoning 中解释为何选择此分支\n- 可自由使用诊断工具辅助判断",
        "s3_sop_legacy_v1": "【知识使用规范】\n你有 SOP 排障流程可用，请严格按其步骤顺序执行，在每个判断节点收集证据后再做决策。\n\n【SOP 排障流程 | 来源：{sop_title}】\n{sop_content}",
        "s4_fallback_v1": "【机制推理模式】\n当前知识库中暂未找到与分类 {category_id} 高度匹配 of SOP 或历史案例。\n请基于 HCI 平台架构机制知识进行推理：\n  - 所有推断必须标注【机制推理】\n  - 在回复末尾追加：「如能提供更具体的报错信息，我可以尝试匹配更精确的排障流程」\n\n【降级模式警告】\n当前处于机制推理的降级排障模式，由于缺乏匹配的专家知识库或 SOP 支持：\n1. 你的所有排障建议和临时结论必须明确标注「需要执行验证」，禁止给出任何「已确认根因」或「最终故障定位」等确定性声明。\n2. 在向用户推荐任何修复或诊断操作时，必须提示用户「该操作基于机制推理推荐，执行前请先手动/命令验证其安全性和必要性」。",
        "s5_solution_v1": "【修复操作规范】\n1. 先解释修复原理，让工程师理解每步操作的目的\n2. 每个修复步骤执行前会弹出确认对话框，工程师确认后才执行\n3. 区分「临时修复」和「永久解决方案」，明确标注\n4. 执行后验证：每个修复步骤完成后，立即执行验证命令确认效果\n5. 若修复失败，停止操作并给出人工介入建议\n\n【已确认根因】\n{root_cause}\n\n【推荐修复方案】\n{solution}\n\n⚠️ 重要提示：以下所有操作步骤均需工程师逐步确认后才会执行。",
        # ─── React 执行通用约束（新增，数据库化，与 02_system_prompts.sql 同步）───
        "s1_react_output_constraint_v1": '''【输出约束 — 强制执行】
1. 每次回复前用 <reasoning> 简述：已收集证据 / 假设支撑与反对 / 置信度(高/中/低) / 下一步行动。
2. 最终诊断报告严格按此模板，章节标题不得改名或增减：
   ## 故障摘要
   （一段话概述：什么故障、什么主机/磁盘/虚拟机、关键证据数据、根本原因）
   ## 根因
   （明确的根因判定，引用工具输出中的具体数据）
   ## 修复方案
   （solution 节点原文直出，合并快速恢复和彻底恢复为一段，不可修改、不可增删步骤）
3. solution 节点（no_tool_execution=true）：只输出其 content 原文，**严禁调用任何工具**。
4. 报告输出完毕后立即终止，不得追加额外步骤或工具调用。
5. 【数据源铁律】诊断报告的每一项内容必须来自以下三类来源之一，禁止使用训练数据中的通用知识：
   a. SOP 节点的 content/solution 原文（只能引用，不能改写）
   b. 工具执行的实际输出（bash_exec stdout / acli_exec json，必须标注具体命令）
   c. SOP 变量值（通过 sop_request_variable 或 skill 获取的变量值）
   ⚠️ 严禁：编造具体数值（虚拟机名、时延ms、容量TB）、添加 SOP 中没有的修复步骤、使用「通常」「一般」「建议」「可能」开头且无工具输出支撑的句子。
6. 【SOP决策树强制遵循】诊断必须严格按决策树节点推进，禁止自由探索：
   a. 到达 branch 节点后先执行该节点的 commands，再根据结果选择子节点
   b. 选择子节点必须通过 sop_advance 推进，严禁自行判断分支
   c. 叶节点(diagnosis/solution)到达后直接输出其 content
   d. **严禁绕过 SOP 自行调用 acli_exec/bash_exec 探索**，所有工具调用必须来自当前 SOP 节点的 commands
   e. sop_request_variable 获取 skill 变量后必须根据结果走对应 solution，不继续探索子节点
   f. sop_request_variable 只能请求 SOP 节点 required_variables 中列出的变量名，**严禁自行编造变量名**（如 alert_parsed、disk_info 等）。若返回 suggested_variables，必须用建议的变量名重试。
7. 【分支选择原则】到达 branch 节点后：
   a. 若节点有 has_solution=true：先获取依赖变量（如 check_meth），根据 skill 输出判断是否需要深入子节点
   b. 若节点 commands 为空且 children 非空：直接读取子节点内容，对比 evidence 选择最匹配的分支
   c. 选择分支**只看子节点的 prerequisites 是否满足当前收集的证据**，不凭「经验」或「通用知识」判断''',
        "s1_react_structured_output_v1": "【结构化输出强制要求】\n你必须输出符合以下 JSON Schema 的结构化 JSON：\n{schema_json}\n确保你的最终文本回复必须是合法的 JSON（位于 <reasoning> 之外），不要在 JSON 外包裹任何 markdown 或自然语言解释。",
        # ─── KBD 差异判定（新增，数据库化，与 02_system_prompts.sql 同步）────────
        "s3_kbd_judge_v1": '''你是 HCI 智能运维排障助手，正在执行 KBD 差异诊断。

已执行诊断工具：**{tool_name}**

实际工具输出：
```
{truncated_output}
```

请判断以上输出是否符合以下各 KBD 在此步骤的期望特征：

{kbd_expectations}

判断规则：
- 若实际输出包含 KBD 期望的关键特征 → true
- 若实际输出明确不符合 KBD 期望 → false
- 若无法确定（信息不足）→ 保守地返回 true

严格返回 JSON，不要有任何额外说明：
{{"matches": {{"KBD_ID_1": true, "KBD_ID_2": false}}}}
''',
        # ─── KBD 报告生成 & React 反幻觉（新增，数据库化，与 02_system_prompts.sql 同步）
        "s4_kbd_report_v1": '''你是 HCI 智能运维排障助手，已完成 KBD 差异诊断，请生成结构化诊断报告。

诊断步骤执行情况（共 {steps_count} 步）：
{steps_summary}

匹配 KBD（共 {kbds_count} 个）：
{kbds_summary}

报告要求（Markdown 格式）：
1. **故障确认**：最可能的根因（1-2句）
2. **诊断依据**：必须引用上方“诊断步骤执行情况”中真实采集到的关键信号输出（如报错原文、进程名、状态值）作为证据，禁止凭空断言；若某条结论在步骤输出中找不到对应证据，必须明确标注“（未经现场信号确认，建议执行：<具体命令>）”。
3. **处理建议**：按优先级列出 3-5 个操作步骤
4. **参考文档**：最匹配 KBD 名称及编号

约束：诊断依据中的每条论断都必须能在“诊断步骤执行情况”找到对应的真实输出支撑；不得把 KBD 文档里的既定结论当作已发生的现场观测。
面向 HCI 运维工程师，简洁专业，不要有多余废话。''',
        "s4_react_antihallucination_v1": "【反幻觉自我检查指令】你的上一次回答中包含未实际执行的工具引用，或者未在工具输出中找到数据来源的数值/百分比。请进行一步自我检查，修正这些幻觉，仅引用实际执行过的工具及对应的结果。请重新输出你的回答。",
    }
    if custom_templates:
        templates.update(custom_templates)

    def factory():
        return MockSession(templates)

    return factory
