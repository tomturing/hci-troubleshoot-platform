"""
HallucinationDetector: 轻量级反幻觉规则引擎 (T3-4)

职责：
  - 零 LLM 调用开销，全规则正则引擎进行幻觉检测，耗时 < 100ms
  - phantom_tool_reference: 检查 LLM 输出是否引用了当前会话中未实际执行的工具
  - overconfident_claim: 检查是否包含强事实声明但缺乏不确定性修饰词
  - ungrounded_number: 检查数字事实（百分比、GB、ms 等）是否在工具输出中可找到来源
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("hallucination-detector")


class HallucinationDetector:
    """轻量级幻觉检测器"""

    def __init__(self, tool_registry: dict[str, Any] | None = None) -> None:
        # 工具名称必须来自当前运行时 registry 快照；registry 缺失时跳过工具引用检测，避免内置清单漂移。
        self._registered_tools = set(tool_registry.keys()) if tool_registry else set()

    def detect(
        self,
        llm_text: str,
        executed_tools: list[str],
        tool_outputs: list[str],
    ) -> dict[str, Any]:
        """检测 LLM 文本中的幻觉项

        Args:
            llm_text: LLM 输出的 final text
            executed_tools: 当前会话已执行的工具名称列表
            tool_outputs: 对应工具执行结果的文本列表

        Returns:
            结构化检测报告:
            {
                "has_hallucination": bool,
                "phantom_tools": list[str],
                "overconfident_claims": list[str],
                "ungrounded_numbers": list[str],
                "reasons": list[str]
            }
        """
        phantom_tools = self._check_phantom_tools(llm_text, executed_tools)
        overconfident_claims = self._check_overconfident_claims(llm_text)
        ungrounded_numbers = self._check_ungrounded_numbers(llm_text, tool_outputs)

        reasons = []
        if phantom_tools:
            reasons.append(f"引用了未执行的工具: {', '.join(phantom_tools)}")
        if overconfident_claims:
            reasons.append(f"强事实声明缺乏不确定修饰词: {', '.join(overconfident_claims)}")
        if ungrounded_numbers:
            reasons.append(f"未找到数据来源的数字/百分比: {', '.join(ungrounded_numbers)}")

        has_hallucination = len(reasons) > 0

        return {
            "has_hallucination": has_hallucination,
            "phantom_tools": phantom_tools,
            "overconfident_claims": overconfident_claims,
            "ungrounded_numbers": ungrounded_numbers,
            "reasons": reasons,
        }

    def _check_phantom_tools(self, text: str, executed_tools: list[str]) -> list[str]:
        """检查未执行工具的虚假引用"""
        if not self._registered_tools:
            logger.warning("工具 registry 为空，跳过 phantom_tool_reference 检测")
            return []

        phantom = []
        executed_set = set(executed_tools)

        # 匹配文本中出现的所有工具名称
        for tool in self._registered_tools:
            if tool in text and tool not in executed_set:
                # 检查是否以"引用其结果/执行"的形式提及
                # 如：根据 acli_vm_list 的输出、acli_vm_list 显示、执行了 acli_vm_list
                patterns = [
                    f"根据\\s*{tool}",
                    f"{tool}\\s*(?:的结果|输出|显示|表明)",
                    f"(?:执行|运行|调用|使用)\\s*{tool}"
                ]
                if any(re.search(pat, text) for pat in patterns):
                    phantom.append(tool)
        return phantom

    def _check_overconfident_claims(self, text: str) -> list[str]:
        """检查过度自信的强事实断言"""
        claims = []
        # 匹配断言句式
        assertion_patterns = [
            r"已确认是[a-zA-Z0-9_\u4e00-\u9fa5]+",
            r"确认是[a-zA-Z0-9_\u4e00-\u9fa5]+",
            r"故障原因就是[a-zA-Z0-9_\u4e00-\u9fa5]+",
            r"绝对是[a-zA-Z0-9_\u4e00-\u9fa5]+",
            r"必然是[a-zA-Z0-9_\u4e00-\u9fa5]+",
            r"已定位到根因"
        ]

        # 不确定性修饰词
        uncertainty_words = ["可能", "疑似", "推测", "待验证", "需验证", "大概", "大概率", "预计"]

        for pat in assertion_patterns:
            matches = re.findall(pat, text)
            if matches and not any(word in text for word in uncertainty_words):
                claims.extend(matches)

        return list(set(claims))

    def _check_ungrounded_numbers(self, text: str, tool_outputs: list[str]) -> list[str]:
        """检查是否有数字事实无法在工具输出中匹配到"""
        ungrounded = []
        # 合并所有工具输出
        combined_outputs = "\n".join(tool_outputs)

        # 提取百分比和带单位的数值，例如: 95.5%, 10GB, 250ms, 12.5MB/s, 5秒
        unit_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*(%|GB|MB|ms|KB|s|sec|秒|字节|MB/s)', re.IGNORECASE)
        # 提取高精度的浮点数（如 0.95, 12.34）
        decimal_pattern = re.compile(r'\b(\d+\.\d+)\b')

        found_patterns: set[str] = set()

        for match in unit_pattern.finditer(text):
            found_patterns.add(match.group(0))

        for match in decimal_pattern.finditer(text):
            # 排除 1.0, 0.0, 2.0 等过于通用的值以防假阳性
            val = match.group(1)
            if val not in ("1.0", "0.0", "2.0", "3.0"):
                found_patterns.add(val)

        for pat in found_patterns:
            # 去除两端空格和可能的多余字符进行匹配
            cleaned_pat = pat.strip()
            # 从数字部分和单位进行查找验证
            if cleaned_pat not in combined_outputs:
                # 允许极度通用的变量值，如 context 注入的值
                # 如果这个数值完全无法在任何工具的输出中搜索到，则被判定为 ungrounded
                ungrounded.append(pat)

        return ungrounded
