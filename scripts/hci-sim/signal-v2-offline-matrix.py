#!/usr/bin/env python3
"""逐篇执行原始 Signal v2 五样例的完整离线采集与诊断闭环。"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB_INSTANCE = "signal-v2-offline"


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True, text=True)


def reset_lab() -> None:
    run_dir = ROOT / ".hci-sim-run" / "lab" / LAB_INSTANCE
    if run_dir.exists():
        run(
            [
                str(ROOT / ".venv/bin/python"),
                str(ROOT / "scripts/hci-sim/diagnosis-lab.py"),
                "reset",
                "--instance",
                LAB_INSTANCE,
            ]
        )


def stop_stack(args: argparse.Namespace) -> None:
    run(
        [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "scripts/hci-sim/semantic-e2e-stack.py"),
            "--run-id",
            args.run_id,
            "--database-name",
            args.database_name,
            "--down",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--database-name", required=True)
    parser.add_argument("--base-url", default="http://localhost:3003")
    parser.add_argument("--variant", default="positive")
    parser.add_argument("--attempt", default="", help="显式指定本轮结果后缀；默认使用时间戳")
    parser.add_argument("--leave-running", action="store_true", help="完成或失败后保留隔离服务栈")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", args.run_id):
        raise SystemExit("run-id 不合法")
    if not re.fullmatch(r"hci_semantic_e2e_[a-z0-9_]{1,40}", args.database_name):
        raise SystemExit("database-name 必须是独立 hci_semantic_e2e_ 数据库")
    if args.variant != "positive":
        raise SystemExit("全量回归矩阵当前只接受 positive；其它故障注入请使用分阶段命令")
    attempt = args.attempt or f"matrix-{int(time.time())}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", attempt):
        raise SystemExit("attempt 不合法")

    run_dir = ROOT / ".hci-sim-run" / "semantic-e2e" / args.run_id
    suite = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    cases = suite.get("cases", [])
    expected_case_count = int(suite.get("expected_case_count", 5))
    if suite.get("suite_kind") != "full":
        raise SystemExit("该命令只验收原始在线/离线诊断 Signal v2 全量样例")
    if len(cases) != expected_case_count:
        raise SystemExit(f"离线矩阵要求 {expected_case_count} 篇样例，实际 {len(cases)} 篇")

    python = str(ROOT / ".venv/bin/python")
    stack = str(ROOT / "scripts/hci-sim/semantic-e2e-stack.py")
    lab = str(ROOT / "scripts/hci-sim/diagnosis-lab.py")
    stage = str(ROOT / "scripts/hci-sim/semantic-e2e-offline.py")
    summary_path = run_dir / f"offline-matrix-{attempt}.json"
    summary = {
        "check": "signal-v2-offline-matrix",
        "status": "running",
        "run_id": args.run_id,
        "attempt": attempt,
        "suite_kind": "full",
        "source_sample_suite": suite.get("source_sample_suite"),
        "cases": [],
    }

    def save() -> None:
        temporary = summary_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, summary_path)

    save()
    try:
        run(
            [
                python,
                stack,
                "--run-id",
                args.run_id,
                "--database-name",
                args.database_name,
                "--sync-offline-resources",
            ]
        )
        for index, case in enumerate(cases, start=1):
            reset_lab()
            env = {
                **os.environ,
                "HCI_SIM_SCENARIO_PROFILE": str(run_dir / "scenario-profile.json"),
                "HCI_SIM_CAPABILITIES_URL": "http://127.0.0.1:18024/api/kb/hci-sim/capabilities",
            }
            run(
                [
                    python,
                    lab,
                    "up",
                    "--scenario",
                    str(case["support_id"]),
                    "--instance",
                    LAB_INSTANCE,
                    "--variant",
                    args.variant,
                ],
                env=env,
            )
            common = [
                python,
                stage,
                "--run-id",
                args.run_id,
                "--support-id",
                str(case["support_id"]),
                "--instance",
                LAB_INSTANCE,
                "--base-url",
                args.base_url,
                "--attempt",
                attempt,
            ]
            try:
                for stage_name in ("prepare", "collect", "upload", "verify"):
                    run([*common, "--stage", stage_name])
            finally:
                reset_lab()
            result_path = run_dir / f"offline-{case['support_id']}-{attempt}.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            verification = result.get("verification") or {}
            if verification.get("status") != "PASS":
                raise RuntimeError(f"{case['source_support_id']} 未形成 PASS 验收结果")
            row = {
                "index": index,
                "support_id": case["support_id"],
                "source_support_id": case["source_support_id"],
                "case_id": verification["case_id"],
                "entry_mode": verification["entry_mode"],
                "signal_count": verification["signal_count"],
                "status": "PASS",
                "result_file": str(result_path),
            }
            summary["cases"].append(row)
            save()
            print(json.dumps({"check": "signal-v2-offline-matrix-case", **row}, ensure_ascii=False), flush=True)
        summary["status"] = "PASS"
        summary["passed"] = len(summary["cases"])
        save()
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    except BaseException as exc:
        summary["status"] = "FAILED"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        save()
        raise
    finally:
        with contextlib.suppress(Exception):
            reset_lab()
        if not args.leave_running:
            with contextlib.suppress(Exception):
                stop_stack(args)


if __name__ == "__main__":
    main()
