"""KBD 统一 CLI 的 Stage 5/6 可发现性与文件审计回归。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from kbd import runtime
from kbd.pipeline import Stage, _display_width, _stage_banner
from kbd.run import (
    _choose_history_run_id,
    _cmd_extract_signals,
    _cmd_review_signals,
    _collect_interactive_task_args,
    _ConsoleFormatter,
    _parse_interactive_rework_statuses,
    _parse_stages,
    _print_pipeline_summary,
    _prompt_choice,
    _prompt_yes_no,
    build_parser,
)
from kbd.terminal_layout import TERMINAL_LAYOUT_WIDTH


def test_pipeline_stage_parser_includes_extract_and_review():
    assert _parse_stages("extract-signals,review-signals") == [
        Stage.EXTRACT_SIGNALS,
        Stage.REVIEW_SIGNALS,
    ]
    assert _parse_stages("5,6") == [Stage.EXTRACT_SIGNALS, Stage.REVIEW_SIGNALS]


def test_numeric_stage_parser_uses_classify_as_stage_three_and_vision_as_stage_four():
    assert _parse_stages("3,4") == [Stage.CLASSIFY, Stage.VISION]


def test_stage_banners_have_the_same_display_width_and_aligned_edges():
    banners = [
        _stage_banner(1, "数据抓取"),
        _stage_banner(2, "语义提取 + 原子入库"),
        _stage_banner(3, "AI 分类 + Stage 4: 图片语义化"),
        _stage_banner(5, "关键信号分级抽取"),
        _stage_banner(6, "Shared Runtime 全量 Signal 审查"),
    ]

    assert len({_display_width(banner) for banner in banners}) == 1
    assert _display_width(banners[0]) == TERMINAL_LAYOUT_WIDTH
    assert all(banner.startswith("=") and banner.endswith("=") for banner in banners)


def test_long_stage_banner_is_rendered_as_a_standalone_line(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    banner = _stage_banner(6, "Shared Runtime 全量 Signal 审查")
    formatter = _ConsoleFormatter("%(levelname)s %(message)s")
    rendered = formatter.format(logging.makeLogRecord({"levelname": "INFO", "msg": banner}))

    assert rendered == banner
    assert not rendered.startswith("INFO ")


def test_extract_signals_is_first_class_subcommand():
    args = build_parser().parse_args(["extract-signals", "--ids", "37150,41818"])

    assert args.command == "extract-signals"
    assert args.ids == "37150,41818"


def test_wizard_command_is_removed():
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["wizard", "--help"])

    assert exc_info.value.code == 2


def test_prompt_yes_no_accepts_standard_answers_and_default(monkeypatch):
    answers = iter(["y", "YES", "n", "NO", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert _prompt_yes_no("确认", default=False) is True
    assert _prompt_yes_no("确认", default=False) is True
    assert _prompt_yes_no("确认", default=True) is False
    assert _prompt_yes_no("确认", default=True) is False
    assert _prompt_yes_no("确认", default=True) is True


def test_cli_interactive_model_has_four_statuses_and_default_mode(monkeypatch):
    assert _parse_interactive_rework_statuses("") == "draft"
    assert _parse_interactive_rework_statuses("1,2,4") == "draft,published,archived"
    answers = iter(["1", "29351", "1", "1", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    args = _collect_interactive_task_args()
    assert args.ids == "29351"
    assert args.stages == "all"
    assert args.resume is False and args.failed is False and args.rework is None


def test_cli_parser_exposes_interactive_frontend():
    args = build_parser().parse_args(["cli"])
    assert args.command == "cli"


def test_cli_history_selector_uses_recent_manifest(monkeypatch, tmp_path):
    from kbd import task_state

    monkeypatch.setattr(task_state.settings, "KBD_LOGS_DIR", tmp_path)
    task_state.save_execution_manifest({
        "execution_id": "20260809_103000",
        "requested_ids": ["29351"],
        "mode": "failed",
    })
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    assert _choose_history_run_id() == "20260809_103000"


def test_prompt_choice_reprompts_until_a_numbered_option(monkeypatch):
    answers = iter(["label", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert _prompt_choice(
        "请选择：",
        (("1", "one", "第一项"), ("2", "two", "第二项")),
        default="1",
    ) == "two"


def test_pipeline_summary_table_has_consistent_visible_width(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _print_pipeline_summary(
        "20260806_161016",
        {
            "pipeline": {"success": False, "total_ids": 4, "completed_ids": 0},
            "fetch": {"done": 0, "failed": 0, "skipped": 4, "elapsed_s": 2.4},
            "import": {"created": 4, "overridden": 0, "skipped": 0, "error": 0, "elapsed_s": 0.6},
            "vision": {
                "done": 0,
                "failed": 4,
                "case_status_counts": {"done": 0, "failed": 4, "needs_review": 0},
                "elapsed_s": 64.3,
            },
            "classify": {"done": 0, "failed": 0, "skipped": 0, "elapsed_s": 0.0},
            "extract": {"done": 0, "blocked_by_dependency": 4, "elapsed_s": 0.1},
            "review_signals": {"case_count": 0, "issue_counts": {}, "elapsed_s": 0.0},
        },
    )

    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(("┌", "│", "├", "└"))
    ]
    assert lines
    assert len({_display_width(line) for line in lines}) == 1
    assert _display_width(lines[0]) == TERMINAL_LAYOUT_WIDTH
    assert all(line.count("│") == 9 for line in lines if line.startswith("│"))


def test_pipeline_summary_lists_classification_before_vision(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    _print_pipeline_summary(
        "20260808_073018",
        {
            "pipeline": {"success": True, "total_ids": 1, "completed_ids": 1},
            "vision": {"done": 4, "failed": 0, "elapsed_s": 25.1},
            "classify": {"done": 1, "failed": 0, "elapsed_s": 8.6},
        },
    )

    output = capsys.readouterr().out
    assert output.index("案例分类") < output.index("截图识别")


def test_task_parser_exposes_single_lifecycle_mode():
    parser = build_parser()
    args = parser.parse_args(["task", "--ids", "27582", "--failed", "--stages", "vision"])
    assert args.failed is True
    assert args.stages == "vision"
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "--ids", "27582", "--resume", "--failed"])


@pytest.mark.asyncio
async def test_extract_signals_protects_existing_or_unclassified_proposals(monkeypatch):
    import asyncpg
    from kbd import extract_signals

    class FakePool:
        def __init__(self) -> None:
            self.sql = ""
            self.closed = False

        async def fetch(self, sql: str, _ids: list[str]):
            self.sql = sql
            return [{"support_id": "37150"}]

        async def close(self):
            self.closed = True

    pool = FakePool()
    extracted: list[str] = []

    async def fake_create_pool(*, dsn: str):
        assert dsn
        return pool

    async def fake_extract(ids: list[str], actual_pool):
        extracted.extend(ids)
        assert actual_pool is pool
        return {"done": 1, "failed": 0, "skipped": 0, "needs_review": 0}

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(extract_signals, "extract_signals_batch", fake_extract)
    args = build_parser().parse_args(["extract-signals", "--ids", "37150,41818"])

    await _cmd_extract_signals(args, "20260730_120000")

    assert extracted == ["37150"]
    assert "status = 'draft'" in pool.sql
    assert "category_id" in pool.sql and "ai_category_id" in pool.sql
    assert "signals_json IS NULL" in pool.sql
    assert "'{}'::jsonb" in pool.sql
    assert "jsonb_array_length" in pool.sql
    assert pool.closed is True


def test_review_input_requires_exactly_one_source():
    parser = build_parser()
    args = parser.parse_args(["review-input", "--stdin", "--output", "report.json"])

    assert args.command == "review-input"
    assert args.stdin is True
    assert args.output == "report.json"

    with pytest.raises(SystemExit):
        parser.parse_args(["review-input"])
    with pytest.raises(SystemExit):
        parser.parse_args(["review-input", "--stdin", "--file", "x"])


@pytest.mark.asyncio
async def test_review_input_reads_file_and_writes_report(tmp_path):
    source = tmp_path / "signals.json"
    output = tmp_path / "report.json"
    source.write_text(
        json.dumps(
            [
                {
                    "support_id": "37150",
                    "signals_json": {
                        "signals": [
                            {
                                "id": "log_ok",
                                "acquire": {"tool": "qfk_log", "args": {"file": "messages"}},
                                "match": {"type": "keyword", "pattern": "failed"},
                            }
                        ]
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        ["review-input", "--file", str(source), "--output", str(output)]
    )

    exit_code = await _cmd_review_signals(args, "20260730_120000")

    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["review_engine"] == "shared_resolution_runtime"
    assert report["case_count"] == 1


@pytest.mark.asyncio
async def test_review_input_fail_on_blocked_is_opt_in(tmp_path):
    source = tmp_path / "blocked.json"
    source.write_text(
        json.dumps(
            [
                {
                    "support_id": "blocked",
                    "signals_json": {
                        "signals": [
                            {
                                "id": "missing_file",
                                "acquire": {"tool": "qfk_log", "args": {}},
                                "match": {"type": "exists"},
                            }
                        ]
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    parser = build_parser()
    normal = parser.parse_args(["review-input", "--file", str(source)])
    strict = parser.parse_args(
        ["review-input", "--file", str(source), "--fail-on-blocked"]
    )

    assert await _cmd_review_signals(normal, "20260730_120000") == 0
    assert await _cmd_review_signals(strict, "20260730_120001") == 1


def test_repository_module_entrypoint_bootstraps_shared_contract_without_pythonpath(tmp_path):
    """真实用户入口不应在 Stage 6 才因 backend/shared 导入失败。"""

    source = tmp_path / "signals.json"
    source.write_text(
        json.dumps(
            [
                {
                    "support_id": "40061",
                    "signals_json": {
                        "signals": [
                            {
                                "id": "log_ok",
                                "acquire": {"tool": "qfk_log", "args": {"file": "messages"}},
                                "match": {"type": "keyword", "pattern": "failed"},
                            }
                        ]
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository_root = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "data-pipeline.kbd.run",
            "review-input",
            "--file",
            str(source),
        ],
        cwd=repository_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"review_engine": "shared_resolution_runtime"' in result.stdout


def test_shared_contract_preflight_only_blocks_commands_that_need_stage_six(monkeypatch, tmp_path):
    """缺少共享契约必须得到明确错误，而不是等到审计模块 traceback。"""

    monkeypatch.setattr(runtime, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(runtime.sys, "path", list(runtime.sys.path))
    assert runtime.bootstrap_repository_imports() == tmp_path / "backend"

    with pytest.raises(RuntimeError, match="backend/shared"):
        runtime.require_shared_contracts()
