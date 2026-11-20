"""收集脚本库的纯函数边界测试。

只覆盖不依赖数据库的部分：基线快照扫描、文件名/内容校验、解释器推断、
引用识别。数据库版本化语义由路由集成测试覆盖（需要真实 session）。
"""

from __future__ import annotations

import pytest
from app.services.log_collect_script_service import (
    MAX_SCRIPT_BYTES,
    LogCollectScriptService,
    ScriptError,
    _items_reference,
    baseline_content,
    baseline_scripts,
    infer_interpreter,
    normalize_content,
    resolve_interpreter,
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


def test_validate_code_allows_subdir_but_rejects_traversal() -> None:
    """code 是相对 script/ 的路径：内置脚本住在 vs_script/ 等子目录里。"""
    for good in ("pl_disk_check.py", "dpcollect", "vs_script/vs_collect_upgrade_info.py", "tools/sub/x.sh"):
        assert validate_code(good) == good
    for bad in (
        "../x.py",
        "..",
        "",
        "-x.py",
        ".hidden",
        "/abs/x.py",
        "a/",
        "a//b.py",
        "a\\b.py",
        "a/b/c/d.py",
    ):
        with pytest.raises(ScriptError):
            validate_code(bad)


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


def test_infer_interpreter_prefers_shebang() -> None:
    """shebang 是脚本自己声明的执行方式，后缀只是弱证据。

    后缀优先的旧实现会把「.sh 里其实是 python」判成 bash，且让无后缀脚本
    （插件里大量存在）无法表达真实解释器。
    """
    assert infer_interpreter("a.py", "#!/bin/bash") == "bash"
    assert infer_interpreter("a.sh", "#!/usr/bin/env python3") == "python"
    assert infer_interpreter("dpcollect", "#!/bin/bash\n") == "bash"
    assert infer_interpreter("custom", "#!/usr/bin/env python3\n") == "python"
    # 无 shebang 时退回后缀
    assert infer_interpreter("a.py", "print(1)") == "python"
    assert infer_interpreter("a.sh", "echo a") == "bash"
    # 无后缀无 shebang：默认 bash
    assert infer_interpreter("dpdoctor", "echo a") == "bash"


def test_resolve_interpreter_manual_wins_with_warning() -> None:
    interp, source, warnings = resolve_interpreter("a.sh", "#!/bin/bash\n", "python")
    assert (interp, source) == ("python", "manual")
    assert any("shebang" in item for item in warnings)


def test_resolve_interpreter_explains_default_and_conflicts() -> None:
    interp, source, warnings = resolve_interpreter("dpdoctor", "echo a")
    assert (interp, source) == ("bash", "default")
    assert any("无后缀" in item for item in warnings)

    interp, source, warnings = resolve_interpreter("a.sh", "#!/usr/bin/env python3\n")
    assert (interp, source) == ("python", "shebang")
    assert any("后缀" in item for item in warnings)

    with pytest.raises(ScriptError):
        resolve_interpreter("a.sh", "echo a", "perl")


def test_items_reference_only_matches_script_action() -> None:
    items = [
        {"action": "script", "path": "pl_a.py"},
        {"action": "copy", "path": "pl_a.py"},
        {"action": "script", "path": "pl_b.py"},
    ]
    assert _items_reference(items, "pl_a.py") is True
    assert _items_reference(items, "pl_c.py") is False
    assert _items_reference(None, "pl_a.py") is False


class _ExplodingSession:
    """任何查库都算失败：用于证明无脚本条目时校验不会触碰数据库。"""

    async def execute(self, *args, **kwargs):
        raise AssertionError("无 action=script 条目时不应查库")


async def test_assert_scenario_items_skips_db_without_script_items() -> None:
    service = LogCollectScriptService(_ExplodingSession())  # type: ignore[arg-type]
    items = [
        {"action": "copy", "path": "/sf/log/cluster/"},
        {"action": "spc", "path": "demo_network.sh"},
        {"action": "copy", "path": ""},
    ]
    assert await service.assert_scenario_items(items) == []
    assert items[1]["path"] == "demo_network.sh"
