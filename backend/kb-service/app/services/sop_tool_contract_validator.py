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

ALLOWED_BASH_CONTAINERS = {"asv-con", "vn-con", "vn-agent", "vs-cp-manager"}
_BASH_FORBIDDEN_PREFIX_RE = re.compile(r"(^|\s)(docker\s+exec|kubectl\s+exec|nsenter)\b", re.IGNORECASE)
_BASH_CONTAINER_HINT_RE = re.compile(
    r"\b(?:container|container_exec|容器|进入容器)\b.*\b(asv-con|vn-con|vn-agent|vs-cp-manager)\b",
    re.IGNORECASE,
)
_ACLI_CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "acli_command_catalog.json"


def _normalize_spaces(value: str) -> str:
    return " ".join(value.strip().split())


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


@lru_cache(maxsize=1)
def _get_acli_catalog_commands() -> frozenset[str]:
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


def _make_issue(code: str, message: str, location: str, line_number: int | None = None) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        level="warning",
        location=location,
        line_number=line_number,
        message=message,
    )


def _validate_acli_command(command: str, location: str, line_number: int | None) -> list[ValidationIssue]:
    tokens = _tokenize(command)
    if not tokens:
        return [
            _make_issue(
                "sop_tool_acli_parse_failed",
                f"SOP 命令无法解析为合法 shell 参数：{command}",
                location,
                line_number,
            )
        ]

    if tokens[0] != "acli":
        return []

    if "--help" in tokens or "-?" in tokens or "-h" in tokens:
        return []

    path_tokens = _strip_global_options(tokens)
    normalized_path = _normalize_spaces(" ".join(path_tokens))
    if normalized_path == "acli acli command list":
        return []

    if not _catalog_matches(path_tokens, _get_acli_catalog_commands()):
        return [
            _make_issue(
                "sop_tool_acli_command_not_in_catalog",
                f"SOP 中的 aCLI 命令不在本地 catalog 中：{normalized_path}。请改为受支持命令，或先使用 acli ... --help 探索。",
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

    # 只有解析器已明确标记为 command 的前置检查/代码块才按 bash_exec 草案处理。
    # 旧 SOP 常把命令写成自然语言，本阶段先 warning 暴露质量问题，不阻断发布。
    if not _BASH_CONTAINER_HINT_RE.search(stripped):
        return [
            _make_issue(
                "sop_tool_bash_container_missing",
                "SOP 中的 bash 命令缺少明确容器边界；后续映射为 bash_exec 时必须指定 asv-con/vn-con/vn-agent/vs-cp-manager。",
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
