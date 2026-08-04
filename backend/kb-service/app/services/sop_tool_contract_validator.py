"""
SOP 发布阶段工具契约校验。

该模块只做知识库发布前的静态质量检查，不执行命令、不访问外部服务。
规则语义与 agent-service 的工具语义校验保持一致，但实现放在 kb-service 内，
避免微服务镜像之间产生运行时 import 耦合。
"""

from __future__ import annotations

import json
import re
import shlex
from functools import lru_cache
from pathlib import Path

from app.schemas.sop_template import SOPNode, ValidationIssue
from app.services.sop_command_intent import ALLOWED_SOP_BASH_CONTAINERS, normalize_sop_command

ALLOWED_BASH_CONTAINERS = ALLOWED_SOP_BASH_CONTAINERS
_BASH_FORBIDDEN_PREFIX_RE = re.compile(r"(^|\s)(docker\s+exec|kubectl\s+exec|nsenter)\b", re.IGNORECASE)
_ACLI_CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "acli_command_catalog.json"

# aCLI catalog 目前只记录命令路径，不记录 argv schema。这里只维护已经由回归
# 证实的最小调用契约；它描述命令本身，不绑定 KBD/support_id。
_ACLI_MIN_TAIL_ARGS = {
    "acli system smartctl": 1,
}


def _normalize_spaces(value: str) -> str:
    return " ".join(value.strip().split())


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


@lru_cache(maxsize=1)
def get_acli_catalog_commands() -> frozenset[str]:
    """返回当前代码随附的 aCLI catalog 命令集合。"""

    with _ACLI_CATALOG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    commands: set[str] = set()
    for item in data.get("commands") or []:
        if isinstance(item, dict) and item.get("command"):
            commands.add(_normalize_spaces(str(item["command"])))
        elif isinstance(item, str):
            commands.add(_normalize_spaces(item))
    return frozenset(commands)


def _strip_global_options(tokens: list[str]) -> list[str]:
    result: list[str] = []
    i = 0
    value_options = {"--formatter", "--cluster", "--timeout", "--container"}
    bool_options = {"--debug", "--force", "--version", "--help", "-?", "-h"}
    while i < len(tokens):
        token = tokens[i]
        if token in value_options:
            i += 2
            continue
        if any(token.startswith(f"{option}=") for option in value_options):
            i += 1
            continue
        if token in bool_options:
            i += 1
            continue
        result.append(token)
        i += 1
    return result


def _catalog_matches(path_tokens: list[str], catalog_commands: frozenset[str]) -> bool:
    for catalog_command in catalog_commands:
        catalog_tokens = catalog_command.split()
        if len(path_tokens) >= len(catalog_tokens) and path_tokens[: len(catalog_tokens)] == catalog_tokens:
            return True
    return False


def validate_acli_catalog_command(command: str) -> str | None:
    """校验只读编译结果是否落在本地 aCLI 命令目录中。

    返回 ``None`` 表示命令可由当前 catalog 识别；否则返回面向专家的原因。该函数
    同时供 SOP 发布校验和 KBD Signal Proposal 保存门禁复用，避免两个知识生产入口
    对“Schema 合法但现场命令不存在”给出不同结论。
    """

    tokens = _tokenize(command)
    if not tokens:
        return f"aCLI 命令无法解析为合法参数：{command}"
    if tokens[0] != "acli":
        return None
    if "--help" in tokens or "-?" in tokens or "-h" in tokens:
        return None

    path_tokens = _strip_global_options(tokens)
    normalized_path = _normalize_spaces(" ".join(path_tokens))
    if normalized_path == "acli acli command list":
        return None
    if not _catalog_matches(path_tokens, get_acli_catalog_commands()):
        return (
            f"aCLI 命令不在当前 catalog 中：{normalized_path}。"
            "请改为已注册的只读命令，或交由专家确认现场能力"
        )
    return None


def validate_acli_invocation_command(command: str) -> str | None:
    """校验 catalog 已登记命令是否具备可运行的最小 argv。

    catalog 命中仅证明命令路径存在；缺少命令本身必需的参数时，调用仍会直接
    打印 usage 或失败。该结果属于 ``run_failed``，不能归为 ``not_exists``。
    """

    tokens = _tokenize(command)
    if not tokens or tokens[0] != "acli":
        return None
    path_tokens = _strip_global_options(tokens)
    for command_path, minimum in _ACLI_MIN_TAIL_ARGS.items():
        prefix = command_path.split()
        if path_tokens[: len(prefix)] != prefix:
            continue
        actual = len(path_tokens) - len(prefix)
        if actual < minimum:
            return (
                f"aCLI 命令缺少运行所需参数：{_normalize_spaces(command)}；"
                f"{command_path} 至少需要 {minimum} 个命令参数"
            )
    return None


def _make_issue(code: str, message: str, location: str, line_number: int | None = None) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        level="warning",
        location=location,
        line_number=line_number,
        message=message,
    )


def _validate_acli_command(command: str, location: str, line_number: int | None) -> list[ValidationIssue]:
    if not _tokenize(command):
        return [
            _make_issue(
                "sop_tool_acli_parse_failed",
                f"SOP 命令无法解析为合法 shell 参数：{command}",
                location,
                line_number,
            )
        ]
    reason = validate_acli_catalog_command(command)
    if reason:
        return [
            _make_issue(
                "sop_tool_acli_command_not_in_catalog",
                f"SOP 中的{reason}",
                location,
                line_number,
            )
        ]
    invocation_reason = validate_acli_invocation_command(command)
    if invocation_reason:
        return [
            _make_issue(
                "sop_tool_acli_invocation_invalid",
                f"SOP 中的{invocation_reason}",
                location,
                line_number,
            )
        ]
    return []


def _validate_bash_command(command: str, location: str, line_number: int | None) -> list[ValidationIssue]:
    stripped = command.strip()
    if not stripped:
        return []

    if _BASH_FORBIDDEN_PREFIX_RE.search(stripped):
        return [
            _make_issue(
                "sop_tool_bash_container_prefix_forbidden",
                "SOP 中的 bash 命令不应手写 docker exec、kubectl exec 或 nsenter；容器进入方式应由 bash_exec(container, command) 结构化表达。",
                location,
                line_number,
            )
        ]

    tokens = _tokenize(stripped)
    if tokens and tokens[0] == "acli":
        return []

    intent = normalize_sop_command(stripped)
    if intent and intent.get("parse_status") == "error":
        return [
            _make_issue(
                "sop_tool_command_parse_failed",
                f"SOP 命令无法归一化为工具调用：{intent.get('error')}",
                location,
                line_number,
            )
        ]
    return []


def validate_sop_tool_contract(root: SOPNode) -> list[ValidationIssue]:
    """遍历 SOP 决策树，生成工具契约校验问题。"""
    issues: list[ValidationIssue] = []

    def walk(node: SOPNode, path: list[str]) -> None:
        location = " > ".join(path)

        for item in node.prerequisite_items:
            if item.content_type == "command":
                issues.extend(_validate_acli_command(item.description, f"{location} > 前置检查", node.line_number))
                issues.extend(_validate_bash_command(item.description, f"{location} > 前置检查", node.line_number))

        if node.diagnosis:
            for method in node.diagnosis.acli_methods:
                issues.extend(_validate_acli_command(method, f"{location} > 判断方法", node.line_number))
                tokens = _tokenize(method)
                if tokens and tokens[0] != "acli":
                    issues.extend(_validate_bash_command(method, f"{location} > 判断方法", node.line_number))

        for child in node.children:
            walk(child, path + [child.title])

    walk(root, [root.title])
    return issues
