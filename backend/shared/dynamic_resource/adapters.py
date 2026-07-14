"""现有业务表到动态资源模型的适配器。"""

from __future__ import annotations

from typing import Any


def _split_allowed_tools(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.replace(",", " ")
    return [item.strip() for item in normalized.split() if item.strip()]


def tool_resource_payload(tool: Any) -> dict[str, Any]:
    """将 tool_definition 行转换为动态资源 payload。"""
    return {
        "resource_type": "tool",
        "resource_name": str(tool.tool_name),
        "version": str(tool.version or "1.0"),
        "content": {
            "tool_name": tool.tool_name,
            "display_name": tool.display_name,
            "category": tool.category,
            "description": tool.description,
            "usage_template": tool.usage_template,
            "examples": tool.examples or [],
            "is_active": bool(tool.is_active),
        },
        "contract": {
            "parameters_schema": tool.parameters_schema or {},
            "risk_level": int(tool.risk_level or 1),
        },
        "dependencies": [],
        "status": "published" if tool.is_active else "disabled",
    }


def skill_resource_payload(skill: Any) -> dict[str, Any]:
    """将 skill_definition 行转换为动态资源 payload。"""
    allowed_tools = _split_allowed_tools(skill.allowed_tools)
    return {
        "resource_type": "skill",
        "resource_name": str(skill.skill_name),
        "version": "1.0",
        "content": {
            "skill_name": skill.skill_name,
            "display_name": skill.display_name,
            "description": skill.description,
            "instructions_md": skill.instructions_md or "",
            "compatibility": skill.compatibility,
            "license": skill.license,
            "allowed_tools": skill.allowed_tools,
            "metadata_json": skill.metadata_json or {},
            "assets_json": skill.assets_json or [],
            "references_json": skill.references_json or [],
            "is_active": bool(skill.is_active),
        },
        "contract": {
            "allowed_tools": allowed_tools,
        },
        "dependencies": [{"resource_type": "tool", "resource_name": tool_name} for tool_name in allowed_tools],
        "status": "published" if skill.is_active else "disabled",
    }


def prompt_resource_payload(prompt: Any) -> dict[str, Any]:
    """将 system_prompt 行转换为动态资源 payload。"""
    return {
        "resource_type": "prompt",
        "resource_name": str(prompt.name),
        "version": str(prompt.version or "1.0"),
        "content": {
            "stage": prompt.stage,
            "name": prompt.name,
            "description": prompt.description,
            "content_template": prompt.content_template,
            "is_active": bool(prompt.is_active),
        },
        "contract": {},
        "dependencies": [],
        "status": "published" if prompt.is_active else "disabled",
    }


def prompt_slot_resource_payload(
    slot_name: str, active_prompt_name: str, expected_placeholders: list[str], consumer: str
) -> dict[str, Any]:
    """将 prompt slot 转换为动态资源 payload。"""
    return {
        "resource_type": "prompt_slot",
        "resource_name": slot_name,
        "version": "1.0",
        "content": {
            "slot_name": slot_name,
            "active_prompt_name": active_prompt_name,
            "expected_placeholders": expected_placeholders,
            "consumer": consumer,
        },
        "contract": {
            "expected_placeholders": expected_placeholders,
        },
        "dependencies": [{"resource_type": "prompt", "resource_name": active_prompt_name}],
        "status": "published",
    }


def sop_resource_payload(sop: Any) -> dict[str, Any]:
    """将 sop_document 行/字典转换为动态资源 payload。"""
    getter = sop.get if isinstance(sop, dict) else lambda key, default=None: getattr(sop, key, default)
    variable_schema = getter("variable_schema", []) or []
    dependencies: list[dict[str, str]] = []
    for var_def in variable_schema:
        if not isinstance(var_def, dict):
            continue
        strategy = var_def.get("acquisition_strategy")
        tool_name = var_def.get("acquisition_tool")
        if strategy == "skill_call" and tool_name:
            dependencies.append({"resource_type": "skill", "resource_name": str(tool_name)})
        elif strategy == "tool_call" and tool_name:
            dependencies.append({"resource_type": "tool", "resource_name": str(tool_name)})

    resource_name = str(getter("id") or getter("source_id") or getter("title"))
    return {
        "resource_type": "sop",
        "resource_name": resource_name,
        "version": str(getter("tree_schema_version", None) or "sop-tree-v1"),
        "content": {
            "id": getter("id"),
            "source_id": getter("source_id"),
            "category_id": getter("category_id"),
            "title": getter("title"),
            "content_md": getter("content_md"),
            "tree_json": getter("tree_json"),
            "variable_schema": variable_schema,
            "status": getter("status"),
        },
        "contract": {
            "tree_schema_version": getter("tree_schema_version", None) or "sop-tree-v1",
            "tree_validation_status": getter("tree_validation_status"),
            "tree_validation_issues": getter("tree_validation_issues") or [],
        },
        "dependencies": dependencies,
        "status": "published" if getter("status") == "published" else "draft",
    }


def kbd_resource_payload(kbd: Any) -> dict[str, Any]:
    """将 kbd_entry 行/字典转换为动态资源 payload。"""
    getter = kbd.get if isinstance(kbd, dict) else lambda key, default=None: getattr(kbd, key, default)
    resource_name = str(getter("id") or getter("support_id"))
    return {
        "resource_type": "kbd",
        "resource_name": resource_name,
        "version": "1.0",
        "content": {
            "id": getter("id"),
            "support_id": getter("support_id"),
            "title": getter("title"),
            "category_id": getter("category_id"),
            "problem_description": getter("problem_description"),
            "alert_info": getter("alert_info"),
            "steps_text": getter("steps_text"),
            "signals_json": getter("signals_json") or [],
            "content_md": getter("content_md"),
            "content_raw": getter("content_raw"),
            "images_json": getter("images_json") or [],
            "root_cause": getter("root_cause"),
            "solution": getter("solution"),
            "operational_impact": getter("operational_impact"),
            "is_temporary": getter("is_temporary"),
            "recommendations": getter("recommendations"),
            "status": getter("status"),
        },
        "contract": {
            "agent_usable": bool(getter("signals_json") or []),
            "metadata": getter("entry_metadata") or getter("metadata") or {},
        },
        "dependencies": [],
        "status": "published" if getter("status") == "published" else "draft",
    }
