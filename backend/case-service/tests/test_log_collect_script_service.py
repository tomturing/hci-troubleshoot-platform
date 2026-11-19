"""收集脚本库的纯函数边界测试。

只覆盖不依赖数据库的部分：基线快照扫描、文件名/内容校验、解释器推断、
引用识别。数据库版本化语义由路由集成测试覆盖（需要真实 session）。
"""

from __future__ import annotations

import pytest
from app.services.log_collect_script_service import (
    MAX_SCRIPT_BYTES,
    ScriptError,
    _items_reference,
    baseline_content,
    baseline_scripts,
    infer_interpreter,
    normalize_content,
    sha256_of,
    validate_code,
)


def test_baseline_contains_reference_usable_scripts() -> None:
    """基线必须覆盖三个场景脚本与依赖，且只提供元数据（不含厂商源码）。"""
    scripts = baseline_scripts()
    for code in ("demo_check.py", "demo_network.sh", "demo_collect.py"):
        assert code in scripts, f"基线缺少 {code}"
        assert scripts[code].ref_usable, f"{code} 应可被 action=script 引用"
        assert scripts[code].interpreter in {"python", "bash"}
        # 源码不进入公开仓库：基线只能给元数据。
        assert baseline_content(code) is None


def test_demo_baseline_has_no_binary_and_no_content() -> None:
    """仓库内是虚构示例：不含二进制条目，也不含任何原文。"""
    rows = baseline_scripts()
    assert rows, "示例基线不能为空，否则界面无从展示"
    assert all(not row.binary for row in rows.values())
    assert all(baseline_content(code) is None for code in rows)


def test_validate_code_rejects_path_traversal() -> None:
    for bad in ("../x.py", "a/b.py", "..", "", "-x.py"):
        with pytest.raises(ScriptError):
            validate_code(bad)
    assert validate_code("pl_disk_check.py") == "pl_disk_check.py"


def test_normalize_content_rejects_empty_and_nul() -> None:
    with pytest.raises(ScriptError):
        normalize_content("   \n")
    with pytest.raises(ScriptError):
        normalize_content("echo a\x00b")
    with pytest.raises(ScriptError):
        normalize_content("x" * (MAX_SCRIPT_BYTES + 1))


def test_normalize_content_converts_crlf() -> None:
    """CRLF 必须归一：否则同一份内容在 Windows 编辑后摘要变化，任务证据无法比对。"""
    normalized = normalize_content("echo a\r\necho b\r\n")
    assert normalized == "echo a\necho b\n"
    assert sha256_of(normalized) == sha256_of("echo a\necho b\n")


def test_infer_interpreter_prefers_suffix() -> None:
    assert infer_interpreter("a.py", "#!/bin/bash") == "python"
    assert infer_interpreter("a.sh", "#!/usr/bin/env python3") == "bash"
    assert infer_interpreter("dpcollect", "#!/bin/bash\n") == "bash"
    assert infer_interpreter("custom", "#!/usr/bin/env python3\n") == "python"


def test_items_reference_only_matches_script_action() -> None:
    items = [
        {"action": "script", "path": "pl_a.py"},
        {"action": "copy", "path": "pl_a.py"},
        {"action": "script", "path": "pl_b.py"},
    ]
    assert _items_reference(items, "pl_a.py") is True
    assert _items_reference(items, "pl_c.py") is False
    assert _items_reference(None, "pl_a.py") is False
