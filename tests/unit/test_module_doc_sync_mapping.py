"""`scripts/ci/check_module_doc_sync.py` 模块→文档映射的回归测试。

背景（真实缺陷）：历史上存在一条 `database/desired_schema.sql` 特例映射，把 schema
变更指向 agent 文档；由于解析是「按声明顺序 startswith 取首个命中」，该特例遮蔽了
更宽泛的 `database/` → 《数据库设计.md》规则，导致改 schema 时门禁拒绝真正的数据库
设计文档、却要求去改无关的 agent 文档。遮蔽本身不报错，缺陷因此长期存活。

本测试锁定「具体优先」的解析结果与文档可达性，防止同类遮蔽再次发生。

运行：uv run pytest tests/unit/test_module_doc_sync_mapping.py

命名约定：函数名使用 ASCII（ruff N802 将非 ASCII 标识符视为大写）；中文说明写入 docstring。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "ci" / "check_module_doc_sync.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    """按文件路径加载门禁脚本（scripts/ 不是 Python 包，无法直接 import）。"""
    spec = importlib.util.spec_from_file_location("check_module_doc_sync", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"无法加载门禁脚本: {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schema_change_maps_to_database_design_doc(gate: ModuleType) -> None:
    """desired_schema.sql 必须落在通用 database/ 规则上，而不是 agent 文档。"""
    mapping = gate.resolve_mapping("database/desired_schema.sql")

    assert mapping is not None, "desired_schema.sql 未命中任何映射"
    assert mapping.code_prefix == "database/"
    assert mapping.solution_doc == "docs/solution/数据库设计.md"


def test_data_migration_maps_to_database_design_doc(gate: ModuleType) -> None:
    """data-migrations/*.sql 与 schema 同属数据库域，应共用同一份设计文档。"""
    mapping = gate.resolve_mapping(
        "database/data-migrations/032_add_effect_verification.sql"
    )

    assert mapping is not None
    assert mapping.solution_doc == "docs/solution/数据库设计.md"


def test_seeds_maps_to_agent_doc(gate: ModuleType) -> None:
    """database/seeds/ 是 Prompt/工具定义，实质属 agent-service，不能被通用规则吞掉。"""
    mapping = gate.resolve_mapping("database/seeds/02_system_prompts.sql")

    assert mapping is not None
    assert mapping.code_prefix == "database/seeds/"
    assert mapping.solution_doc == "docs/solution/agent/design/agent工具设计.md"


def test_specific_prefix_wins_over_generic_prefix(gate: ModuleType) -> None:
    """acli 子目录映射必须优先于 backend/agent-service/ 通用映射。"""
    mapping = gate.resolve_mapping("backend/agent-service/app/tools/acli/client.py")

    assert mapping is not None
    assert mapping.code_prefix == "backend/agent-service/app/tools/acli/"


def test_generic_prefix_fallback_works(gate: ModuleType) -> None:
    """未命中任何具体映射的 agent-service 文件，应落到 backend/agent-service/。"""
    mapping = gate.resolve_mapping("backend/agent-service/app/services/runner.py")

    assert mapping is not None
    assert mapping.code_prefix == "backend/agent-service/"


def test_exempt_files_produce_no_mapping(gate: ModuleType) -> None:
    """测试文件、锁文件等豁免项不解析出映射。"""
    assert gate.resolve_mapping("backend/case-service/tests/test_case.py") is None
    assert gate.resolve_mapping("backend/case-service/requirements.lock") is None


def test_unmapped_path_returns_none(gate: ModuleType) -> None:
    """data-pipeline/ 等未纳入映射的目录不触发门禁。"""
    assert gate.resolve_mapping("data-pipeline/extract/signals.py") is None


def test_all_mapped_docs_exist(gate: ModuleType) -> None:
    """映射指向的文档必须真实存在，否则文档改名后门禁会永久失效。"""
    missing = [
        f"{mapping.code_prefix} -> {doc}"
        for mapping in gate.MODULE_MAPPINGS
        for doc in (mapping.solution_doc, mapping.task_doc)
        if doc and not (_ROOT / doc).is_file()
    ]

    assert not missing, "以下映射的目标文档不存在：\n" + "\n".join(missing)


def test_mapped_docs_live_under_docs(gate: ModuleType) -> None:
    """防止误把代码路径或仓库外路径写成文档目标。"""
    bad = [
        f"{mapping.code_prefix} -> {doc}"
        for mapping in gate.MODULE_MAPPINGS
        for doc in (mapping.solution_doc, mapping.task_doc)
        if doc and not doc.startswith("docs/")
    ]

    assert not bad, "以下映射的文档路径不在 docs/ 下：\n" + "\n".join(bad)
