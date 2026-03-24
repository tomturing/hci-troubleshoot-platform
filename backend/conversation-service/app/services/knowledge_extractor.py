"""
知识提炼服务 — 从成功闭环的排障对话中提炼可复用知识原子候选

触发时机：对话状态机进入 S6（验证闭环）时异步触发
写入目标：kb-service /api/v1/atoms（verified=false，待人工审核）
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import httpx
from shared.utils.logger import get_logger

if TYPE_CHECKING:
    from app.core.glm_client import GLMClient

logger = get_logger("conversation-knowledge-extractor")

# 知识提炼 Prompt（专注于 HCI 领域诊断知识结构化提取）
_EXTRACTION_PROMPT = """你是一位 HCI（超融合基础设施）知识管理专家。
以下是一次完整的 HCI 故障排障对话记录（JSON 格式）。
请从中提炼出可复用的诊断知识，输出 JSON 格式的知识原子列表。

每个知识原子格式：
{{
  "type": "diagnostic_step|fix_action|decision_gate",
  "trigger": {{
    "stage": "S2|S3|S4",
    "category_id": "（故障分类 ID，如 vm_power_failure）",
    "conditions": ["触发该步骤的条件描述"]
  }},
  "content": {{
    "full_text": "完整诊断步骤描述（可被其他工程师直接参考执行）",
    "commands": ["相关 acli/系统命令列表"]
  }},
  "source_ref": "{source_ref}"
}}

提炼规则：
1. 只提炼在本次对话中被**验证有效**的诊断步骤或修复操作
2. 每条知识原子要独立完整，可以脱离上下文单独理解执行
3. commands 只包含真实调用过的命令，不要杜撰
4. 最多输出 5 条，选取价值最高的

对话记录：
{conversation_summary}

仅输出 JSON 数组，不要有其他文字。输出格式示例：
[{{"type": "diagnostic_step", "trigger": {{}}, "content": {{}}, "source_ref": ""}}]
"""

# KB service 写入超时（秒）
_KB_WRITE_TIMEOUT = 5.0


class KnowledgeExtractor:
    """从成功排障会话中自动提炼知识原子候选"""

    def __init__(self, glm_client: "GLMClient", kb_service_url: str) -> None:
        self.glm = glm_client
        # 去除末尾斜线，确保 URL 拼接正确
        self._kb_url = kb_service_url.rstrip("/")

    @classmethod
    def from_env(cls, glm_client: "GLMClient") -> "KnowledgeExtractor":
        """从环境变量创建实例。

        必需：KB_SERVICE_URL — kb-service 内部地址，如 http://kb-service:8004
        """
        import os

        kb_url = os.environ.get("KB_SERVICE_URL", "http://kb-service:8004")
        return cls(glm_client=glm_client, kb_service_url=kb_url)

    async def extract_from_session(
        self,
        session_id: str,
        conversation_messages: list[dict],
        tool_audit_logs: list[dict],
        category_id: str,
    ) -> list[str]:
        """从会话中提炼知识原子候选，返回创建的原子 ID 列表。

        只在 S6 阶段（对话成功闭环）触发，不阻塞对话响应流。

        Args:
            session_id: 会话/对话 ID（作为 source_ref 记录凭证）
            conversation_messages: 历史消息列表，格式 [{"role": "user|assistant", "content": "..."}]
            tool_audit_logs: 工具调用日志，格式 [{"tool_name": ..., "tool_args": ..., "result": ...}]
            category_id: 故障分类 ID（来自工单 case.category）

        Returns:
            成功写入的知识原子 ID 列表（空列表表示全部失败或无有效候选）
        """
        # 1. 构建对话摘要（限制 token 数量）
        summary = self._build_summary(conversation_messages, tool_audit_logs)

        # 2. 调用 GLM 提炼知识原子
        prompt = _EXTRACTION_PROMPT.format(
            source_ref=session_id,
            conversation_summary=summary,
        )
        try:
            response = await self.glm.chat([{"role": "user", "content": prompt}])
            raw = response.content or "[]"
        except Exception as exc:
            logger.warning(
                event="knowledge_extraction_llm_error",
                message=f"GLM 调用失败，跳过知识提炼: {exc}",
                session_id=session_id,
            )
            return []

        # 3. 解析 JSON（使用 GLMClient 内置的容错解析）
        candidates = self.glm._safe_parse_json(raw, f"extract-{session_id}")
        if not isinstance(candidates, list):
            logger.warning(
                event="knowledge_extraction_parse_error",
                message="知识提炼结果非列表，跳过写入",
                session_id=session_id,
                raw_preview=raw[:200],
            )
            return []

        # 4. 逐条写入知识库（POST /api/v1/atoms，最多 5 条）
        created_ids: list[str] = []
        async with httpx.AsyncClient(timeout=_KB_WRITE_TIMEOUT) as client:
            for candidate in candidates[:5]:
                if not isinstance(candidate, dict):
                    continue
                atom_id = f"ka-{uuid.uuid4().hex[:12]}"
                try:
                    resp = await client.post(
                        f"{self._kb_url}/api/v1/atoms",
                        json={
                            "id": atom_id,
                            "atom_type": candidate.get("type", "diagnostic_step"),
                            "category_id": category_id or "",
                            "trigger_json": candidate.get("trigger", {}),
                            "content_json": candidate.get("content", {}),
                            "source_type": "session",
                            "source_ref": session_id,
                            "verified": False,      # 待人工审核
                            "confidence": 0.70,     # 机器生成初始置信度
                        },
                    )
                    if resp.status_code in (200, 201):
                        created_ids.append(atom_id)
                        logger.info(
                            event="knowledge_atom_created",
                            atom_id=atom_id,
                            atom_type=candidate.get("type"),
                            session_id=session_id,
                        )
                    else:
                        logger.warning(
                            event="knowledge_atom_write_failed",
                            atom_id=atom_id,
                            status_code=resp.status_code,
                            session_id=session_id,
                        )
                except Exception as exc:
                    logger.error(
                        event="knowledge_atom_write_error",
                        message=str(exc),
                        atom_id=atom_id,
                        session_id=session_id,
                    )

        logger.info(
            event="knowledge_extraction_done",
            message=f"会话 {session_id} 提炼了 {len(created_ids)}/{len(candidates[:5])} 个知识候选",
            session_id=session_id,
            created=len(created_ids),
            candidates=len(candidates),
            category_id=category_id,
        )
        return created_ids

    def _build_summary(self, messages: list[dict], audit_logs: list[dict]) -> str:
        """构建对话摘要（限制 token 数量，优先保留最近内容）"""
        parts: list[str] = []

        # 取最近 20 条消息，每条内容截断到 300 字
        for msg in messages[-20:]:
            role = msg.get("role", "unknown")
            content = str(msg.get("content", ""))[:300]
            parts.append(f"[{role}] {content}")

        # 追加工具调用记录（最多 10 条，结果截断到 200 字）
        if audit_logs:
            parts.append("\n--- 工具调用记录 ---")
            for log in audit_logs[:10]:
                parts.append(
                    f"工具: {log.get('tool_name', '?')} | "
                    f"参数: {str(log.get('tool_args', {}))[:100]} | "
                    f"结果: {str(log.get('result', ''))[:200]}"
                )

        return "\n".join(parts)
