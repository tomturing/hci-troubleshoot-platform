"""
backend/kb-service/tests/test_signal_multi_agent_prompts.py
验证关键信号多 Agent 分层抽取的 4 个系统提示词占位符契约与规则约束。
"""
from pathlib import Path
import pytest
from shared.utils.prompt_loader import StrictPromptLoader

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_PATH = (
    _REPO_ROOT
    / "database"
    / "atlas-migrations"
    / "20260904000001_seed_multi_agent_extract_prompts.sql"
)


def _load_prompt_template_from_migration(prompt_name: str) -> str:
    content = _MIGRATION_PATH.read_text(encoding="utf-8")
    marker = f"'{prompt_name}',"
    start_pos = content.index(marker)
    template_start = content.index("$TEMPLATE$", start_pos) + len("$TEMPLATE$")
    template_end = content.index("$TEMPLATE$", template_start)
    return content[template_start:template_end]


def test_count_agent_prompt_placeholders_and_contracts():
    """验证计数 Agent 提示词占位符与角色解耦契约"""
    template = _load_prompt_template_from_migration("kbd_signal_count_v1")
    placeholders = StrictPromptLoader.get_template_placeholders(template)
    
    assert placeholders == {"composite_text", "steps_text"}
    assert "纯内容驱动" in template
    assert "角色感知去重" in template
    assert "严禁互相去重覆盖" in template
    assert "signal_count" in template
    assert "intents" in template


def test_classify_agent_prompt_placeholders_and_contracts():
    """验证分类 Agent 提示词占位符与双重视角对抗审查契约"""
    template = _load_prompt_template_from_migration("kbd_signal_classify_v1")
    placeholders = StrictPromptLoader.get_template_placeholders(template)
    
    assert placeholders == {
        "core_entity",
        "evidence_raw",
        "composite_text",
        "steps_text",
        "acquirer_catalog",
        "category_baseline",
    }
    assert "双重视角对抗审查" in template
    assert "动词优先律" in template
    assert "任务优先律" in template
    assert "qkv_task" in template
    assert "qfk_log" in template
    assert "unclassified" in template


def test_model_agent_prompt_placeholders_and_contracts():
    """验证建模 Agent 提示词占位符与最佳实践注入契约"""
    template = _load_prompt_template_from_migration("kbd_signal_model_v1")
    placeholders = StrictPromptLoader.get_template_placeholders(template)
    
    assert placeholders == {
        "tool_name",
        "core_entity",
        "evidence_raw",
        "shared_variables",
        "best_practices",
        "acli_catalog",
    }
    assert "acquire 标准化" in template
    assert "orchestrate 变量协议约束" in template
    assert "禁止在 qfk_system 命令（date/uptime/ps 等）中使用无实质过滤的 exists: true 恒真伪断言" in template
    assert "threshold Matcher" in template


def test_verify_agent_prompt_placeholders_and_contracts():
    """验证裁判 Agent 提示词占位符与门禁自愈契约"""
    template = _load_prompt_template_from_migration("kbd_signal_verify_v1")
    placeholders = StrictPromptLoader.get_template_placeholders(template)
    
    assert placeholders == {
        "signals_json",
        "rejected_candidates",
        "raw_count",
        "kbd_context",
        "gate_issues",
    }
    assert "数量刚性对账" in template
    assert "变量 DAG 连通性" in template
    assert "门禁错误自愈" in template
    assert "verification_status" in template
