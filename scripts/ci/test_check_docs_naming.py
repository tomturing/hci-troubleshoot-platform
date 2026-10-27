"""check_docs_naming 结果分级的回归测试。

防护目标：**建议级提示不得阻断合并**。

回归背景：`main()` 曾把 `check_file()` 返回的 ❌（违规）与 ⚠️（建议）
混在同一个 `errors` 列表里，只要有任意一条就 `exit 1`。而 ⚠️ 的来源是
`BRANCH_APPROVED_STEMS` —— 一张手写白名单，天然滞后于文档增长。
实测 `--full` 在 main 上带着 6 条存量 ⚠️ 常年红灯，于是没人再跑全量模式，
`❌` 检查（版本号后缀、点分隔符、泛化文件名）也随之失去价值。

一条永远失败的门禁与没有门禁等价。本组测试把分级行为钉住：
❌ 阻断，⚠️ 只提示。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "check_docs_naming.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_docs_naming", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_split_findings_separates_blocking_from_advisory():
    """❌ 进阻断桶，⚠️ 进建议桶，互不混淆。"""
    module = _load_module()
    findings = [
        "❌ docs/solution/agent/接口设计_v3.md\n   带版本号后缀",
        "⚠️  docs/solution/agent/关键信号架构落地设计.md\n   文件名干不在白名单内",
    ]

    blocking, advisory = module.split_findings(findings)

    assert len(blocking) == 1
    assert blocking[0].startswith("❌")
    assert len(advisory) == 1
    assert advisory[0].startswith("⚠️")


def test_split_findings_only_advisory_yields_empty_blocking():
    """只有建议项时，阻断桶必须为空——这正是全量模式能变绿的前提。"""
    module = _load_module()
    findings = ["⚠️  docs/solution/knowledge-base/关键信号架构设计.md\n   不在白名单内"]

    blocking, advisory = module.split_findings(findings)

    assert blocking == []
    assert len(advisory) == 1


def test_version_suffix_filename_is_blocking():
    """版本号后缀是硬违规，必须落进阻断桶，不能因为「文件已存在很久」被降级。"""
    module = _load_module()

    blocking, advisory = module.split_findings(
        module.check_file(Path("docs/solution/agent/离线诊断模式数据库迁移说明_V1.md"))
    )

    assert blocking, "版本号后缀应产生 ❌"
    assert all("版本号" in b for b in blocking)
    assert advisory == []


def test_whitelist_miss_is_advisory_only():
    """文件名干不在白名单只是建议：白名单是手写清单，滞后不该阻断正确命名。"""
    module = _load_module()

    blocking, advisory = module.split_findings(module.check_file(Path("docs/solution/agent/关键信号架构落地设计.md")))

    assert blocking == []
    assert len(advisory) == 1


def test_generic_filename_is_blocking():
    """分支目录下叫「设计.md」是硬违规。"""
    module = _load_module()

    blocking, _ = module.split_findings(module.check_file(Path("docs/solution/case/设计.md")))

    assert blocking
    assert "泛化文件名" in blocking[0]


def test_events_and_archive_are_skipped():
    """事件文档与归档件不受命名规范约束。"""
    module = _load_module()

    assert module.check_file(Path("docs/solution/events/2026-04-05-X方案.md")) == []
    assert module.check_file(Path("docs/archive/09_项目进展.md")) == []
