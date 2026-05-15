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
3. 如果参考了知识库中的某个历史案例（KBD-{{id}} 格式标记的案例），在根因结论后追加一行「关联案例：KBD-{{id}}」（{{id}} 替换为实际的数字 ID）
4. 如果证据不足以确定根因，说明还需要什么信息""",

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
        """S0 阶段专用方法论（v3：统一候选确认，禁止高置信度直接确认）"""
        return """【当前阶段：S0 意图识别】

⚠️ S0 是分类任务，不是对话任务。目标是得到用户确认的 1 个 kb_category.code。

工作流程：
1. **全量分析**：综合所有已有信息（客户描述 + 告警日志 + 任务日志），一次性完成推理
   - 提取关键实体（虚拟机名/集群名/存储名/时间点）
   - 识别告警和失败任务中的错误模式
   - 结合实体和错误模式，在分类基准中定位候选

2. **统一展示候选选项（无论置信度高低，一律如此）**：

   - 高置信度（单一候选明显领先）：展示 ① 命中项 + ③ 以上都不是（2 个选项）
   - 中置信度（存在 2 个势均力敌的候选）：展示 ① 主要候选 + ② 次要候选 + ③ 以上都不是（3 个选项）
   - 候选最多 2 个（+固定的③），不得超过 3 个选项
   - **禁止直接在回复末尾写 `已确认故障分类：...` 标记**，该标记只能由后端在用户确认后写入

3. ⛔ 禁止追问：不得输出「请问…」「您能描述…」「是否尝试过…」这类开放性问题
4. ⛔ 禁止硬猜：置信度不足时诚实呈现候选，由用户确认，不可凭感觉臆断分类
5. ⛔ 禁止独自确认：即使 99% 把握，也必须通过 ①②③ 让用户完成确认"""

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
                parts = [f"[{alert.get('level', 'WARNING')}]"]
                if alert.get("time"):
                    parts.append(alert["time"])
                if alert.get("target"):
                    parts.append(f"对象: {alert['target']}")
                if alert.get("type"):
                    parts.append(f"事件: {alert['type']}")
                if alert.get("host"):
                    parts.append(f"主机: {alert['host']}")
                if alert.get("vm"):
                    parts.append(f"VM: {alert['vm']}")
                if alert.get("description"):
                    parts.append(f"描述: {alert['description']}")
                alert_lines.append("- " + " | ".join(parts))
            alert_text = f"""## 最新告警（{len(alert_lines)} 条）
{chr(10).join(alert_lines)}"""

        # 任务日志格式化（最多 10 条）
        task_text = ""
        if task_logs:
            task_lines = []
            for task in task_logs[:10]:
                parts = [f"[{task.get('status', '未知')}]"]
                if task.get("time"):
                    parts.append(task["time"])
                if task.get("type"):
                    parts.append(f"行为: {task['type']}")
                if task.get("host"):
                    parts.append(f"主机: {task['host']}")
                if task.get("vm"):
                    parts.append(f"VM: {task['vm']}")
                if task.get("target"):
                    parts.append(f"对象: {task['target']}")
                if task.get("errcode_tracing"):
                    parts.append(f"错误码: {task['errcode_tracing']}")
                if task.get("trace_id"):
                    parts.append(f"trace_id: {task['trace_id']}")
                if task.get("description"):
                    parts.append(f"描述: {task['description']}")
                task_lines.append("- " + " | ".join(parts))
            task_text = f"""## 近期任务（{len(task_lines)} 条）
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
        """S0 阶段输出格式规范（v3：统一候选确认，无论置信度高低）"""
        return """【输出格式规范】

==== 统一格式（高置信度和中置信度均使用此格式）====
根据 [简要判断依据]，您的问题可能属于以下故障之一，请确认：

① {分类code-1} {分类名-1}
   判断依据：[引用告警/日志原文或描述关键词]，概率估计 ~XX%

② {分类code-2} {分类名-2}（可选，有明确次候选时提供）
   判断依据：[引用具体证据]，概率估计 ~XX%

③ 以上都不是（请补充症状描述）

请回复 ① ② 或 ③ 完成确认。

==== 严格约束 ====
- 候选最多 2 个（①②），加上固定的 ③，总计不超过 3 个选项
- 若只有 1 个候选：只展示 ① 和 ③，不需要 ②
- 每个候选必须附判断依据，引用实际日志或告警内容
- ⛔ 严禁输出 `已确认故障分类：XXX YYY` 格式的直接标记
- ⛔ 不得输出任何开放性问题（"请问..." / "您是否..." / "能描述一下..."）
- ⛔ 不得跳过用户确认步骤，即使置信度极高也必须展示选项"""


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
