"""
SOP 导航工具实现（T-AGT-20、T-AGT-21）

提供 LLM 通过 tool_call 调用的 SOP 决策树导航工具：
  - get_sop_node  : 获取指定节点的内容 + 子节点列表（分步导航）
  - sop_advance   : 推进 SOP 到子节点，记录推理路径，更新执行状态

设计依据：
  - docs/solution/agent/agent设计.md §12.6 推荐方案③（导航工具化）
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md

使用场景：
  SOP 命中后，ReactEngine 通过 SopToolExecutor 注入执行上下文，
  LLM 按需调用 get_sop_node 获取节点内容，再调用 sop_advance 推进流程，
  实现上下文可控的决策树遍历（避免一次性注入完整 SOP 文档）。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from shared.clients import KBClient
from shared.observability.logger import get_logger
from shared.utils.acquisition_strategy import parse_strategy

from app.tools.sop.client import ConversationSopClient
from app.tools.sop.command_intent import normalize_sop_commands

logger = get_logger("tools.sop.nav")


async def get_sop_node(
    node_id: str,
    *,
    sop_document_id: int,
    kb_client: KBClient,
) -> dict[str, Any]:
    """获取 SOP 决策树节点内容。

    Args:
        node_id: 节点 ID，如 "n-1"、"n-3-2"（由 LLM 传入）
        sop_document_id: SOP 文档 ID（由 SOP 命中时注入，非 LLM 传入）
        kb_client: KB 服务客户端（由上下文注入）

    Returns:
        节点内容字典，格式：
        {
          "node_id": "n-3",
          "type": "branch",           // branch | diagnosis | solution
          "title": "存储 I/O 故障诊断",
          "content": "检查磁盘错误率...",
          "commands": ["acli host.disk.stat"],
          "children": [
            {"node_id": "n-3-1", "title": "磁盘错误率 < 5%"},
            {"node_id": "n-3-2", "title": "磁盘错误率 > 5%"}
          ]
        }

    Raises:
        返回错误信息字典（而非抛异常），格式：
        {"error": "节点 n-999 不存在", "node_id": "n-999"}
    """
    # 获取完整决策树
    tree_data = await kb_client.get_sop_tree(sop_document_id)
    if tree_data is None:
        logger.warning(
            event="sop_tree_not_found",
            sop_document_id=sop_document_id,
            node_id=node_id,
        )
        return {
            "error": f"SOP 文档 {sop_document_id} 的决策树不存在或未发布",
            "sop_document_id": sop_document_id,
        }

    tree_json = tree_data.get("tree")
    if tree_json is None:
        logger.warning(
            event="sop_tree_empty",
            sop_document_id=sop_document_id,
            node_id=node_id,
        )
        return {
            "error": f"SOP 文档 {sop_document_id} 决策树未生成",
            "sop_document_id": sop_document_id,
        }

    # 递归查找目标节点
    target_node = _find_node_in_tree(tree_json, node_id)
    if target_node is None:
        logger.warning(
            event="sop_node_not_found",
            sop_document_id=sop_document_id,
            node_id=node_id,
        )
        return {
            "error": f"节点 {node_id} 不存在于 SOP 决策树中",
            "node_id": node_id,
        }

    variable_schema: list[dict[str, Any]] = []
    try:
        sop_doc = await kb_client.get_sop_document(sop_document_id)
        if sop_doc and isinstance(sop_doc.get("variable_schema"), list):
            variable_schema = sop_doc.get("variable_schema") or []
    except Exception as exc:
        logger.warning(
            event="sop_variable_schema_load_failed",
            sop_document_id=sop_document_id,
            node_id=node_id,
            error=str(exc),
        )

    # 构建返回结果
    result = _build_node_response(target_node, variable_schema=variable_schema)

    logger.info(
        event="sop_node_retrieved",
        sop_document_id=sop_document_id,
        node_id=node_id,
        node_type=result.get("type"),
        children_count=len(result.get("children", [])),
    )

    return result


def _find_node_in_tree(tree_json: dict, node_id: str) -> dict | None:
    """递归查找节点（模拟 SOPNode.find_node 方法）。

    Args:
        tree_json: SOPNode.model_dump() 格式的 JSON
        node_id: 目标节点 ID

    Returns:
        找到的节点 dict，未找到返回 None
    """
    node_key = tree_json.get("node_id") or tree_json.get("id")
    if node_key == node_id:
        return tree_json

    for child in tree_json.get("children", []):
        found = _find_node_in_tree(child, node_id)
        if found:
            return found

    return None


def find_node_in_tree(tree_json: dict, node_id: str) -> dict | None:
    """公共包装：按 node_id 从 SOP 决策树中查找节点。"""
    return _find_node_in_tree(tree_json, node_id)


def _build_node_response(node: dict, *, variable_schema: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """构建节点响应（符合任务文档规格）。

    Args:
        node: SOPNode.model_dump() 格式的节点 dict

    Returns:
        工具返回格式：
        - branch（中间节点）：返回 title + prerequisites + children 概览
        - diagnosis：返回判断方法详情（叶节点 diagnosis 字段）
        - solution：返回解决方案详情（叶节点 solution 字段）
    """
    children = node.get("children", [])
    is_leaf = not children

    node_id_val = node.get("node_id") or node.get("id") or ""
    node_name_val = node.get("name") or node.get("title") or ""
    variable_schema = variable_schema or []

    # 提取子节点概览，兼容 node_id/id 和 name/title，同时外显分支条件依赖的变量。
    children_summary = [
        {
            "node_id": child.get("node_id") or child.get("id") or "",
            "title": child.get("name") or child.get("title") or "",
            "prerequisites": child.get("prerequisites", []),
            "required_variables": _build_required_variables(child, variable_schema),
        }
        for child in children
    ]

    if is_leaf:
        diagnosis = node.get("diagnosis")
        solution = node.get("solution")

        if diagnosis:
            commands = diagnosis.get("acli_methods", [])
            response = {
                "node_id": node_id_val,
                "type": "diagnosis",
                "title": node_name_val,
                "content": _format_diagnosis_content(diagnosis),
                "commands": commands,
                "tool_calls": normalize_sop_commands(commands, reason=f"执行 SOP 节点「{node_name_val}」的诊断命令"),
                "children": [],
                "has_solution": solution is not None,
            }
            response["required_variables"] = _build_required_variables(node, variable_schema)
            return response
        elif solution:
            response = {
                "node_id": node_id_val,
                "type": "solution",
                "title": node_name_val,
                "content": _format_solution_content(solution),
                "commands": [],
                "children": [],
            }
            response["required_variables"] = _build_required_variables(node, variable_schema)
            return response
        else:
            response = {
                "node_id": node_id_val,
                "type": "leaf",
                "title": node_name_val,
                "content": "（此叶节点缺少诊断和解决方案内容）",
                "commands": [],
                "children": [],
            }
            response["required_variables"] = _build_required_variables(node, variable_schema)
            return response
    else:
        prerequisites = node.get("prerequisites", [])
        prerequisite_items = node.get("prerequisite_items", [])
        commands = [
            item.get("description", "")
            for item in prerequisite_items
            if isinstance(item, dict) and item.get("content_type") == "command" and item.get("description")
        ]
        content_parts = []
        if prerequisites:
            content_parts.append("【进入条件】")
            content_parts.extend(f"- {p}" for p in prerequisites)

        response = {
            "node_id": node_id_val,
            "type": "branch",
            "title": node_name_val,
            "content": "\n".join(content_parts) if content_parts else "",
            "commands": commands,
            "tool_calls": normalize_sop_commands(commands, reason=f"执行 SOP 节点「{node_name_val}」的前置检查命令"),
            "children": children_summary,
        }
        response["required_variables"] = _build_required_variables(node, variable_schema)
        return response


def _build_required_variables(node: dict, variable_schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回当前节点文本直接引用的变量定义。"""
    names = _extract_node_variable_names(node)
    if not names:
        return []
    schema_by_name = {item.get("name"): item for item in variable_schema if isinstance(item, dict)}
    required: list[dict[str, Any]] = []
    for name in sorted(names):
        schema = schema_by_name.get(name) or {"name": name, "acquisition_strategy": "user_input", "type": "string"}
        required.append(
            {
                "name": name,
                "type": schema.get("type", "string"),
                "description": schema.get("description", ""),
                "acquisition_strategy": schema.get("acquisition_strategy", "user_input"),
                "acquisition_tool": schema.get("acquisition_tool"),
            }
        )
    return required


def _extract_node_variable_names(node: dict) -> set[str]:
    texts: list[str] = []
    texts.append(str(node.get("title") or ""))
    for prerequisite in node.get("prerequisites", []) or []:
        texts.append(str(prerequisite))
    for item in node.get("prerequisite_items", []) or []:
        if isinstance(item, dict):
            texts.append(str(item.get("description") or ""))
    diagnosis = node.get("diagnosis") or {}
    if isinstance(diagnosis, dict):
        for key in ("acli_methods", "page_methods", "analysis_steps", "possible_causes"):
            for value in diagnosis.get(key, []) or []:
                texts.append(str(value))
    solution = node.get("solution") or {}
    if isinstance(solution, dict):
        for key in ("quick_recovery", "thorough_fix"):
            for value in solution.get(key, []) or []:
                texts.append(str(value))
    joined = "\n".join(texts)
    return {match.replace("\\", "") for match in re.findall(r"(?<!\{)\$?\{([a-z][a-z0-9_\\]*)\}(?!\})", joined)}


def _has_variable_value(context_variables: dict[str, Any], variable_name: str) -> bool:
    """判断运行时变量池是否已有有效值。"""
    if variable_name not in context_variables:
        return False
    raw_value = context_variables.get(variable_name)
    value = raw_value.get("value") if isinstance(raw_value, dict) else raw_value
    return value is not None and value != ""


def _merge_extracted_variables(
    context_variables: dict[str, Any],
    variables_extracted: dict[str, Any] | None,
) -> dict[str, Any]:
    """把本次 sop_advance 携带的变量抽取结果纳入门禁判断。"""
    merged = dict(context_variables or {})
    for name, value in (variables_extracted or {}).items():
        merged[name] = {"value": value, "source": "pending_sop_advance"}
    return merged


def _find_missing_guarded_variables(
    required_variables: list[dict[str, Any]],
    context_variables: dict[str, Any],
) -> list[dict[str, Any]]:
    """找出进入节点前必须先按来源策略获取的缺失变量。

    受守护策略（guarded）= 需要阻断推进直到变量就绪：env_injection / user_input / user_confirm。
    自动策略（auto）= 无需预先阻断：tool_call / skill_call / llm_inference / derived 等。
    """
    missing: list[dict[str, Any]] = []
    for variable in required_variables:
        name = variable.get("name")
        raw_strategy = str(variable.get("acquisition_strategy") or "user_input")
        if not name:
            continue
        # 统一使用公共解析器判断是否受守护
        if not parse_strategy(raw_strategy).is_guarded:
            continue
        if not _has_variable_value(context_variables, name):
            missing.append(variable)
    return missing


def find_missing_guarded_variables_for_node_window(
    *,
    current_node: dict[str, Any],
    variable_schema: list[dict[str, Any]],
    context_variables: dict[str, Any],
) -> list[dict[str, Any]]:
    """检查当前节点和直接子节点缺失的受控来源变量。

    运行时 before-tool-call 门禁使用该窗口，避免把未来深层分支变量提前阻断。
    """
    candidates = [current_node]
    # 若当前节点为非叶子节点，只检测当前节点本身所需的受控变量，不应把子分支的前置变量合并进来提前阻断
    is_leaf = not (current_node.get("children", []) or [])
    if is_leaf:
        candidates.extend(child for child in current_node.get("children", []) or [] if isinstance(child, dict))

    missing_by_name: dict[str, dict[str, Any]] = {}
    for node in candidates:
        required = _build_required_variables(node, variable_schema)
        for variable in _find_missing_guarded_variables(required, context_variables):
            name = variable.get("name")
            if name and name not in missing_by_name:
                missing_by_name[name] = variable
    return list(missing_by_name.values())


def _format_diagnosis_content(diagnosis: dict) -> str:
    """格式化诊断内容为可读文本。"""
    parts = []

    prerequisites = diagnosis.get("prerequisites", [])
    if prerequisites:
        parts.append("【前置检查】")
        parts.extend(f"- {p}" for p in prerequisites)

    page_methods = diagnosis.get("page_methods", [])
    if page_methods:
        parts.append("【页面判断方法】")
        parts.extend(f"- {p}" for p in page_methods)

    description = diagnosis.get("description")
    if description:
        parts.append(f"【说明】{description}")

    root_cause = diagnosis.get("root_cause")
    if root_cause:
        parts.append(f"【根因】{root_cause}")

    notes = diagnosis.get("notes")
    if notes:
        parts.append(f"【注意事项】{notes}")

    return "\n".join(parts)


def _format_solution_content(solution: dict) -> str:
    """格式化解决方案内容为可读文本。"""
    parts = []

    quick_recovery = solution.get("quick_recovery", [])
    if quick_recovery:
        parts.append("【快速恢复】")
        parts.extend(f"- {s}" for s in quick_recovery)

    thorough_fix = solution.get("thorough_fix", [])
    if thorough_fix:
        parts.append("【彻底解决】")
        parts.extend(f"- {s}" for s in thorough_fix)

    return "\n".join(parts)


async def sop_advance(
    target_node_id: str,
    reasoning: str,
    *,
    conversation_id: str,
    sop_document_id: int,
    kb_client: KBClient,
    conversation_sop_client: ConversationSopClient | None = None,
    node_type: str | None = None,
    variables_extracted: dict[str, Any] | None = None,
    completed_steps: list[str] | None = None,  # T-AGT-23: 已完成节点列表（幂等性检查）
) -> dict[str, Any]:
    """推进 SOP 到指定子节点，记录推理路径（T-AGT-21 + T-AGT-23）。

    Args:
        target_node_id: 目标子节点 ID（由 LLM 决策后传入）
        reasoning: LLM 推进理由（写入 execution_log）
        conversation_id: 会话 ID（由上下文注入）
        sop_document_id: SOP 文档 ID（由 SOP 命中时注入）
        kb_client: KB 服务客户端（用于验证节点合法性）
        conversation_sop_client: Conversation SOP API 客户端（用于更新执行状态）
        node_type: 目标节点类型（可选，用于判断叶节点）
        variables_extracted: 变量池更新（可选，变量池功能）
        completed_steps: 已完成节点列表（T-AGT-23，用于幂等性检查）

    Returns:
        {
            "ok": true,
            "current_node_id": "n-3-2",
            "node_type": "diagnosis",
            "message": "已推进到：磁盘错误率 > 5% 分支",
            "is_leaf": false
        }
        或 {"error": "..."}
    """
    try:
        # T-AGT-23: 幂等性检查 - 目标节点已在 completed_steps 中时跳过推进
        if completed_steps and target_node_id in completed_steps:
            logger.info(
                event="sop_advance_idempotent_skip",
                conversation_id=conversation_id,
                sop_document_id=sop_document_id,
                target_node_id=target_node_id,
                completed_steps=completed_steps,
            )
            return {
                "ok": True,
                "current_node_id": target_node_id,
                "node_type": node_type or "already_completed",
                "message": f"节点 {target_node_id} 已完成，跳过重复推进",
                "is_leaf": True,
                "skipped": True,
            }

        # 获取 SOP 决策树，验证目标节点存在
        tree_data = await kb_client.get_sop_tree(sop_document_id)
        if tree_data is None:
            return {"error": f"SOP 文档 {sop_document_id} 不存在"}

        tree_json = tree_data.get("tree")
        if tree_json is None:
            return {"error": f"SOP 文档 {sop_document_id} 决策树未生成"}

        # 查找目标节点
        target_node = _find_node_in_tree(tree_json, target_node_id)
        if target_node is None:
            return {"error": f"目标节点 {target_node_id} 不存在于决策树中"}

        # 确定节点类型
        children = target_node.get("children", [])
        is_leaf_node = not children
        actual_node_type = node_type
        if actual_node_type is None:
            if is_leaf_node:
                if target_node.get("solution"):
                    actual_node_type = "solution"
                elif target_node.get("diagnosis"):
                    actual_node_type = "diagnosis"
                else:
                    actual_node_type = "leaf"
            else:
                actual_node_type = "branch"

        if conversation_sop_client is None:
            return {"error": "ConversationSopClient 未注入，无法推进 SOP 执行"}

        variable_schema: list[dict[str, Any]] = []
        try:
            sop_doc = await kb_client.get_sop_document(sop_document_id)
            if sop_doc and isinstance(sop_doc.get("variable_schema"), list):
                variable_schema = sop_doc.get("variable_schema") or []
        except Exception as exc:
            logger.warning(
                event="sop_advance_variable_schema_load_failed",
                conversation_id=conversation_id,
                sop_document_id=sop_document_id,
                target_node_id=target_node_id,
                error=str(exc),
            )

        required_variables = _build_required_variables(target_node, variable_schema)
        if required_variables:
            execution = await conversation_sop_client.get_execution(uuid.UUID(conversation_id))
            context_variables = (execution or {}).get("context_variables", {}) or {}
            effective_variables = _merge_extracted_variables(context_variables, variables_extracted)
            missing_variables = _find_missing_guarded_variables(required_variables, effective_variables)
            if missing_variables:
                logger.info(
                    event="sop_advance_blocked_by_missing_variables",
                    conversation_id=conversation_id,
                    sop_document_id=sop_document_id,
                    target_node_id=target_node_id,
                    missing_variables=[v.get("name") for v in missing_variables],
                )
                first_missing = missing_variables[0]
                return {
                    "ok": False,
                    "error": "missing_required_variables",
                    "message": (
                        f"目标节点 {target_node_id} 依赖变量 {first_missing.get('name')}，"
                        "且该变量尚未按 SOP 声明的来源策略获取。请先调用 sop_request_variable。"
                    ),
                    "target_node_id": target_node_id,
                    "missing_variables": missing_variables,
                    "next_tool_call": {
                        "tool_name": "sop_request_variable",
                        "args": {
                            "variable_name": first_missing.get("name"),
                            "reason": (
                                first_missing.get("description") or f"进入 SOP 节点 {target_node_id} 前需要该变量"
                            ),
                        },
                    },
                }

        node_title = target_node.get("name") or target_node.get("title") or ""

        # 调用 conversation-service 更新执行状态
        result = await conversation_sop_client.advance(
            conversation_id=uuid.UUID(conversation_id),
            target_node_id=target_node_id,
            reasoning=reasoning,
            node_type=actual_node_type,
            variables_extracted=variables_extracted,
        )

        if "error" in result:
            return result

        logger.info(
            event="sop_advance_success",
            conversation_id=conversation_id,
            sop_document_id=sop_document_id,
            target_node_id=target_node_id,
            node_type=actual_node_type,
            is_leaf=is_leaf_node,
            reasoning=reasoning[:100],
        )

        return {
            "ok": True,
            "current_node_id": target_node_id,
            "node_type": actual_node_type,
            "message": f"已推进到：{node_title}",
            "is_leaf": is_leaf_node,
        }

    except Exception as exc:
        logger.error(
            event="sop_advance_error",
            conversation_id=conversation_id,
            sop_document_id=sop_document_id,
            target_node_id=target_node_id,
            error=str(exc),
        )
        return {"error": f"推进 SOP 失败: {exc}"}
