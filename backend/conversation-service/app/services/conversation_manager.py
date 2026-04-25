"""
会话诊断状态管理器：维护 S0-S6 状态转换

设计原则：
- 无状态纯函数，所有输入/输出均通过参数传递，便于单元测试
- 转换优先保守：宁可不转换，不要误转换
- 分析 GLM 回复文本以检测阶段推进信号

S0 阶段增强：
- extract_category_from_reply(): 从 LLM 回复中提取「已确认故障分类」标记
- detect_stage_transition(): S0→S1 转换时返回提取的分类信息

S6 阶段处理（v6.3 新增）：
- build_pending_resolution(): 构造 S6 等待快照，写入 conversation.pending_resolution
- handle_resolution_choice(): 处理用户 A/B/C 选择，返回需执行的动作
"""

import re
from datetime import UTC, datetime
from typing import Literal

from shared.utils.logger import get_logger

logger = get_logger("conversation-manager")

# ─── 阶段转换触发词（正则模式，匹配 GLM 回复文本）────────────────────────────
# key: (当前阶段, 下一阶段)
# value: 触发转换的正则模式列表（命中任一即触发）
STAGE_TRIGGERS: dict[tuple[str, str], list[str]] = {
    ("S0", "S1"): [
        # 原有触发词（保留兼容）
        r"确认(?:您?的|一下)?(?:故障|问题|情况).*(?:是|为)",
        r"(?:故障|问题)(?:已|初步)?(?:定位|确认)",
        r"根据.*(?:回答|描述).*(?:判断|确定).*(?:故障|问题)",
        # 新增：匹配候选被用户确认后，后端发出的推进语句
        r"好的，确认故障分类为",
        r"已收到您的确认",
        r"进入.*(?:故障定位|S1)",
        r"开始.*(?:故障定位|定位分析)",
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

# T8: S4 根因确认中 AI 输出「关联案例：KBD-{id}」的提取正则
_KBD_RELATED_PATTERN = re.compile(r"关联案例[:：]\s*KBD-(\d+)")

# S0 阶段候选选择正则：匹配用户输入 ①②③ 或 1/2/3（带可选后缀）
_CANDIDATE_SELECT_PATTERN = re.compile(
    r"^[\s\u3000]*([①②③]|[1-3](?:[\.、\s]|$))"
)

# S0 候选确认轮数上限（超过后触发兜底）
S0_MAX_CANDIDATE_ROUNDS: int = 2


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

    def extract_resolved_kbd(self, assistant_reply: str) -> int | None:
        """
        从 S4 AI 回复中提取「关联案例：KBD-{id}」标记。

        AI 在 S4 确认根因时，若参考了 KBD 案例，会输出「关联案例：KBD-{id}」。
        本方法解析该标记，返回 kbd_entry.id（整数），或 None（表示新问题未收录）。

        Args:
            assistant_reply: S4 阶段 AI 完整回复文本

        Returns:
            int: kbd_entry.id；None: 未提取到（新问题或 AI 未引用 KBD）
        """
        match = _KBD_RELATED_PATTERN.search(assistant_reply)
        if match:
            kbd_id = int(match.group(1))
            logger.info(
                event="s4_kbd_extracted",
                message=f"S4 阶段提取到关联 KBD：{kbd_id}",
                kbd_id=kbd_id,
            )
            return kbd_id
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

    # ─── S0 候选确认模式 (v2) ────────────────────────────────────────────────

    def parse_candidate_selection(self, user_reply: str) -> int | None:
        """
        解析用户在 S0 候选选项中的选择。

        支持格式：
          ① ② ③（圆圈数字）
          1 / 2 / 3（阿拉伯数字 + 可选标点/空格/行尾）

        Args:
            user_reply: 用户的回复文本（逐行扫描取第一个匹配）

        Returns:
            int 1 / 2 / 3，或 None（未匹配，视为自然语言补充描述）
        """
        _circle_map = {"①": 1, "②": 2, "③": 3}
        for line in user_reply.splitlines():
            line = line.strip()
            m = _CANDIDATE_SELECT_PATTERN.match(line)
            if m:
                token = m.group(1).strip()
                if token in _circle_map:
                    return _circle_map[token]
                # 阿拉伯数字
                digit = token.rstrip(".、 \t")
                if digit.isdigit():
                    n = int(digit)
                    if 1 <= n <= 3:
                        return n
        return None

    def resolve_candidate_category(
        self,
        selection: int,
        candidates: list[dict[str, str]],
    ) -> dict[str, str] | None:
        """
        将用户选择序号（1/2）映射到对应的 category_info。

        选择 3（"以上都不是"）返回 None，由调用方决定是否继续追问或触发失败兜底。

        Args:
            selection: parse_candidate_selection() 返回的序号（1/2/3）
            candidates: 当前轮次 LLM 给出的候选列表，
                        格式：[{"code": "虚拟机-003", "name": "虚拟机开机失败"}, ...]

        Returns:
            dict {"code": ..., "name": ...} 或 None
        """
        if selection == 3:
            logger.info(event="s0_candidate_none", message="用户选 ③：以上都不是")
            return None
        idx = selection - 1  # 0-based
        if 0 <= idx < len(candidates):
            chosen = candidates[idx]
            logger.info(
                event="s0_candidate_selected",
                message=f"用户选 {'①②'[idx]}：{chosen.get('code')} {chosen.get('name')}",
                code=chosen.get("code"),
                name=chosen.get("name"),
            )
            return {"code": chosen.get("code", ""), "name": chosen.get("name", "")}
        return None

    @staticmethod
    def should_trigger_s0_failure(s0_candidate_rounds: int) -> bool:
        """
        判断 S0 是否应触发失败兜底。

        超过 S0_MAX_CANDIDATE_ROUNDS 轮候选确认仍未收敛，
        说明 AI 无法胜任该问题的分类，应快速移交人工。

        Args:
            s0_candidate_rounds: 已进行的候选确认轮次数

        Returns:
            True 表示应触发兜底
        """
        return s0_candidate_rounds >= S0_MAX_CANDIDATE_ROUNDS

    def build_pending_resolution(self) -> dict:
        """
        构造 S6 完成后的等待快照（写入 conversation.pending_resolution）。

        格式符合 schema_design.json 规范：
          {"stage": "S6", "sent_at": "...", "options": ["A", "B", "C"]}

        调用时机：AI 在 S6 阶段完成 VM 验证后，向用户推送三选项 SSE 事件时同步写入 DB。

        Returns:
            dict: pending_resolution JSONB 快照
        """
        return {
            "stage": "S6",
            "sent_at": datetime.now(UTC).isoformat(),
            "options": ["A", "B", "C"],
        }

    def handle_resolution_choice(
        self,
        choice: Literal["A", "B", "C"],
    ) -> dict:
        """
        处理用户在 S6 三选项中的选择，返回需要执行的动作描述。

        无状态纯函数：不直接操作数据库，只返回动作描述，由调用方实现具体数据库操作。

        Args:
            choice: 用户的选择，"A"/"B"/"C" 之一

        Returns:
            dict，包含以下字段：
              - action: str，动作类型（"resolve" / "retry_s1" / "escalate"）
              - case_status: str | None，需要更新 case.status 的目标值（None 表示不更新）
              - close_reason: str | None，需要更新 case.close_reason 的值
              - new_stage: str | None，conversation.diagnostic_stage 的新值（None=不变）
              - archive_diagnostic_items: bool，是否需要 batch UPDATE 旧 diagnostic_item 为 archived
              - clear_pending_resolution: bool，是否需要清空 pending_resolution

        Raises:
            ValueError: choice 不是 "A"/"B"/"C" 时
        """
        if choice not in ("A", "B", "C"):
            raise ValueError(f"无效的 S6 选择：{choice!r}，必须是 'A'、'B' 或 'C'")

        if choice == "A":
            # 用户选 A：问题已解决
            # → case.status = resolved（等待 Pod 回收后变 closed）
            logger.info(event="s6_choice_a", message="用户选 A：问题已解决，转 resolved")
            return {
                "action": "resolve",
                "case_status": "resolved",
                "close_reason": None,
                "new_stage": None,
                "archive_diagnostic_items": False,
                "clear_pending_resolution": True,
            }
        elif choice == "B":
            # 用户选 B：未解决，需要重新诊断
            # → diagnostic_stage 回退 S1，旧 diagnostic_item 全部 archived
            # → case.status 不变（仍为 confirmed，诊断仍在进行）
            logger.info(event="s6_choice_b", message="用户选 B：未解决回退 S1，archive 旧诊断结论")
            return {
                "action": "retry_s1",
                "case_status": None,
                "close_reason": None,
                "new_stage": "S1",
                "archive_diagnostic_items": True,
                "clear_pending_resolution": True,
            }
        else:  # C
            # 用户选 C：升级人工
            # → case.status = in_progress，close_reason = escalated
            logger.info(event="s6_choice_c", message="用户选 C：升级人工，转 in_progress")
            return {
                "action": "escalate",
                "case_status": "in_progress",
                "close_reason": "escalated",
                "new_stage": None,
                "archive_diagnostic_items": False,
                "clear_pending_resolution": True,
            }
