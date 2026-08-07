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
    _cli_options,
    _cmd_audit_log_signals,
    _cmd_cli,
    _cmd_extract_signals,
    _cmd_pipeline,
    _ConsoleFormatter,
    _parse_stages,
    _print_pipeline_summary,
    _prompt_choice,
    _prompt_yes_no,
    build_parser,
)
from kbd.terminal_layout import TERMINAL_LAYOUT_WIDTH


def test_pipeline_stage_parser_includes_extract_and_audit():
    assert _parse_stages("extract-signals,audit-log-signals") == [
        Stage.EXTRACT_SIGNALS,
        Stage.AUDIT_LOG_SIGNALS,
    ]
    assert _parse_stages("5,6") == [Stage.EXTRACT_SIGNALS, Stage.AUDIT_LOG_SIGNALS]


def test_stage_banners_have_the_same_display_width_and_aligned_edges():
    banners = [
        _stage_banner(1, "数据抓取"),
        _stage_banner(2, "语义提取 + 原子入库"),
        _stage_banner(5, "关键信号分级抽取"),
        _stage_banner(6, "qfk_log Proposal 只读契约审计"),
    ]

    assert len({_display_width(banner) for banner in banners}) == 1
    assert _display_width(banners[0]) == TERMINAL_LAYOUT_WIDTH
    assert all(banner.startswith("=") and banner.endswith("=") for banner in banners)


def test_long_stage_banner_is_rendered_as_a_standalone_line(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    banner = _stage_banner(6, "qfk_log Proposal 只读契约审计")
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


def test_cli_typical_uses_safe_pipeline_defaults(monkeypatch):
    answers = iter([""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert _cli_options() == {
        "force_fetch": False,
        "override": False,
        "override_status": None,
        "resume": False,
        "failed_only": False,
    }


def test_cli_custom_uses_numbered_choices(monkeypatch):
    # mode=2, force_fetch=no, override=yes, scope=1(draft), resume=yes, failed_only=no
    answers = iter(["2", "n", "y", "1", "y", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert _cli_options() == {
        "force_fetch": False,
        "override": True,
        "override_status": ["draft"],
        "resume": True,
        "failed_only": False,
    }


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
            "audit_log_signals": {"case_count": 0, "issue_counts": {}, "elapsed_s": 0.0},
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


@pytest.mark.asyncio
async def test_cli_typical_passes_no_force_or_override_to_pipeline(monkeypatch):
    from kbd import config, pipeline

    class Tty:
        @staticmethod
        def isatty() -> bool:
            return True

    calls: dict[str, object] = {}

    async def fake_run_pipeline(ids, **kwargs):
        calls["ids"] = ids
        calls.update(kwargs)
        return {"pipeline": {"success": True, "completed_ids": 1, "total_ids": 1}}, "20260806_160000"

    monkeypatch.setattr("kbd.run.sys.stdin", Tty())
    monkeypatch.setattr(config.settings, "INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    answers = iter(["37150", "", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    args = build_parser().parse_args(["cli"])
    assert await _cmd_cli(args, "20260806_160000") == 0
    assert calls == {
        "ids": ["37150"],
        "run_id": "20260806_160000",
        "force_fetch": False,
        "override": False,
        "override_status": None,
        "resume": False,
        "failed_only": False,
    }


@pytest.mark.asyncio
async def test_pipeline_command_returns_nonzero_when_critical_stage_failed(monkeypatch):
    from kbd import pipeline

    async def fake_run_pipeline(*_args, **_kwargs):
        return {
            "import": {"error": 1},
            "pipeline": {"success": False, "failed_steps": 1},
        }, "20260806_120000"

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    args = build_parser().parse_args(["pipeline", "--ids", "27582"])

    assert await _cmd_pipeline(args, "20260806_120000") == 1


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
    assert pool.closed is True


def test_audit_log_signals_requires_exactly_one_source():
    parser = build_parser()
    args = parser.parse_args(["audit-log-signals", "--all", "--output", "report.json"])

    assert args.command == "audit-log-signals"
    assert args.all is True
    assert args.output == "report.json"

    with pytest.raises(SystemExit):
        parser.parse_args(["audit-log-signals"])
    with pytest.raises(SystemExit):
        parser.parse_args(["audit-log-signals", "--all", "--stdin"])


@pytest.mark.asyncio
async def test_audit_log_signals_reads_file_and_writes_report(tmp_path):
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
        ["audit-log-signals", "--file", str(source), "--output", str(output)]
    )

    exit_code = await _cmd_audit_log_signals(args, "20260730_120000")

    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["case_status_counts"] == {"PASS_LOG_CONTRACT": 1}


@pytest.mark.asyncio
async def test_audit_log_signals_fail_on_blocked_is_opt_in(tmp_path):
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
    normal = parser.parse_args(["audit-log-signals", "--file", str(source)])
    strict = parser.parse_args(
        ["audit-log-signals", "--file", str(source), "--fail-on-blocked"]
    )

    assert await _cmd_audit_log_signals(normal, "20260730_120000") == 0
    assert await _cmd_audit_log_signals(strict, "20260730_120001") == 1


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
            "audit-log-signals",
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
    assert '"PASS_LOG_CONTRACT": 1' in result.stdout


def test_shared_contract_preflight_only_blocks_commands_that_need_stage_six(monkeypatch, tmp_path):
    """缺少共享契约必须得到明确错误，而不是等到审计模块 traceback。"""

    monkeypatch.setattr(runtime, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(runtime.sys, "path", list(runtime.sys.path))
    assert runtime.bootstrap_repository_imports() == tmp_path / "backend"

    with pytest.raises(RuntimeError, match="backend/shared"):
        runtime.require_shared_contracts()
