"""ADR-2：{{VAR}} 全大写占位符强制校验单测。"""

from __future__ import annotations

import re

import pytest

# 与 variable_pool/engine.py 保持一致的真相源正则
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)\}\}")
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^}]*)\}\}")


def _validate(template: str) -> list[str]:
    """镜像 extract_signals.validate_placeholder_case 的校验逻辑。"""
    valid: list[str] = []
    for m in _ANY_PLACEHOLDER_RE.finditer(template):
        if not _PLACEHOLDER_RE.fullmatch(m.group(0)):
            raise ValueError(f"非法占位符 {{{{{m.group(1)}}}}}")
        valid.append(m.group(1))
    return valid


def test_valid_uppercase_placeholder():
    assert _validate("{{HOST}}") == ["HOST"]


def test_valid_dotted_path():
    assert _validate("{{NODE.IP}}") == ["NODE.IP"]


def test_mixed_in_string():
    assert _validate("prefix-{{HOST}}-{{VM_ID}}-suffix") == ["HOST", "VM_ID"]


def test_reject_lowercase():
    with pytest.raises(ValueError):
        _validate("{{host}}")


def test_reject_mixed_case():
    with pytest.raises(ValueError):
        _validate("{{HostName}}")


def test_reject_empty():
    with pytest.raises(ValueError):
        _validate("{{}}")


def test_no_placeholder_passes():
    assert _validate("plain text without placeholder") == []


def test_reject_dollar_brace_legacy():
    """旧 ${host} 不再被识别为合法占位符（不匹配即不校验，但也不渲染）。"""
    # ${host} 不匹配 {{...}} 模式，故校验通过（无占位符），但渲染时也不会替换
    assert _validate("${host}") == []
