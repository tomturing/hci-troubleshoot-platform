"""
数据库动态 Skill 运行器。

Skill 的权威定义来自 skill_definition 表。这里仅保留通用执行内核：
加载 active Skill、组织输入上下文、调用 LLM、解析结构化输出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from shared.clients import AIAssistantRegistry
from shared.dynamic_resource.adapters import skill_resource_payload
from shared.dynamic_resource.loader import DynamicResourceLoader, snapshot_revision_metadata
from shared.dynamic_resource.models import UsageRecord
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.models.skill_definition import SkillDefinitionORM
from shared.models.tool_definition import ToolDefinitionORM
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import select

logger = get_logger("skills.dynamic-runner")


class DynamicSkillError(RuntimeError):
    """动态 Skill 执行失败。"""


class SkillNotFoundError(DynamicSkillError):
    """Skill 不存在或未启用。"""


@dataclass(frozen=True)
class SkillSnapshot:
    """运行时 Skill 快照。"""

    id: int
    skill_name: str
    display_name: str | None
    description: str
    instructions_md: str
    allowed_tools: str | None
    updated_at: str | None = None
    resource_revision: dict[str, Any] | None = None


def build_skill_name_candidates(skill_name: str) -> list[str]:
    """生成兼容候选名，解决历史 snake_case 与标准 kebab-case 的过渡问题。"""
    normalized = skill_name.strip()
    kebab = normalized.replace("_", "-")
    candidates = [normalized]
    if kebab != normalized:
        candidates.append(kebab)
    if not kebab.startswith("hci-"):
        candidates.append(f"hci-{kebab}")

    result: list[str] = []
    for item in candidates:
        if item and item not in result:
            result.append(item)
    return result


def extract_output_value(result: Any, *, output_path: str | None, variable_name: str | None = None) -> Any:
    """从 Skill 结构化输出中提取变量值。"""
    if result is None:
        return None
    if not isinstance(result, dict):
        return result

    if output_path:
        current: Any = result
        for part in output_path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                idx = int(part)
                current = current[idx] if 0 <= idx < len(current) else None
            else:
                return None
            if current is None:
                return None
        return current

    if variable_name:
        if variable_name in result:
            return result.get(variable_name)
        values = result.get("values")
        if isinstance(values, dict) and variable_name in values:
            return values.get(variable_name)

    for key in ("value", "result", "output"):
        if key in result:
            return result.get(key)

    payload_keys = [key for key in result if key not in {"ok", "error", "message", "explanation", "confidence"}]
    if len(payload_keys) == 1:
        return result.get(payload_keys[0])
    return result


class DynamicSkillRunner:
    """按运行时数据库定义执行 Skill。"""

    def __init__(
        self,
        *,
        db_session_factory: Any,
        ai_registry: AIAssistantRegistry,
        assistant_type: str = "htp-agent",
    ) -> None:
        self._db_session_factory = db_session_factory
        self._ai_registry = ai_registry
        self._assistant_type = assistant_type

    async def get_active_skill(self, skill_name: str) -> SkillSnapshot:
        """从数据库按 active 状态加载 Skill，管理页修改后下一次调用立即生效。"""
        candidates = build_skill_name_candidates(skill_name)
        async with self._db_session_factory() as session:
            result = await session.execute(
                select(SkillDefinitionORM).where(
                    SkillDefinitionORM.skill_name.in_(candidates),
                    SkillDefinitionORM.is_active.is_(True),
                )
            )
            rows = list(result.scalars().all())

            by_name = {row.skill_name: row for row in rows}
            row = next((by_name[name] for name in candidates if name in by_name), None)
            if row is None:
                raise SkillNotFoundError(f"动态 Skill 不存在或未启用: {skill_name}")
            await self._validate_allowed_tools(session, row.allowed_tools)
            snapshot = await DynamicResourcePublisher(session).ensure_published(**skill_resource_payload(row))
            await session.commit()

            return SkillSnapshot(
                id=int(row.id),
                skill_name=str(row.skill_name),
                display_name=row.display_name,
                description=row.description,
                instructions_md=row.instructions_md or "",
                allowed_tools=row.allowed_tools,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                resource_revision=snapshot_revision_metadata(snapshot),
            )

    async def _validate_allowed_tools(self, session: Any, allowed_tools: str | None) -> None:
        """执行前校验 Skill allowed_tools 引用仍然存在且启用。"""
        if not allowed_tools:
            return
        tool_names = [item.strip() for item in allowed_tools.replace(",", " ").split() if item.strip()]
        if not tool_names:
            return
        result = await session.execute(
            select(ToolDefinitionORM.tool_name).where(
                ToolDefinitionORM.tool_name.in_(tool_names),
                ToolDefinitionORM.is_active.is_(True),
            )
        )
        existing = set(result.scalars().all())
        missing = sorted(set(tool_names) - existing)
        if missing:
            raise DynamicSkillError(f"动态 Skill allowed_tools 引用了不存在或未启用的工具: {', '.join(missing)}")

    async def execute(
        self,
        skill_name: str,
        context_variables: dict[str, Any],
        *,
        variable_name: str | None = None,
        output_path: str | None = None,
        reason: str | None = None,
        conversation_id: str = "",
        case_id: str = "",
    ) -> dict[str, Any]:
        """执行数据库 Skill 并返回结构化结果。"""
        snapshot = await self.get_active_skill(skill_name)
        ai_client = self._ai_registry.get_client(self._assistant_type)
        if ai_client is None:
            raise DynamicSkillError(f"未找到动态 Skill 使用的 AI 客户端: {self._assistant_type}")

        trace_id = get_current_trace_id() or "unknown"
        logger.info(
            event="dynamic_skill_execute_start",
            skill_name=snapshot.skill_name,
            requested_name=skill_name,
            variable_name=variable_name,
            output_path=output_path,
            conversation_id=conversation_id,
            trace_id=trace_id,
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 HCI 排障平台的动态 Skill 运行器。"
                    "你必须严格按照给定 Skill 指令处理输入上下文。"
                    "只能输出 JSON 对象，不要输出 Markdown。"
                    "若无法确定结果，返回 {\"ok\": false, \"error\": \"...\"}。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "skill_name": snapshot.skill_name,
                        "description": snapshot.description,
                        "instructions_md": snapshot.instructions_md,
                        "allowed_tools": snapshot.allowed_tools,
                        "variable_name": variable_name,
                        "output_path": output_path,
                        "reason": reason,
                        "context_variables": context_variables,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        invoke_result = await ai_client.invoke(
            messages=messages,
            tools=None,
            user_id=conversation_id,
            case_id=case_id,
            response_format={"type": "json_object"},
        )
        raw_content = invoke_result.content or ""
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            logger.error(
                event="dynamic_skill_invalid_json",
                skill_name=snapshot.skill_name,
                raw_content=raw_content[:500],
                trace_id=trace_id,
            )
            raise DynamicSkillError(f"动态 Skill 输出不是合法 JSON: {exc}") from exc

        if isinstance(parsed, dict) and parsed.get("ok") is False:
            error = parsed.get("error") or parsed.get("message") or "动态 Skill 返回失败"
            raise DynamicSkillError(str(error))

        value = extract_output_value(parsed, output_path=output_path, variable_name=variable_name)
        if value is None:
            raise DynamicSkillError(
                f"动态 Skill {snapshot.skill_name} 未返回变量 {variable_name or output_path or '<unknown>'} 的可用值"
            )

        await self._audit_skill_usage(
            snapshot,
            status="success",
            context_variables=context_variables,
            output_payload=parsed,
            variable_name=variable_name,
            conversation_id=conversation_id,
            case_id=case_id,
            trace_id=trace_id,
        )

        logger.info(
            event="dynamic_skill_execute_success",
            skill_name=snapshot.skill_name,
            variable_name=variable_name,
            trace_id=trace_id,
        )
        return {
            "ok": True,
            "value": value,
            "raw": parsed,
            "source": "dynamic_skill",
            "skill_name": snapshot.skill_name,
            "skill_id": snapshot.id,
            "skill_updated_at": snapshot.updated_at,
            "resource_revision": snapshot.resource_revision,
        }

    async def _audit_skill_usage(
        self,
        snapshot: SkillSnapshot,
        *,
        status: str,
        context_variables: dict[str, Any],
        output_payload: Any | None,
        variable_name: str | None,
        conversation_id: str,
        case_id: str,
        trace_id: str,
        error: str | None = None,
    ) -> None:
        """写入 Skill 动态资源使用审计。"""
        if not snapshot.resource_revision:
            return
        async with self._db_session_factory() as session:
            resource_snapshot = await DynamicResourceLoader(session).get_active("skill", snapshot.skill_name)
            await DynamicResourceLoader(session).audit_usage(
                resource_snapshot,
                UsageRecord(
                    consumer="agent-service.dynamic_skill_runner",
                    status=status,
                    conversation_id=conversation_id,
                    case_id=case_id,
                    trace_id=trace_id,
                    input_payload={"variable_name": variable_name, "context_variables": context_variables},
                    output_payload=output_payload,
                    error=error,
                ),
            )
            await session.commit()
