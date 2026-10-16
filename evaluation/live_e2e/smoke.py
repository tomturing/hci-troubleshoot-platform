"""硬盘坏道 Live E2E Smoke —— CLI 编排入口（发布前 gate，非每 PR CI）。

用法：
    uv run python -m evaluation.live_e2e.smoke \
        --scenario evaluation/live_e2e/scenarios/disk_badtrack.json \
        --mode stub --attempts 3

设计（第一性原理）：
- 真实 LLM 非确定性 → 每个场景跑 `--attempts` 次，多数表决（≥2/3 通过判绿），
  把偶发漂移与真回归区分开。
- `--mode stub` 时由 disk_badtrack_llm_stub 确定性注入历史坏输出，断言引擎守卫**必须**
  拦住（expect_grounding_filter=True）；`--mode real` 仅观测真实模型是否被守卫约束。
- 断言只打在结构不变量 + 守卫事件上（见 asserts.evaluate_invariants），绝不断言逐字文本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from evaluation.live_e2e import asserts
from evaluation.live_e2e.driver import DriverConfig, run_scenario

ROOT = Path(__file__).resolve().parents[2]


def _resolved_variable_names(persisted: dict[str, Any]) -> set[str]:
    """从后端变量池真值中提取「已就绪」变量名（值非空即视为已解决）。"""
    resolved: set[str] = set()
    for name, value in (persisted or {}).items():
        raw = value.get("value") if isinstance(value, dict) else value
        if raw not in (None, "", [], {}):
            resolved.add(name)
    return resolved


def _build_config(args: argparse.Namespace) -> DriverConfig:
    connection: dict[str, Any] | None = None
    if args.sim:
        conn_path = Path(args.connection) if args.connection else None
        if conn_path is None or not Path(conn_path).is_file():
            raise SystemExit("--sim 需要 --connection 指向 diagnosis-lab 生成的 connection.json")
        connection = json.loads(Path(conn_path).read_text(encoding="utf-8"))
    return DriverConfig(
        case_base=args.case_base or os.getenv("LIVE_E2E_CASE_BASE", "http://case-service:8001"),
        conv_base=args.conv_base or os.getenv("LIVE_E2E_CONV_BASE", "http://conversation-service:8002"),
        agent_base=args.agent_base or os.getenv("LIVE_E2E_AGENT_BASE", "http://agent-service:8005"),
        client_id=args.client_id or os.getenv("LIVE_E2E_CLIENT_ID", "live-e2e-smoke"),
        internal_token=args.internal_token or os.getenv("INTERNAL_API_TOKEN", ""),
        assistant_type=os.getenv("LIVE_E2E_ASSISTANT_TYPE", "htp-agent"),
        max_turns=args.max_turns,
        sim=args.sim,
        bridge_url=args.bridge_url or os.getenv("LIVE_E2E_BRIDGE_URL", "ws://127.0.0.1:9999"),
        bridge_origin=args.bridge_origin or os.getenv("LIVE_E2E_BRIDGE_ORIGIN"),
        connection=connection,
    )


def _evaluate(spec: dict[str, Any], obs, mode: str, sim: bool = False) -> dict[str, list[str]]:
    expect = spec.get("expect") or {}
    declared = set((spec.get("variables") or {}).get("declared") or [])
    resolved = _resolved_variable_names(obs.persisted_variables) | set(obs.submitted_variables)
    return asserts.evaluate_invariants(
        observed_category_code=obs.observed_category_code,
        expected_category_code=expect.get("category_code"),
        sop_document_hit_id=obs.sop_document_hit_id,
        expected_sop_id=expect.get("sop_document_id"),
        submitted_variables=obs.submitted_variables,
        persisted_variables=obs.persisted_variables,
        final_user_questions=obs.final_user_questions,
        resolved_variables=resolved,
        declared_variables=declared,
        guard_events=obs.guard_events,
        expect_grounding_filter=(mode == "stub"),
        collected_exec_ids=obs.collected_exec_ids,
        expect_real_collection=sim,
    )


async def _run(args: argparse.Namespace, spec: dict[str, Any]) -> int:
    config = _build_config(args)
    if not config.internal_token:
        print("[live-e2e] 缺少 INTERNAL_API_TOKEN（身份签名/落库查询必需）", file=sys.stderr)
        return 2
    dump_dir = Path(args.dump_dir) if args.dump_dir else ROOT / ".local" / "live_e2e"
    attempts = max(1, args.attempts)
    pass_count = 0
    for i in range(1, attempts + 1):
        print(f"[live-e2e] attempt {i}/{attempts} mode={args.mode} ...")
        obs = await run_scenario(config, spec, dump_dir=dump_dir / f"attempt-{i}", on_progress=lambda m: print(f"  - {m}"))
        if obs.error:
            print(f"[live-e2e] attempt {i} 驱动错误: {obs.error}", file=sys.stderr)
        result = _evaluate(spec, obs, args.mode, args.sim)
        for name in result["passed"]:
            print(f"  ✓ {name}")
        for name in result["failed"]:
            print(f"  ✗ {name}", file=sys.stderr)
        if not result["failed"] and obs.terminal and not obs.error:
            pass_count += 1

    needed = math.ceil(attempts * 2 / 3)
    green = pass_count >= needed
    print(
        f"[live-e2e] 多数表决: {pass_count}/{attempts} 通过（判绿需 ≥{needed}）→ "
        + ("GREEN ✅" if green else "RED 🔴")
    )
    return 0 if green else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="硬盘坏道 Live E2E Smoke（L3）")
    parser.add_argument("--scenario", default=str(ROOT / "evaluation" / "live_e2e" / "scenarios" / "disk_badtrack.json"))
    parser.add_argument("--mode", choices=["stub", "real"], default=os.getenv("LIVE_E2E_MODE", "stub"))
    parser.add_argument("--attempts", type=int, default=int(os.getenv("LIVE_E2E_ATTEMPTS", "3")))
    parser.add_argument("--max-turns", dest="max_turns", type=int, default=8)
    parser.add_argument("--case-base", dest="case_base", default=None)
    parser.add_argument("--conv-base", dest="conv_base", default=None)
    parser.add_argument("--agent-base", dest="agent_base", default=None)
    parser.add_argument("--client-id", dest="client_id", default=None)
    parser.add_argument("--internal-token", dest="internal_token", default=None)
    parser.add_argument("--dump-dir", dest="dump_dir", default=None)
    # 真实采集（headless bridge relay）：命令经 terminal_bridge 真下发到 hci-sim 节点，替掉静态 answers。
    parser.add_argument("--sim", action="store_true", default=os.getenv("LIVE_E2E_SIM") == "real",
                        help="启用真实采集：接 agent_exec_command → bridge → hci-sim 节点 → /exec-result")
    parser.add_argument("--bridge-url", dest="bridge_url", default=None)
    parser.add_argument("--bridge-origin", dest="bridge_origin", default=None)
    parser.add_argument("--connection", dest="connection", default=None,
                        help="diagnosis-lab connection.json 路径（--sim 必需）")
    args = parser.parse_args(argv)

    spec_path = Path(args.scenario)
    if not spec_path.is_absolute():
        spec_path = ROOT / spec_path
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return asyncio.run(_run(args, spec))


if __name__ == "__main__":
    raise SystemExit(main())
