#!/usr/bin/env python3
"""Q2026102600003 端到端验收：SOP#3 虚拟机开机失败。

复用 evaluation/live_e2e 的真实会话驱动（建案 → 建会话 → SSE → 变量卡提交 → 续跑），
在原有不变量之外补两条**针对本工单**的判据：

  1. **卡片变量名必须已声明**：任何 interactive_request 的 variable_name 必须出现在
     sop_document.variable_schema 里。历史缺陷是下发了「任务原始数据」这种自然语言
     伪变量——该名字在 schema 中不存在，用户填的值写进野键，SOP 永远消费不到。
     （原有 asserts 用 `_VAR_TOKEN_RE` 只认 ASCII 下划线 token，识别不了中文伪变量，
      所以必须单独断言。）
  2. **提交后不得重复索要**：已提交成功的变量名不得在后续轮次再次出现。

用法（在能访问 case/conversation/agent 服务的机器上）::

    uv run python scripts/verify/vm_start_failure_e2e.py \\
        --case-base http://127.0.0.1:18001 \\
        --conv-base http://127.0.0.1:18002 \\
        --agent-base http://127.0.0.1:18005 \\
        --internal-token "$INTERNAL_API_TOKEN" \\
        --dump-dir /tmp/e2e-dump
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from evaluation.live_e2e import asserts  # noqa: E402
from evaluation.live_e2e.driver import DriverConfig, run_scenario  # noqa: E402

DEFAULT_SPEC = ROOT / "evaluation" / "live_e2e" / "scenarios" / "vm_start_failure.json"


def _extract_variable_requests(events: list[dict]) -> list[str]:
    """按时间顺序提取所有变量卡请求的变量名。"""
    names: list[str] = []
    for event in events:
        if event.get("event") != "interactive_request":
            continue
        req = asserts.normalize_interactive_request(event.get("data") or {})
        if asserts.is_variable_request(req):
            names.append(req.get("variable") or "")
    return names


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default=str(DEFAULT_SPEC))
    parser.add_argument("--case-base", default=os.getenv("LIVE_E2E_CASE_BASE", "http://127.0.0.1:18001"))
    parser.add_argument("--conv-base", default=os.getenv("LIVE_E2E_CONV_BASE", "http://127.0.0.1:18002"))
    parser.add_argument("--agent-base", default=os.getenv("LIVE_E2E_AGENT_BASE", "http://127.0.0.1:18005"))
    parser.add_argument("--internal-token", default=os.getenv("INTERNAL_API_TOKEN", ""))
    parser.add_argument("--client-id", default=os.getenv("LIVE_E2E_CLIENT_ID", "live-e2e-smoke"))
    parser.add_argument("--max-turns", dest="max_turns", type=int, default=8)
    parser.add_argument("--dump-dir", dest="dump_dir", default=None)
    # ── 真实采集（headless bridge relay）──────────────────────────────────
    # 不启用时 driver 的 on_exec_command 为 None，会话 SSE 上的 agent_exec_command
    # 没有任何执行端接手，agent-service 只能等 AGENT_EXEC_RESULT_TIMEOUT_SECONDS
    # （默认 150s，日志 exec_result_timeout timeout_sec=140）后重试，SSE 流不结束，
    # 脚本会一直挂到外部 timeout 被杀，且 run_scenario 正常返回才写 dump，
    # 于是连事件证据都拿不到。要对"命令真下发→真执行→结果回贴"做验收，必须启用。
    parser.add_argument(
        "--sim",
        action="store_true",
        help="启用真实采集：起 headless bridge relay，把命令真下发到 hci-sim 并回贴结果",
    )
    parser.add_argument(
        "--bridge-url",
        dest="bridge_url",
        default=os.getenv("LIVE_E2E_BRIDGE_URL"),
        help="terminal_bridge WebSocket 地址，如 ws://127.0.0.1:19999/ws",
    )
    parser.add_argument(
        "--bridge-origin",
        dest="bridge_origin",
        default=os.getenv("LIVE_E2E_BRIDGE_ORIGIN"),
        help="WebSocket Origin（bridge 侧有校验时必填）",
    )
    parser.add_argument(
        "--connection",
        default=os.getenv("LIVE_E2E_CONNECTION"),
        help="connection.json 路径（由 make_connection.py 签发 lease 生成）",
    )
    args = parser.parse_args()

    if not args.internal_token:
        print("缺少 --internal-token / INTERNAL_API_TOKEN", file=sys.stderr)
        return 2

    spec = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    declared = set((spec.get("variables") or {}).get("declared") or [])

    connection: dict | None = None
    if args.connection:
        connection = json.loads(Path(args.connection).read_text(encoding="utf-8"))
    if args.sim and not (args.bridge_url and connection):
        print("--sim 需要同时提供 --bridge-url 与 --connection", file=sys.stderr)
        return 2
    if not args.sim:
        print("⚠️ 未启用 --sim：命令不会被真实执行，会话可能长时间不结束（仅适合验证变量落库）")

    config = DriverConfig(
        case_base=args.case_base,
        conv_base=args.conv_base,
        agent_base=args.agent_base,
        client_id=args.client_id,
        internal_token=args.internal_token,
        max_turns=args.max_turns,
        sim=args.sim,
        bridge_url=args.bridge_url,
        bridge_origin=args.bridge_origin,
        connection=connection,
    )

    print(f"场景: {spec.get('scenario_id')}  →  {args.case_base} / {args.conv_base} / {args.agent_base}")
    obs = await run_scenario(
        config,
        spec,
        dump_dir=Path(args.dump_dir) if args.dump_dir else None,
        on_progress=lambda msg: print(f"  · {msg}", flush=True),
    )

    print("\n=== 观测 ===")
    print(f"case_id={obs.case_id} conversation_id={obs.conversation_id}")
    print(f"category={obs.observed_category_code} sop_hit={obs.sop_document_hit_id} terminal={obs.terminal}")
    print(f"提交变量={obs.submitted_variables}")
    print(f"变量池键={sorted((obs.persisted_variables or {}).keys())}")
    print(f"pending={obs.pending_variable_name}")
    if obs.error:
        print(f"driver.error={obs.error}")

    # ── 判据 1：卡片变量名必须已声明 ──────────────────────────────────
    requested = _extract_variable_requests(obs.events)
    undeclared = [name for name in requested if name not in declared]
    print(f"\n卡片请求序列={requested}")

    # ── 判据 2：已提交变量不得重复索要 ────────────────────────────────
    submitted = set(obs.submitted_variables)
    repeated = [name for name in submitted if requested.count(name) > 1]

    # ── 判据 3：变量池键必须是已声明变量（防御写入野键回归）─────────
    persisted_keys = set((obs.persisted_variables or {}).keys())
    wild_keys = sorted(persisted_keys - declared)

    report = asserts.evaluate_invariants(
        observed_category_code=obs.observed_category_code,
        expected_category_code=(spec.get("expect") or {}).get("category_code"),
        sop_document_hit_id=obs.sop_document_hit_id,
        expected_sop_id=(spec.get("expect") or {}).get("sop_document_id"),
        submitted_variables=obs.submitted_variables,
        persisted_variables=obs.persisted_variables or {},
        final_user_questions=obs.final_user_questions,
        resolved_variables={
            k
            for k, v in (obs.persisted_variables or {}).items()
            if (v.get("value") if isinstance(v, dict) else v) not in (None, "", [], {})
        },
        declared_variables=declared,
        guard_events=obs.guard_events,
        expect_grounding_filter=False,
    )

    print("\n=== 框架不变量 ===")
    for item in report["passed"]:
        print(f"  ✅ {item}")
    for item in report["failed"]:
        print(f"  ❌ {item}")

    print("\n=== 本工单专用判据 ===")
    failures = list(report["failed"])

    if undeclared:
        print(f"  ❌ 卡片请求了未声明变量: {undeclared}")
        failures.append("undeclared_variable_requested")
    else:
        print("  ✅ 所有卡片变量名均已在 schema 声明")

    if repeated:
        print(f"  ❌ 已提交变量被重复索要: {repeated}")
        failures.append("submitted_variable_asked_again")
    else:
        print("  ✅ 已提交变量未被重复索要")

    if wild_keys:
        print(f"  ❌ 变量池存在 schema 外键（野键）: {wild_keys}")
        failures.append("wild_context_key")
    else:
        print("  ✅ 变量池无 schema 外键")

    print()
    if failures:
        print(f"❌ 未通过：{len(failures)} 项 → {failures}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
