"""
关键信号提取器（SignalExtractor）

职责：
- 从 KBD/SOP 自然语言文本中提取结构化信号
- 调用 LLM 进行语义解析与类型判别
- 根据判别结果构造对应的派生类实例

Prompt 来源（热加载）：
- 关键信号抽取 Prompt 统一由 system_prompt 表管理（stage='SIG'），
  通过 shared.utils.prompt_loader.StrictPromptLoader 在每次请求时按 {text}
  占位符强校验加载。管理员在 admin-ui 修改 Prompt 后，下一次抽取即生效，
  无需重启服务。
- 本模块的 SignalExtractor 不再硬编码 Prompt，只负责：渲染占位符 → 调用 LLM
  → 解析 JSON → 构造 KeySignal 实例。
"""

from __future__ import annotations

import json
import re
from typing import Any

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
    2. 渲染 Prompt 模板（由调用方从 DB 加载，含 {text} 占位符）
    3. 调用 LLM 进行语义解析
    4. 判别信号类别（frontend/backend）
    5. 构造对应的 FrontendSignal 或 BackendSignal 实例
    """

    # system_prompt 表中的模板名称（由 seed SQL 预置，管理员可在 admin-ui 维护）
    PROMPT_NAME = "kbd_signal_extract_v1"
    BATCH_PROMPT_NAME = "kbd_signal_batch_extract_v1"

    @classmethod
    async def extract_from_text(
        cls,
        text: str,
        *,
        prompt_template: str,
        client: Any,
    ) -> KeySignal:
        """
        从自然语言文本提取关键信号（单条）

        Args:
            text: KBD 案例或 SOP 步骤的自然语言描述
            prompt_template: 从 system_prompt 表加载的模板（含 {text} 占位符）
            client: LLM 客户端（AIAssistantRegistry.get_client() 返回，需支持 async invoke）

        Returns:
            KeySignal 实例（FrontendSignal 或 BackendSignal）

        Raises:
            SignalExtractionError: 提取失败时抛出
        """
        try:
            signal_json = await cls._call_llm_extract(text, prompt_template, client)
            signal = KeySignal.from_dict(signal_json)
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
    async def extract_batch_from_text(
        cls,
        text: str,
        *,
        prompt_template: str,
        client: Any,
    ) -> list[KeySignal]:
        """
        从自然语言文本批量提取多个信号

        Args:
            text: 包含多个排查步骤的 KBD 案例文本
            prompt_template: 批量抽取模板（含 {text} 占位符）
            client: LLM 客户端

        Returns:
            KeySignal 列表
        """
        try:
            signals_json_list = await cls._call_llm_extract_batch(text, prompt_template, client)
            signals: list[KeySignal] = []
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
    def _render_prompt(cls, prompt_template: str, text: str) -> str:
        """
        安全渲染 Prompt：先解除双花括号转义（保留 {text} 单花括号），
        再替换 {text} 为用户文本（用户文本中的花括号保持原样，避免 .format 报错）。
        """
        return prompt_template.replace("{{", "{").replace("}}", "}").replace("{text}", text)

    @classmethod
    async def _call_llm_extract(
        cls,
        text: str,
        prompt_template: str,
        client: Any,
    ) -> dict[str, Any]:
        """调用 LLM 提取单个信号（内部方法）"""
        prompt = cls._render_prompt(prompt_template, text)
        response = await client.invoke(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        try:
            json_str = cls._extract_json_block(response.content or "")
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise SignalExtractionError(
                f"LLM 返回非 JSON 格式: {(response.content or '')[:200]}"
            ) from e

    @classmethod
    async def _call_llm_extract_batch(
        cls,
        text: str,
        prompt_template: str,
        client: Any,
    ) -> list[dict[str, Any]]:
        """调用 LLM 批量提取信号（内部方法）"""
        prompt = cls._render_prompt(prompt_template, text)
        response = await client.invoke(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        try:
            json_str = cls._extract_json_block(response.content or "")
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise SignalExtractionError(
                f"LLM 返回非 JSON 格式: {(response.content or '')[:200]}"
            ) from e

    @staticmethod
    def _extract_json_block(text: str) -> str:
        """
        从文本中提取 JSON 块

        支持：
        - ```json ... ``` 格式
        - 直接 JSON 格式
        """
        json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if json_match:
            return json_match.group(1)
        code_match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
        if code_match:
            return code_match.group(1)
        return text.strip()
