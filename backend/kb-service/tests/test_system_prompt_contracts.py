"""热加载 Prompt 必须与当前保存 Schema、截图分类契约同步。"""

from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SEED_PATH = _REPOSITORY_ROOT / "database" / "seeds" / "02_system_prompts.sql"


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


def test_vision_prompt_gives_task_detail_modal_task_semantic_priority():
    template = _seed_template("kbd_vision_v1")

    assert "任务详情可以用弹窗展示" in template
    assert "任务详情弹窗必须判为任务截图" in template
    assert "只有没有可识别任务或告警记录的通用失败提示，才判为弹框截图" in template
