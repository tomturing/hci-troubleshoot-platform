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

import uuid
from typing import Any

from shared.clients import KBClient
from shared.observability.logger import get_logger

from app.tools.sop.client import ConversationSopClient

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
    tree_json = await kb_client.get_sop_tree(sop_document_id)
    if tree_json is None:
        logger.warning(
            event="sop_tree_not_found",
            sop_document_id=sop_document_id,
            node_id=node_id,
        )
        return {
            "error": f"SOP 文档 {sop_document_id} 的决策树不存在或未发布",
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

    # 构建返回结果
    result = _build_node_response(target_node)

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
    if tree_json.get("node_id") == node_id:
        return tree_json

    for child in tree_json.get("children", []):
        found = _find_node_in_tree(child, node_id)
        if found:
            return found

    return None


def _build_node_response(node: dict) -> dict[str, Any]:
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

    # 提取子节点概览（仅 node_id + title）
    children_summary = [
        {"node_id": child.get("node_id", ""), "title": child.get("name", "")}
        for child in children
    ]

    if is_leaf:
        diagnosis = node.get("diagnosis")
        solution = node.get("solution")

        if diagnosis:
            return {
                "node_id": node.get("node_id", ""),
                "type": "diagnosis",
                "title": node.get("name", ""),
                "content": _format_diagnosis_content(diagnosis),
                "commands": diagnosis.get("acli_methods", []),
                "children": [],
                "has_solution": solution is not None,
            }
        elif solution:
            return {
                "node_id": node.get("node_id", ""),
                "type": "solution",
                "title": node.get("name", ""),
                "content": _format_solution_content(solution),
                "commands": [],
                "children": [],
            }
        else:
            return {
                "node_id": node.get("node_id", ""),
                "type": "leaf",
                "title": node.get("name", ""),
                "content": "（此叶节点缺少诊断和解决方案内容）",
                "commands": [],
                "children": [],
            }
    else:
        prerequisites = node.get("prerequisites", [])
        content_parts = []
        if prerequisites:
            content_parts.append("【进入条件】")
            content_parts.extend(f"- {p}" for p in prerequisites)

        return {
            "node_id": node.get("node_id", ""),
            "type": "branch",
            "title": node.get("name", ""),
            "content": "\n".join(content_parts) if content_parts else "",
            "commands": [],
            "children": children_summary,
        }


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

        node_title = target_node.get("name", "")

        # 调用 conversation-service 更新执行状态
        if conversation_sop_client is None:
            return {"error": "ConversationSopClient 未注入，无法推进 SOP 执行"}

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
