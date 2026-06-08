"""
StrictPromptLoader - Prompt 强验证热加载引擎
"""

import string

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
        cls, db_session: AsyncSession, prompt_name: str, expected_placeholders: list[str]
    ) -> str:
        """
        从数据库加载 Prompt 并强行进行占位符契约验证。
        若有任何异常，绝不静默降级，直接抛出，阻断推理进行。
        """
        # 1. 数据库检索
        try:
            stmt = select(SystemPrompt.content_template).where(
                SystemPrompt.name == prompt_name, SystemPrompt.is_active.is_(True)
            )
            result = await db_session.execute(stmt)
            content_template = result.scalar_one_or_none()
        except Exception as exc:
            # 数据库访问异常，直接抛出，阻断逻辑
            raise PromptLoadError(f"数据库查询异常，无法加载 Prompt 模板 '{prompt_name}': {exc}") from exc

        if not content_template:
            raise PromptLoadError(f"在 system_prompt 表中未找到处于激活状态且名称为 '{prompt_name}' 的 Prompt 模板")

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
                f"Prompt 模板 '{prompt_name}' 校验不通过！包含运行时无法识别的非法占位符: {redundant_placeholders}。"
            )

        return content_template


class MockSession:
    """单元测试中模拟的 SQLAlchemy Session"""

    def __init__(self, templates: dict[str, str]):
        self.templates = templates

    async def execute(self, stmt):
        compiled = stmt.compile()
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
        "base_identity_v1": "你是深信服超融合基础设施（HCI）智能排障专家助手。\n你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、\nIPMI硬件管理、acli诊断工具集的完整用法。\n你的目标是协助现场工程师快速定位和解决 HCI 平台故障。",
        "base_methodology_v1": "【工作方法论】\n当前诊断阶段：{stage_desc}\n\n标准诊断流程：\nS0 意图识别：从客户描述提取关键实体（虚拟机名/集群/时间点），同时查看告警日志和操作日志，确认客户真实问题\nS1 故障定位：向客户提出 1-3 个精准确认问题，定位到最小故障分类\nS2 假设生成：列出 2-3 个最可能的根因假设，按概率排序\nS3 验证执行：逐一执行诊断命令，收集系统状态证据\nS4 根因确认：根据证据确定根因\nS5 方案输出：提供明确可执行的修复步骤\nS6 验证闭环：确认问题已解决，记录知识",
        "base_case_context_v1": "---\n当前工单 ID：{case_id}",
        "s0_intent_recognition_v1": "【知识使用规范】\n在意图识别阶段（S0），你的唯一目标是：\n  从用户描述中提取故障特征，在分类列表中选出最匹配的 1 个分类。\n\n规则：\n  - 不要主动诊断或推理根因（等到分类确认后再诊断）\n  - 若特征明确，直接输出确认分类\n  - 若特征模糊，提出 1 个澄清问题，并给出最多 4 个候选分类供用户选择\n  - 严禁捏造分类编码（只能使用分类列表中的编码）\n\n【环境上下文】\n## 当前环境信息\n{env_info}\n## 最新告警\n{alert_logs}\n## 近期任务日志\n{task_logs}\n\n【故障分类列表】\n请从以下 {total_count} 个分类中选择最匹配的故障分类：\n\n{categories_text}\n\n输出格式要求：\n1. 先用自然语言解释判断依据（1-2 句）\n2. 如需澄清，最多提 1 个问题\n3. 有足够信息时，**必须**在末尾输出（独立一行）：\n   「已确认故障分类：{{code}} {{name}}」\n4. 或者输出候选列表供用户选择，并引导用户进行选择（包含最多 4 个推荐选项和 1 个“以上都不是”选项，独立五行）：\n   ① {{code1}} {{name1}}\n   ② {{code2}} {{name2}}\n   ③ {{code3}} {{name3}}\n   ④ {{code4}} {{name4}}\n   ⑤ 以上都不是（请补充症状描述）\n5. 确认分类之前，不做诊断推理，不引用 SOP",
        "s1_sop_react_new_v1": "【SOP 排障流程导航模式】\n当前执行 SOP：《{sop_title}》\n\n【根节点：{root_node_title}】\n类型：{root_node_type}\n内容摘要：\n{root_node_content}\n\n【可选分支】\n{root_node_branches}\n\n{known_variables}\n\n【工具使用指引】\n1. 使用 get_sop_node(node_id) 获取节点的详细内容和子节点列表\n2. 根据节点判断结果，使用 sop_advance(target_node_id, reasoning) 推进到子节点\n3. 可同时使用诊断工具（acli、SCP 工具）收集证据\n4. 到达 solution 节点时，总结解决方案并完成排障\n\n【注意事项】\n- 每次推进前请先获取节点内容，确保理解判断条件\n- 在 reasoning 中解释为何选择此分支（记录推理路径）\n- 可自由使用诊断工具辅助判断，工具调用和 SOP 导航可交替进行",
        "s2_sop_react_resume_v1": "【SOP 排障流程恢复模式】\n正在执行 SOP：《{sop_title}》\n已完成步骤 {completed_steps_count} 步，当前位置节点：{current_node_id}\n{known_variables}\n\n【当前节点：{current_node_title}】\n类型：{current_node_type}\n内容摘要：\n{current_node_content}\n\n【可选分支】\n{current_node_branches}\n\n【工具使用指引】\n1. 使用 get_sop_node(node_id) 获取当前节点或子节点的详细内容\n2. 根据节点判断结果，使用 sop_advance(target_node_id, reasoning) 推进到子节点\n3. 可同时使用诊断工具（acli、SCP 工具）收集证据\n4. 到达 solution 节点时，总结解决方案并完成排障\n\n【幂等性约束 - 重要】\n已完成节点：{completed_nodes_str}\n- 已在 completed_steps 中的节点，不重复执行写操作命令（如 acli_exec 执行 restart/start/stop 等有副作用的命令）\n- 只读命令（如 acli_exec 执行 acli --formatter json vm list 或 acli --formatter json platform info get 等只读命令）可正常调用\n- 若需要重新执行写操作，请先向用户说明原因并获取明确授权\n\n【注意事项】\n- 从当前节点继续执行，不要从头开始\n- 在 reasoning 中解释为何选择此分支\n- 可自由使用诊断工具辅助判断",
        "s3_sop_legacy_v1": "【知识使用规范】\n你有 SOP 排障流程可用，请严格按其步骤顺序执行，在每个判断节点收集证据后再做决策。\n\n【SOP 排障流程 | 来源：{sop_title}】\n{sop_content}",
        "s4_fallback_v1": "【机制推理模式】\n当前知识库中暂未找到与分类 {category_id} 高度匹配 of SOP 或历史案例。\n请基于 HCI 平台架构机制知识进行推理：\n  - 所有推断必须标注【机制推理】\n  - 在回复末尾追加：「如能提供更具体的报错信息，我可以尝试匹配更精确的排障流程」",
        "s5_solution_v1": "【修复操作规范】\n1. 先解释修复原理，让工程师理解每步操作的目的\n2. 每个修复步骤执行前会弹出确认对话框，工程师确认后才执行\n3. 区分「临时修复」和「永久解决方案」，明确标注\n4. 执行后验证：每个修复步骤完成后，立即执行验证命令确认效果\n5. 若修复失败，停止操作并给出人工介入建议\n\n【已确认根因】\n{root_cause}\n\n【推荐修复方案】\n{solution}\n\n⚠️ 重要提示：以下所有操作步骤均需工程师逐步确认后才会执行。",
    }
    if custom_templates:
        templates.update(custom_templates)

    def factory():
        return MockSession(templates)

    return factory
