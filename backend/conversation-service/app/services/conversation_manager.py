"""
会话诊断状态管理器：维护 S0-S6 状态转换

设计原则：
- 无状态纯函数，所有输入/输出均通过参数传递，便于单元测试
- 转换优先保守：宁可不转换，不要误转换
- 分析 GLM 回复文本以检测阶段推进信号

S0 阶段增强：
- extract_category_from_reply(): 从 LLM 回复中提取「已确认故障分类」标记
- detect_stage_transition(): S0→S1 转换时返回提取的分类信息
"""

import re
from typing import Any

from shared.utils.logger import get_logger

logger = get_logger("conversation-manager")

# ─── 阶段转换触发词（正则模式，匹配 GLM 回复文本）────────────────────────────
# key: (当前阶段, 下一阶段)
# value: 触发转换的正则模式列表（命中任一即触发）
STAGE_TRIGGERS: dict[tuple[str, str], list[str]] = {
    ("S0", "S1"): [
        r"确认(?:您?的|一下)?(?:故障|问题|情况).*(?:是|为)",
        r"(?:故障|问题)(?:已|初步)?(?:定位|确认)",
        r"根据.*(?:回答|描述).*(?:判断|确定).*(?:故障|问题)",
    ],
    ("S1", "S2"): [
        r"(?:故障|问题)(?:类型|分类).*(?:已|明确|确定)",
        r"开始(?:分析|生成)(?:可能的)?根因",
        r"(?:以下是|提出|给出).*(?:假设|可能原因)",
        r"根因假设",
    ],
    ("S2", "S3"): [
        r"(?:开始|执行|进行)诊断(?:验证|检查)?",
        r"(?:检查|查看|获取).*(?:状态|日志|进程|任务)",
        r"(?:运行|执行|调用).*(?:命令|工具|acli)",
    ],
    ("S3", "S4"): [
        r"根据(?:以上|上述|收集的)?(?:结果|证据|信息)",
        r"诊断(?:完成|结束|结果)[:：]",
        r"(?:证据|结果)(?:表明|显示|指向)",
        r"已(?:获取|收集)足够证据",
    ],
    ("S4", "S5"): [
        r"根因确认[:：]",
        r"(?:建议|推荐)(?:以下)?(?:解决|修复)方案",
        r"确定(?:根本原因|根因)(?:为|是)",
    ],
    ("S5", "S6"): [
        r"(?:请|麻烦).*(?:执行|尝试|操作).*(?:后|完成后|之后).*(?:反馈|告知|确认)",
        r"执行以上步骤后",
        r"完成后.*(?:告诉我|反馈|确认)(?:是否)?(?:成功|解决)",
    ],
}

# 阶段顺序（用于边界校验）
STAGE_ORDER: list[str] = ["S0", "S1", "S2", "S3", "S4", "S5", "S6"]

# 用户重置关键词（触发回到 S0）
_RESET_KEYWORDS: list[str] = [
    "重新来",
    "重新开始",
    "换个问题",
    "另一个问题",
    "我要问",
    "重新描述",
    "换一下",
]

# S0 阶段分类确认标记的正则模式
_CATEGORY_CONFIRM_PATTERN = re.compile(
    r"已确认故障分类[:：]\s*([\u4e00-\u9fa5]+-\d+)\s+([\u4e00-\u9fa5A-Za-z0-9\u4e00-\u9fa5]+)"
)


class ConversationManager:
    """管理诊断状态转换（无状态，所有数据通过参数传入）

    S0 阶段增强：
    - detect_stage_transition_with_category(): 检测阶段转换并提取分类信息
    - extract_category(): 从 LLM 回复中提取「已确认故障分类」标记
    """

    def detect_stage_transition(
        self,
        current_stage: str,
        assistant_reply: str,
        user_message: str,
    ) -> str | None:
        """分析 GLM 回复，判断是否应进行阶段转换。

        Args:
            current_stage: 当前阶段（如 "S0"）
            assistant_reply: AI 助手本轮完整回复文本
            user_message: 用户本轮输入文本

        Returns:
            新阶段字符串（如 "S1"），或 None 表示不转换
        """
        # 检查用户是否请求重置
        if self._should_reset(user_message):
            if current_stage != "S0":
                return "S0"
            return None

        # 校验当前阶段合法性
        if current_stage not in STAGE_ORDER:
            return None

        current_idx = STAGE_ORDER.index(current_stage)
        # 已到最后阶段，无需继续推进
        if current_idx >= len(STAGE_ORDER) - 1:
            return None

        next_stage = STAGE_ORDER[current_idx + 1]
        transition_key = (current_stage, next_stage)

        if transition_key not in STAGE_TRIGGERS:
            return None

        for pattern in STAGE_TRIGGERS[transition_key]:
            if re.search(pattern, assistant_reply):
                return next_stage

        return None

    def detect_stage_transition_with_category(
        self,
        current_stage: str,
        assistant_reply: str,
        user_message: str,
    ) -> tuple[str | None, dict[str, str] | None]:
        """
        分析 GLM 回复，判断是否应进行阶段转换，并提取分类信息。

        这是 detect_stage_transition 的增强版本，专门用于 S0 阶段：
        - 检测阶段转换
        - 从 LLM 回复中提取「已确认故障分类：{code} {name}」标记

        Args:
            current_stage: 当前阶段（如 "S0"）
            assistant_reply: AI 助手本轮完整回复文本
            user_message: 用户本轮输入文本

        Returns:
            tuple:
              - [0] 新阶段字符串（如 "S1"），或 None 表示不转换
              - [1] 分类信息字典 {"code": "虚拟机-003", "name": "虚拟机开机失败"}，
                   或 None 表示未提取到分类
        """
        # 先执行标准阶段转换检测
        new_stage = self.detect_stage_transition(current_stage, assistant_reply, user_message)

        # 提取分类信息（仅在 S0 阶段有意义）
        category_info = None
        if current_stage == "S0":
            category_info = self.extract_category(assistant_reply)
            if category_info:
                logger.info(
                    event="s0_category_extracted",
                    message=f"S0 分类提取成功：{category_info['code']} {category_info['name']}",
                    code=category_info["code"],
                    name=category_info["name"],
                )

        return new_stage, category_info

    def extract_category(self, assistant_reply: str) -> dict[str, str] | None:
        """
        从 LLM 回复中提取已确认的分类信息。

        匹配模式：「已确认故障分类：{分类code} {分类name}」
        示例：「已确认故障分类：虚拟机-003 虚拟机开机失败」

        Args:
            assistant_reply: LLM 的完整回复文本

        Returns:
            提取成功时返回 {"code": "虚拟机-003", "name": "虚拟机开机失败"}
            提取失败时返回 None
        """
        match = _CATEGORY_CONFIRM_PATTERN.search(assistant_reply)
        if match:
            code = match.group(1)
            name = match.group(2)
            return {"code": code, "name": name}
        return None

    def _should_reset(self, user_message: str) -> bool:
        """检查用户是否请求重置诊断流程"""
        return any(kw in user_message for kw in _RESET_KEYWORDS)

    def get_stage_label(self, stage: str) -> str:
        """返回阶段的中文标签，用于日志/展示"""
        _labels = {
            "S0": "S0-意图识别",
            "S1": "S1-故障定位",
            "S2": "S2-假设生成",
            "S3": "S3-验证执行",
            "S4": "S4-根因确认",
            "S5": "S5-方案输出",
            "S6": "S6-验证闭环",
        }
        return _labels.get(stage, stage)
