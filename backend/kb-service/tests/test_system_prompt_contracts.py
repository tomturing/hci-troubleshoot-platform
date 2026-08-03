"""热加载 Prompt 必须与当前保存 Schema、截图分类契约同步。"""

from pathlib import Path

from shared.utils.prompt_loader import StrictPromptLoader

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SEED_PATH = _REPOSITORY_ROOT / "database" / "seeds" / "02_system_prompts.sql"
_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "015_align_signal_and_vision_prompts_to_current_contract.sql"
)
_MATCHER_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "017_align_matcher_prompt_with_extract_contract.sql"
)
_QFK_SYSTEM_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "database" / "data-migrations" / "018_align_qfk_system_prompt_command_model.sql"
)


def _seed_template(prompt_name: str) -> str:
    content = _SEED_PATH.read_text(encoding="utf-8")
    marker = f"'{prompt_name}',"
    start = content.index(marker)
    end = content.index("$TEMPLATE$", start)
    end = content.index("$TEMPLATE$", end + len("$TEMPLATE$"))
    return content[start:end]


def test_signal_extract_prompt_only_teaches_declarative_text_extract_and_current_match_modes():
    template = _seed_template("kbd_extract_signals_v2")

    assert '"parser":"whitespace_table"' in template
    assert '"rows"' in template
    assert '"columns"' in template
    assert '"value_key"' in template
    assert "column_mode" not in template
    assert '"mode": "any"' not in template
    assert '"mode": "or"' in template
    assert '"type": "<keyword|regex|state|threshold|delta|trend|exists>"' in template
    assert "json_path" not in template
    assert "判定模式的 match 必须包含 extract" in template
    assert "字段严格隔离：rows.include_mode" in template
    assert "绝不可把 any 或 all 写入 match.mode" in template
    assert '"pattern": "ClwDRDBClient", "mode": "or", "expected": true, "extract"' in template


def test_signal_extract_prompt_json_examples_escape_format_braces():
    template = _seed_template("kbd_extract_signals_v2")

    assert StrictPromptLoader.get_template_placeholders(template) == {
        "title",
        "category_id",
        "problem_description",
        "alert_info",
        "steps_text",
        "root_cause",
        "solution",
        "image_evidence",
        "acquirer_catalog",
        "variable_schema",
    }


def test_prompt_migration_escapes_declarative_extract_json_examples():
    migration = _MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$,", 1)[0]

    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_matcher_prompt_migration_escapes_declarative_extract_json_examples():
    migration = _MATCHER_MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$,", 1)[0]

    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_qfk_system_prompt_migration_teaches_current_command_model():
    migration = _QFK_SYSTEM_MIGRATION_PATH.read_text(encoding="utf-8")
    supplemental_rule = migration.split("|| $RULE$", 1)[1].split("$RULE$,", 1)[0]

    assert "补充规则 22：qfk_system 命令模型与资源字段隔离" in supplemental_rule
    assert "qfk_system 禁止 resource_keyword" in supplemental_rule
    assert "command_args 字符串数组" in supplemental_rule
    assert "produces.extract.rows.include" in supplemental_rule
    assert "timeout=120" in supplemental_rule
    assert StrictPromptLoader.get_template_placeholders(supplemental_rule) == set()


def test_vision_prompt_gives_task_detail_modal_task_semantic_priority():
    template = _seed_template("kbd_vision_v1")

    assert "任务详情可以用弹窗展示" in template
    assert "任务详情弹窗必须判为任务截图" in template
    assert "只有没有可识别任务或告警记录的通用失败提示，才判为弹框截图" in template
