"""KBD 统一 CLI 的 Stage 5/6 可发现性与文件审计回归。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from kbd import runtime
from kbd.pipeline import Stage
from kbd.run import (
    _cmd_audit_log_signals,
    _cmd_extract_signals,
    _cmd_pipeline,
    _parse_stages,
    build_parser,
)


def test_pipeline_stage_parser_includes_extract_and_audit():
    assert _parse_stages("extract-signals,audit-log-signals") == [
        Stage.EXTRACT_SIGNALS,
        Stage.AUDIT_LOG_SIGNALS,
    ]
    assert _parse_stages("5,6") == [Stage.EXTRACT_SIGNALS, Stage.AUDIT_LOG_SIGNALS]


def test_extract_signals_is_first_class_subcommand():
    args = build_parser().parse_args(["extract-signals", "--ids", "37150,41818"])

    assert args.command == "extract-signals"
    assert args.ids == "37150,41818"


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
