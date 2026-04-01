"""
5 段式动态 Prompt 构建器，随诊断阶段变化

取代 conversation_service.py 中的内联 Prompt 常量，
独立成类以便单元测试和维护。

S0 阶段专用：
  - build_s0_prompt(): 构建意图识别 Prompt，注入 198 个分类列表
"""

import re
from typing import Any

from shared.utils.logger import get_logger

logger = get_logger("prompt-builder")

# ─── 各阶段的 Segment 2 内容（诊断方法论细化版）─────────────────────────────
STAGE_METHODOLOGY: dict[str, str] = {
    "S0": """当前阶段：【S0 意图识别】
你的任务：
1. 从客户描述中提取关键实体（虚拟机名/集群名/存储名/时间点）
2. 根据实体信息，结合告警日志和操作日志，判断故障的大方向
3. 提出 1-3 个精准确认问题，不要一次问超过 3 个
4. 参考分类基准，尝试初步定位到技术域（虚拟机/存储/网络/硬件/平台）""",

    "S1": """当前阶段：【S1 故障定位】
已有信息：{known_info}
你的任务：
1. 根据客户回答，确认具体故障类型
2. 尝试精确到分类基准中的叶节点（如：虚拟机开机失败）
3. 如信息不足，继续追问，但每轮不超过 2 个问题""",

    "S2": """当前阶段：【S2 假设生成】
故障分类：{category_path}
你的任务：
1. 列出 2-3 个最可能的根因假设，每个假设给出可能性百分比
2. 说明每个假设的诊断方向（需要检查什么）
3. 优先排查概率最高的假设""",

    "S3": """当前阶段：【S3 验证执行】
故障分类：{category_path}
当前假设：{hypothesis}
你的任务：
1. 按假设概率从高到低，逐一调用诊断工具收集证据
2. 每次工具调用后，根据结果更新假设状态
3. 若当前假设被排除，检验下一个假设
4. 收集到决定性证据后，终止验证，进入 S4""",

    "S4": """当前阶段：【S4 根因确认】
你的任务：
1. 综合所有诊断证据，明确说明根本原因
2. 用"根因确认：XXX" 格式表述
3. 如果证据不足以确定根因，说明还需要什么信息""",

    "S5": """当前阶段：【S5 方案输出】
根因：{root_cause}
你的任务：
1. 输出明确的修复步骤，每步有可执行命令或操作说明
2. 区分"快速恢复方案"（临时解决）和"彻底解决方案"
3. 注明操作风险（是否需要停机/迁移虚拟机）
4. 提示用户执行完成后回复确认""",

    "S6": """当前阶段：【S6 验证闭环】
你的任务：
1. 询问用户是否确认问题已解决
2. 如已解决，说明本次排障的总结（耗时、根因、方案）
3. 如未解决，分析可能原因，考虑是否需要上升支持""",
}

# 各阶段对应的操作描述（用于 Segment 5 的上下文提示）
_STAGE_ACTION_MAP: dict[str, str] = {
    "S0": "意图确认",
    "S1": "故障定位",
    "S2": "假设生成",
    "S3": "诊断验证",
    "S4": "根因确认",
    "S5": "输出方案",
    "S6": "验证闭环",
}

# HCI 核心机制知识（不依赖 RAG，硬编码保底知识）
_MECHANISM_KNOWLEDGE = """【HCI 核心机制知识】
虚拟机开机链路：用户触发 → vtpdaemon → kvm_runner → QEMU/KVM → 存储挂载 → 网络配置
开机失败 4 大方向：① 宿主资源不足（CPU/内存）② 存储不可访问
                  ③ 序列号/授权问题 ④ 平台服务异常（exporter/cfs/prometheus）
诊断首选命令：acli task get -v {vmid} -k '启动虚拟机' → 获取失败原因
快速预诊断：acli alert get -l 10 → 近期告警，acli task get -s failed -l 5 → 近期失败任务"""


class PromptBuilder:
    """根据诊断阶段和上下文动态构建 5 段式 Prompt

    设计原则：
    - 每个 Segment 方法独立，可单独测试
    - 不依赖外部服务，所有输入通过参数注入
    - 知识原子优先注入 diagnostic_step 类型，其次 fix_action
    """

    def build_system_prompt(
        self,
        diagnostic_stage: str,
        knowledge_atoms: list[dict],
        case_context: dict,
        session_state: dict,
    ) -> str:
        """构建完整 system prompt

        参数：
          diagnostic_stage: 当前诊断阶段 S0-S6
          knowledge_atoms: KB 检索结果（知识原子列表）
          case_context: 工单上下文 {case_id, description}
          session_state: 会话状态 {category_l1, category_l2, hypothesis, root_cause 等}
        """
        segments = [
            self._segment_identity(),
            self._segment_methodology(diagnostic_stage, session_state),
            self._segment_mechanism_knowledge(),
            self._segment_knowledge_atoms(knowledge_atoms),
            self._segment_context(case_context, diagnostic_stage),
        ]
        return "\n\n".join(s for s in segments if s)

    def _segment_identity(self) -> str:
        """Segment 1：专家身份定义（固定）"""
        return (
            "你是深信服超融合基础设施（HCI）智能排障专家助手。\n"
            "你掌握完整的 HCI 平台知识：虚拟机生命周期管理、分布式存储 ASAN、\n"
            "vxlan 虚拟网络、IPMI 硬件管理、acli 诊断工具集（完整命令集）。\n"
            "你的目标是协助现场工程师快速、精准地定位和解决 HCI 平台故障。"
        )

    def _segment_methodology(self, stage: str, state: dict) -> str:
        """Segment 2：当前阶段诊断方法论（随 stage 变化）"""
        template = STAGE_METHODOLOGY.get(stage, STAGE_METHODOLOGY["S0"])
        category_parts = [state.get("category_l1"), state.get("category_l2")]
        return template.format(
            known_info=state.get("known_info", "暂无"),
            category_path=" > ".join(p for p in category_parts if p) or "待定位",
            hypothesis=str(state.get("hypothesis", [])),
            root_cause=state.get("root_cause", "待确认"),
        )

    def _segment_mechanism_knowledge(self) -> str:
        """Segment 3：HCI 机制知识（不依赖 RAG）"""
        return _MECHANISM_KNOWLEDGE

    def _segment_knowledge_atoms(self, atoms: list[dict]) -> str:
        """Segment 4：KB 知识原子注入

        优先注入 diagnostic_step 类型，其次 fix_action，最多 5 个。
        """
        if not atoms:
            return (
                "【知识库参考】\n"
                "当前知识库暂无该类型故障的 SOP，将基于 HCI 机制知识进行推理。\n"
                "所有机制推理内容将标注【机制推理】，与知识库匹配内容（【KB参考】）区分。"
            )

        # 按类型优先级排序：diagnostic_step > fix_action > 其余
        _priority = {"diagnostic_step": 0, "fix_action": 1}
        sorted_atoms = sorted(
            atoms, key=lambda a: (_priority.get(a.get("type", ""), 99), 0)
        )

        _type_label = {
            "diagnostic_step": "诊断步骤",
            "fix_action": "解决方案",
            "decision_gate": "判断条件",
            "background": "背景知识",
        }

        atom_texts: list[str] = []
        for atom in sorted_atoms[:5]:
            content = atom.get("content") or {}
            if isinstance(content, str):
                full_text = content[:500]
                commands: list[str] = []
            else:
                full_text = (content.get("full_text") or "")[:500]
                commands = content.get("commands", [])

            atom_type = atom.get("type", "")
            source = atom.get("source_ref") or "知识库"
            label = _type_label.get(atom_type, "参考")

            cmd_text = "\n".join(f"  $ {c}" for c in commands[:3]) if commands else ""
            entry = f"【KB参考 - {label}】[来源: {source}]\n{full_text}"
            if cmd_text:
                entry += f"\n关键命令：\n{cmd_text}"
            atom_texts.append(entry)

        return "【知识库参考资料】\n\n" + "\n\n---\n".join(atom_texts)

    def _segment_context(self, ctx: dict, stage: str) -> str:
        """Segment 5：当前工单上下文"""
        action = _STAGE_ACTION_MAP.get(stage, "下一步")
        return (
            f"【当前工单上下文】\n"
            f"工单 ID：{ctx.get('case_id', '未知')}\n"
            f"客户描述：{ctx.get('description', '（待获取）')}\n"
            f"当前诊断阶段：{stage}\n"
            f"请直接开始{action}"
        )

    # ─── S0 意图识别专用 Prompt 构建方法 ─────────────────────────────────────

    def build_s0_prompt(
        self,
        context_info: dict[str, Any],
        categories_by_domain: dict[str, list[dict]],
        case_context: dict[str, Any],
    ) -> str:
        """
        构建 S0 意图识别阶段的 System Prompt

        S0 阶段的特殊之处：
        1. 不进行 KB/SOP 检索，避免过早锁定到特定案例
        2. 注入完整的 198 个分类列表，让 LLM 进行意图识别
        3. 注入环境信息、告警日志、任务日志等上下文
        4. LLM 输出必须包含「已确认故障分类：{分类code} {分类name}」标记

        参数：
          context_info: 环境信息字典，包含：
            - env_info: 环境基本信息（版本、集群配置等）
            - alert_logs: 最新告警日志（最多 10 条）
            - task_logs: 近期任务日志（最多 5 条失败任务）
          categories_by_domain: 分类列表按域分组，格式：
            {
              "虚拟机": [{"id": "虚拟机-001", "label": "虚拟机创建失败"}, ...],
              "网络": [...],
              ...
            }
          case_context: 工单上下文，包含 case_id 和 description

        返回：
          构建完成的 System Prompt 字符串
        """
        segments = [
            self._segment_identity(),
            self._segment_s0_methodology(),
            self._segment_mechanism_knowledge(),
            self._segment_s0_context_info(context_info),
            self._segment_s0_categories(categories_by_domain),
            self._segment_s0_output_format(),
            self._segment_context(case_context, "S0"),
        ]
        prompt = "\n\n".join(s for s in segments if s)
        logger.debug(
            event="s0_prompt_built",
            total_chars=len(prompt),
            domain_count=len(categories_by_domain),
            category_count=sum(len(cats) for cats in categories_by_domain.values()),
        )
        return prompt

    def _segment_s0_methodology(self) -> str:
        """S0 阶段专用方法论"""
        return """【当前阶段：S0 意图识别】

你的核心任务：根据客户描述、环境信息和系统日志，精准定位故障分类。

工作流程：
1. **实体提取**：从客户描述中提取关键实体
   - 虚拟机名称/ID
   - 集群名称
   - 存储名称
   - 故障发生时间点

2. **日志分析**：结合告警日志和任务日志，识别异常模式
   - 告警日志：关注错误级别告警、重复告警
   - 任务日志：关注失败任务、卡住任务

3. **分类匹配**：根据提取的实体和日志分析结果，在分类基准中找到最匹配的分类
   - 优先匹配叶节点分类（如「虚拟机开机失败」）
   - 若无法精确定位，先确定技术域（虚拟机/存储/网络/硬件/平台）

4. **确认输出**：一旦确认分类，必须输出标准格式的确认标记

注意事项：
- 不要一次问超过 3 个问题
- 如果信息不足以确定分类，先提出精准确认问题
- 禁止猜测分类，必须有足够证据支撑"""

    def _segment_s0_context_info(self, context_info: dict[str, Any]) -> str:
        """注入环境信息、告警日志、任务日志"""
        env_info = context_info.get("env_info", {})
        alert_logs = context_info.get("alert_logs", [])
        task_logs = context_info.get("task_logs", [])

        # 环境信息格式化
        env_text = ""
        if env_info:
            env_text = f"""## 当前环境信息
- HCI 版本：{env_info.get('hci_version', '未知')}
- 集群名称：{env_info.get('cluster_name', '未知')}
- 主机数量：{env_info.get('host_count', '未知')}
- 存储类型：{env_info.get('storage_type', '未知')}
- 网络配置：{env_info.get('network_config', '未知')}"""

        # 告警日志格式化（最多 10 条）
        alert_text = ""
        if alert_logs:
            alert_lines = []
            for alert in alert_logs[:10]:
                alert_lines.append(
                    f"- [{alert.get('level', 'INFO')}] {alert.get('time', '')} {alert.get('content', '')}"
                )
            alert_text = f"""## 最新告警（{len(alert_lines)} 条）
{chr(10).join(alert_lines)}"""

        # 任务日志格式化（最多 5 条失败任务）
        task_text = ""
        if task_logs:
            task_lines = []
            for task in task_logs[:5]:
                task_lines.append(
                    f"- [{task.get('status', 'failed')}] {task.get('time', '')} {task.get('name', '')}: {task.get('error', '')}"
                )
            task_text = f"""## 近期失败任务（{len(task_lines)} 条）
{chr(10).join(task_lines)}"""

        if not (env_text or alert_text or task_text):
            return ""

        return f"""【系统上下文信息】

{env_text}

{alert_text}

{task_text}"""

    def _segment_s0_categories(self, categories_by_domain: dict[str, list[dict]]) -> str:
        """注入 198 个分类列表（按域分组）"""
        if not categories_by_domain:
            return "【故障分类基准】\n分类列表暂未加载，请基于 HCI 机制知识进行推理。"

        # 计算总分类数
        total_count = sum(len(cats) for cats in categories_by_domain.values())

        # 域顺序（按重要性排序）
        domain_order = ["虚拟机", "存储", "网络", "硬件", "平台"]

        sections = []
        for domain in domain_order:
            categories = categories_by_domain.get(domain, [])
            if not categories:
                continue
            # 格式化分类列表
            cat_lines = []
            for cat in categories:
                cat_id = cat.get("id", "")
                cat_label = cat.get("label", "")
                cat_lines.append(f"- {cat_id} {cat_label}")
            sections.append(f"""### {domain}域（{len(categories)}个）
{chr(10).join(cat_lines)}""")

        return f"""【故障分类基准】（共 {total_count} 个）

{chr(10).join(sections)}

**重要提示**：
- 分类编码格式为「域-序号」，如「虚拟机-003」表示「虚拟机开机失败」
- 选择分类时，优先选择最具体的叶节点分类
- 若存在多个可能分类，选择与客户描述最匹配的那个"""

    def _segment_s0_output_format(self) -> str:
        """S0 阶段输出格式要求"""
        return """【输出格式要求】

一旦你确认了故障分类，必须按以下格式输出确认标记：

**已确认故障分类：{分类code} {分类name}**

示例：
- 已确认故障分类：虚拟机-003 虚拟机开机失败
- 已确认故障分类：存储-015 卡慢盘
- 已确认故障分类：网络-007 主机vxlan网络丢包或不通

输出确认标记后，进入下一阶段的故障诊断流程。

如果尚未确认分类：
1. 列出可能的分类候选（最多 3 个）
2. 提出 1-3 个精准确认问题
3. 等待用户回复后再确认"""


# ─── 分类提取正则模式 ──────────────────────────────────────────────────────

# S0 阶段分类确认标记的正则模式
CATEGORY_CONFIRM_PATTERN = re.compile(
    r"已确认故障分类[:：]\s*([\u4e00-\u9fa5]+-\d+)\s+([\u4e00-\u9fa5A-Za-z0-9\u4e00-\u9fa5]+)"
)


def extract_category_from_reply(assistant_reply: str) -> dict[str, str] | None:
    """
    从 LLM 回复中提取已确认的分类信息

    Args:
        assistant_reply: LLM 的完整回复文本

    Returns:
        提取成功时返回 {"code": "虚拟机-003", "name": "虚拟机开机失败"}
        提取失败时返回 None
    """
    match = CATEGORY_CONFIRM_PATTERN.search(assistant_reply)
    if match:
        code = match.group(1)
        name = match.group(2)
        logger.info(
            event="category_extracted",
            message=f"从回复中提取分类：{code} {name}",
            code=code,
            name=name,
        )
        return {"code": code, "name": name}
    return None
