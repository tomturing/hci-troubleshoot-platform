"""
关键信号提取器（SignalExtractor）

职责：
- 从 KBD/SOP 自然语言文本中提取结构化信号
- 调用 LLM 进行语义解析与类型判别
- 根据判别结果构造对应的派生类实例

@deprecated 统一抽取已迁移至 kb-service 的 extract_signals 路由（signals_json 规范模型，
见 docs/.../关键信号字段级分别抽取.md）。本模块产出的旧 schema（query/keyword/is_failed）
仅供遗留/测试路径使用；新运行时以 KeySignal 基类承载的 acquirer/acquirer_args/produces/
requires/matcher 规范字段为准。
"""

from __future__ import annotations

import json
from typing import Any

from shared.clients import AIAssistantRegistry
from shared.observability.logger import get_logger

from app.tools.signal.base import KeySignal

logger = get_logger("signal-extractor")


class SignalExtractionError(Exception):
    """信号提取异常"""
    pass


class SignalExtractor:
    """
    关键信号提取器

    流程：
    1. 接收 KBD 案例或 SOP 步骤的自然语言文本
    2. 调用 LLM 进行语义解析
    3. 判别信号类别（frontend/backend）
    4. 构造对应的 FrontendSignal 或 BackendSignal 实例
    """

    @classmethod
    def extract_from_text(
        cls,
        text: str,
        *,
        llm_client: AIAssistantRegistry | None = None,
    ) -> KeySignal:
        """
        从自然语言文本提取关键信号

        Args:
            text: KBD 案例或 SOP 步骤的自然语言描述
            llm_client: LLM 客户端（可选，默认使用全局注册表）

        Returns:
            KeySignal 实例（可能是 FrontendSignal 或 BackendSignal）

        Raises:
            SignalExtractionError: 提取失败时抛出
        """
        try:
            # 1. 调用 LLM 解析文本
            signal_json = cls._call_llm_extract(text, llm_client)

            # 2. 根据类别判别并构造对应实例
            signal = KeySignal.from_dict(signal_json)

            # 3. 校验信号参数
            is_valid, error_msg = signal.validate()
            if not is_valid:
                raise SignalExtractionError(f"信号校验失败: {error_msg}")

            logger.info(
                "signal_extracted",
                category=signal.signal_category.value,
                keyword=signal.keyword,
            )

            return signal

        except Exception as e:
            logger.error("signal_extraction_failed", text=text[:100], error=str(e))
            raise SignalExtractionError(f"信号提取失败: {e}") from e

    @classmethod
    def extract_batch_from_text(
        cls,
        text: str,
        *,
        llm_client: AIAssistantRegistry | None = None,
    ) -> list[KeySignal]:
        """
        从自然语言文本批量提取多个信号

        Args:
            text: 包含多个排查步骤的 KBD 案例文本
            llm_client: LLM 客户端（可选）

        Returns:
            KeySignal 列表

        适用于：
        - 一个 KBD 案例包含多个排查步骤
        - SOP 决策树的单个节点包含多个诊断动作
        """
        try:
            # 调用 LLM 批量提取
            signals_json_list = cls._call_llm_extract_batch(text, llm_client)

            signals = []
            for signal_json in signals_json_list:
                try:
                    signal = KeySignal.from_dict(signal_json)
                    is_valid, error_msg = signal.validate()
                    if is_valid:
                        signals.append(signal)
                    else:
                        logger.warning("signal_validation_failed", error=error_msg)
                except Exception as e:
                    logger.warning("signal_parse_failed", error=str(e))
                    continue

            logger.info("signals_batch_extracted", count=len(signals))
            return signals

        except Exception as e:
            logger.error("signals_batch_extraction_failed", error=str(e))
            raise SignalExtractionError(f"批量信号提取失败: {e}") from e

    @classmethod
    def _call_llm_extract(
        cls,
        text: str,
        llm_client: AIAssistantRegistry | None = None,
    ) -> dict[str, Any]:
        """
        调用 LLM 提取单个信号（内部方法）

        使用统一的 KeySignal Schema 作为提取目标
        """
        from app.tools.signal.template import KEY_SIGNAL_EXTRACTION_PROMPT

        if llm_client is None:
            llm_client = AIAssistantRegistry.get_default()

        # 构造 Prompt
        prompt = KEY_SIGNAL_EXTRACTION_PROMPT.format(text=text)

        # 调用 LLM
        response = llm_client.invoke(prompt)

        # 解析 JSON 响应
        try:
            # 尝试提取 JSON 块
            json_str = cls._extract_json_block(response.content)
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise SignalExtractionError(f"LLM 返回非 JSON 格式: {response.content[:200]}") from e

    @classmethod
    def _call_llm_extract_batch(
        cls,
        text: str,
        llm_client: AIAssistantRegistry | None = None,
    ) -> list[dict[str, Any]]:
        """
        调用 LLM 批量提取信号（内部方法）
        """
        from app.tools.signal.template import KEY_SIGNAL_BATCH_EXTRACTION_PROMPT

        if llm_client is None:
            llm_client = AIAssistantRegistry.get_default()

        prompt = KEY_SIGNAL_BATCH_EXTRACTION_PROMPT.format(text=text)
        response = llm_client.invoke(prompt)

        try:
            json_str = cls._extract_json_block(response.content)
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise SignalExtractionError(f"LLM 返回非 JSON 格式: {response.content[:200]}") from e

    @staticmethod
    def _extract_json_block(text: str) -> str:
        """
        从文本中提取 JSON 块

        支持：
        - ```json ... ``` 格式
        - 直接 JSON 格式
        """
        import re

        # 尝试匹配 ```json ... ```
        json_match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if json_match:
            return json_match.group(1)

        # 尝试匹配 ``` ... ```（无 json 标记）
        code_match = re.search(r'```\s*(.*?)\s*```', text, re.DOTALL)
        if code_match:
            return code_match.group(1)

        # 否则直接返回原文
        return text.strip()
