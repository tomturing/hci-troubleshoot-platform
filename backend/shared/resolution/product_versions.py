"""HCI 产品版本约束匹配，兼容历史 glob 与命令级比较表达式。"""

from __future__ import annotations

import fnmatch
import re

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$", re.IGNORECASE)
_COMPARISON_RE = re.compile(r"^(>=|<=|>|<|==|=)\s*(.+)$")


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def product_version_matches(product_version: str, constraint: str) -> bool:
    """判断一个产品版本是否满足 glob 或逗号连接的比较约束。"""

    version = str(product_version).strip()
    expression = str(constraint).strip()
    if not version or not expression:
        return False
    if not any(operator in expression for operator in (">", "<", "=")):
        return fnmatch.fnmatchcase(version, expression)

    actual = _version_tuple(version)
    if actual is None:
        return False
    for clause in (part.strip() for part in expression.split(",")):
        match = _COMPARISON_RE.fullmatch(clause)
        expected = _version_tuple(match.group(2)) if match else None
        if match is None or expected is None:
            return False
        operator = match.group(1)
        if operator == ">=" and not actual >= expected:
            return False
        if operator == "<=" and not actual <= expected:
            return False
        if operator == ">" and not actual > expected:
            return False
        if operator == "<" and not actual < expected:
            return False
        if operator in {"=", "=="} and actual != expected:
            return False
    return True


def matches_any_product_version(product_version: str, constraints: list[str]) -> bool:
    """任一声明约束命中即视为受支持。"""

    return any(product_version_matches(product_version, item) for item in constraints)
