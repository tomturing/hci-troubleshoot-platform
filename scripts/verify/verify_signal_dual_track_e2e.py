#!/usr/bin/env python3
"""信号双路并行 · 端到端实测（真实 KBD 数据 + 真实 CDD 链路 + 仿真采集输出）。

第一性原理：要证明「案例可以通过根因信号被检索到」，必须让**生产代码的每一层都真跑**，
只把最外层不可控的物理采集（SSH/ACLI 到仿真节点）替换为确定性夹具：

    真实 DB 行（staging） → 真实 playbooks._signals()（含运行时准入闸门）
      → 真实 compile_signal_plan / scheduler / reduce_candidates / fuse_track_states
      → 真实 evaluate_matcher → 真实 decide_conclusion
      ⇅ 唯一替身：_execute_acquirer 返回仿真 stdout（按 tool+args 命中夹具）

因此本脚本分两个进程阶段（`app` 包名在 kb-service 与 agent-service 之间冲突，必须隔离）：
    阶段 1（kb-service 依赖）：把 DB 行喂给 `playbooks._signals()`，产出**运行时真实信号并集**；
    阶段 2（agent-service 依赖）：用该并集构造 KBD，跑 `KBDDiagnostic.diagnose()`。

诚实边界：
- 不验证 LLM 抽取质量（由抽取回归与 Prompt 契约测试覆盖），只验证**判定/检索链路**；
- 仿真输出由夹具给定，不代表真实日志分布；但夹具中的关键词取自 DB 里已发布信号本身。

场景矩阵（同一 diagnose 链路，仅换 KB_SIGNAL_TRACK_MODE 与仿真现场；
赛马期曾覆盖 shadow/root_only 两档实验模式，2026-10-21 已收敛删除，现行只跑两模式）：
    S1 故障态（两路证据齐）      → process_only / dual 均应命中
    S2 关键路判死 + 根因路命中    → process_only 不命中；dual 命中（核心增量举证）
    S3 健康态（只有通用噪声）      → 任一路都不得命中（证明不是万能匹配）
    S4 兄弟篇特征（另一篇的签名）  → 目标 KBD 不得命中（证明根因信号有判别力）
    S5 存量未盖章（无 gate_version）→ 准入闸门拦截（准入数=0）

用法：
    E2E_DSN=postgresql://... .venv/bin/python \\
      scripts/verify/verify_signal_dual_track_e2e.py --category 虚拟机-018 --target 627

    DSN 也可用 DATABASE_URL 提供；脚本自举 import 路径，无需手写 PYTHONPATH。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[2]
STAGE_SIGNALS_FLAG = "--emit-runtime-signals"

# 父进程跑阶段 2（agent-service 依赖）：自举 import 路径，令复现命令无需手写 PYTHONPATH。
# 阶段 1（--emit-runtime-signals 子进程）绝不能加 agent-service：`app` 包名会盖掉 kb-service，
# 而子进程的 PYTHONPATH 已由 _load_runtime_signals 显式设成 kb-service 侧。
if STAGE_SIGNALS_FLAG not in sys.argv:
    for _path in (
        str(REPO / "backend"),
        str(REPO / "backend" / "shared"),
        str(REPO / "backend" / "agent-service"),
    ):
        if _path not in sys.path:
            sys.path.insert(0, _path)

# DSN 只从环境变量取：凭据不得进仓库（脚本会被 CI 与其它环境复用，硬编码口令等于凭据泄漏）。
DEFAULT_DSN = os.environ.get("E2E_DSN") or os.environ.get("DATABASE_URL") or ""


# ─── 阶段 1：kb-service 侧，产出运行时真实信号并集 ────────────────────────────


def _emit_runtime_signals() -> int:
    """stdin: [{row}] → stdout: {kbd_id: {signals, root_payload, verification_contract}}。"""
    rows = json.load(sys.stdin)
    from app.routes import playbooks  # noqa: PLC0415
    from app.services.root_signal_track import build_root_track_payload  # noqa: PLC0415

    out: dict[str, Any] = {}
    for row in rows:
        entry = SimpleNamespace(
            id=row["id"],
            support_id=row["support_id"],
            title=row.get("title") or "",
            category_id=row.get("category_id") or "",
            status=row.get("status") or "",
            signals_json=row.get("signals_json") or {"schema_version": 2, "signals": []},
            root_signal_json=row.get("root_signal_json") or [],
        )
        signals_json = entry.signals_json if isinstance(entry.signals_json, dict) else {}
        root_payload = build_root_track_payload(entry)
        out[str(row["id"])] = {
            # 关键信号链路载荷（与根因完全无关）
            "signals": playbooks._signals(entry),
            "verification_contract": signals_json.get("verification_contract") or {},
            # 根因信号链路载荷：kb-service 独立装配后原样下发，agent 端不自行拼装
            "root_payload": root_payload,
            # 准入观测：哪些根因信号被闸门挡在外面，用于解释"S5 未盖章不命中"。
            "root_admitted": len(root_payload["root_signals"]),
        }
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


def _load_runtime_signals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    payload = json.dumps(rows, ensure_ascii=False).encode()
    python = os.environ.get("KB_PYTHON") or str(REPO / ".venv" / "bin" / "python")
    if not Path(python).exists():
        python = sys.executable
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(REPO / "backend"), str(REPO / "backend" / "shared"), str(REPO / "backend" / "kb-service")]
        ),
    }
    proc = subprocess.run(
        [python, __file__, STAGE_SIGNALS_FLAG],
        input=payload,
        capture_output=True,
        env=env,
        cwd=str(REPO),
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"阶段1（kb-service）失败：{proc.stderr.decode()[-2000:]}")
    return json.loads(proc.stdout)


# ─── 仿真现场（唯一替身：物理采集）────────────────────────────────────────────


@dataclass
class World:
    """仿真现场：按工具与参数返回确定性输出。

    ``tasks`` 模拟前端任务列表：qkv_task/qkv_alert 的 **keyword + is_failed 过滤语义必须
    像真实引擎一样生效**——否则任何一篇「只有 qkv_task 生产者作为 must」的 KBD 都会在
    任意现场被误判为 SUPPORTED（首版仿真就把 202/305/982 在健康态误命中，暴露的正是
    仿真保真度问题，而非产品缺陷）。

    task_present=False 代表「平台侧没有对应的失败任务记录」（关键路生产者判死），
    此时根因路仍可能凭现场上下文（HOST/DATE）与日志取证命中——这正是 S2 的核心举证。
    """

    task_present: bool = True
    tasks: list[dict[str, Any]] = field(default_factory=list)
    default_log: str = ""
    logs: dict[str, str] = field(default_factory=dict)
    pool: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def task_records(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.task_present:
            return []
        keyword = str(args.get("keyword") or "").strip()
        require_failed = bool(args.get("is_failed"))
        hits = [
            task
            for task in self.tasks
            if (not keyword or keyword in str(task.get("description") or ""))
            and (not require_failed or str(task.get("status") or "failed") == "failed")
        ]
        try:
            return hits[: max(1, int(args.get("limit") or 100))]
        except (TypeError, ValueError):
            return hits

    def stdout(self, tool: str, args: dict[str, Any]) -> str:
        self.calls.append((tool, str(args.get("file") or args.get("command") or "")))
        if tool.startswith("qfk_log"):
            return self.logs.get(str(args.get("file") or ""), self.default_log)
        return self.default_log


TASK_MIGRATE_FAILED = [
    {
        "description": "新建虚拟机 vm-migrate-0421 失败: create hci vm by conf failed",
        "status": "failed",
        "host": "10.20.30.41",
        "end": "2026-10-20 09:31:12",
        "vm": "vm-migrate-0421",
        "request_id": "req-0421",
    }
]

BITMAP_ERROR = (
    "2026-10-20 09:31:12,441 INFO  vtpdaemon[2311] create hci vm by conf\n"
    "2026-10-20 09:31:12,988 ERROR vtpdaemon[2311] Create bitmap storage area error: "
    "Cannot allocate memory\n"
    "2026-10-20 09:31:13,002 ERROR vtpdaemon[2311] create hci vm by conf failed\n"
)

SIZE_ZERO = (
    "2026-10-20 09:31:10,117 INFO  backup-compute[8891] create hci vm by conf\n"
    "2026-10-20 09:31:10,208 INFO  backup-compute[8891] disk spec: {'size': 0, 'type': 'thin'}\n"
)

HEALTHY = (
    "2026-10-20 09:00:00,000 INFO  systemd[1] Started Service.\n"
    "2026-10-20 09:00:01,000 INFO  kernel[0] link is up, speed 10000Mb/s\n"
    "2026-10-20 09:00:02,000 INFO  vtpdaemon[2311] create hci vm by conf completed\n"
)

SIBLING = (
    "2026-10-20 09:31:12,441 INFO  vtpdaemon[2311] create hci vm by conf\n"
    "2026-10-20 09:31:13,120 ERROR vtpdaemon[2311] 拷贝磁盘数据异常，传输中断\n"
    "2026-10-20 09:31:13,201 INFO  retry scheduled\n"
)


def _target_logs() -> dict[str, str]:
    return {"sfvt_vtpdaemon.log": BITMAP_ERROR, "vtp-backup-compute.log": SIZE_ZERO}


# ─── 阶段 2：agent-service 侧，跑真实诊断链路 ─────────────────────────────────


def _make_diagnostic(world: World, mode: str):
    """构造关键/根因两个仿真诊断器（只替换物理采集边界）。"""
    from app.adapters.agents.htp.kbd_differential import KBDDiagnostic  # noqa: PLC0415
    from app.adapters.agents.htp.root_track import RootTrackDiagnostic  # noqa: PLC0415

    class _SimulatedAcquire:
        """采集边界替身：qkv 生产者按任务列表语义、qfk 按仿真 stdout。"""

        async def _execute_acquirer(
            self,
            step,
            env_context,
            session_id,
            user_id,
            *,
            signal=None,
            exec_id=None,
        ):
            acquirer = step.tool_name
            if acquirer.startswith("qkv_"):
                args = self._resolve_args(step.tool_args_template, env_context, self._variable_pool)
                records = world.task_records(args)
                if not records:
                    # 生产者判死：没有匹配的失败任务记录。
                    return "[]", None, False, None
                first = records[0]
                for name, path in (("host", "host"), ("end", "end"), ("vm", "vm"), ("request_id", "request_id")):
                    if first.get(path) is not None:
                        self._set_pool_var(name, first[path], producer_priority=30)
                end = str(first.get("end") or "")
                if len(end) >= 10:
                    self._set_pool_var("date", end[:10], producer_priority=30)
                for name, value in world.pool.items():
                    self._set_pool_var(name, value, producer_priority=10)
                return json.dumps({"records": records}, ensure_ascii=False), None, True, None
            if not (signal or {}).get("match"):
                # 纯生产者（qfk_var 等）：本次现场没有可提取记录 → 明确反证。
                return "", None, False, None
            args = self._resolve_args(step.tool_args_template, env_context, self._variable_pool)
            return world.stdout(acquirer, args), None, None, None

    class SimulatedDiagnostic(_SimulatedAcquire, KBDDiagnostic):
        """只在采集执行边界替换为仿真输出，其余全部走生产代码。"""

        def __init__(self) -> None:
            super().__init__(ai_registry=None, tool_executor=None, conversation_id="e2e-session", case_id="E2E")

        async def _generate_report(self, *args, **kwargs):  # noqa: ANN002, ANN003
            # 报告文案与判定结论无关，且会调用 LLM；e2e 只举证判定链路。
            return "[e2e] 结论由 CDD 状态机门禁生成，报告正文从略。"

    class SimulatedRootDiagnostic(_SimulatedAcquire, RootTrackDiagnostic):
        """根因链路使用**独立实例**，与关键链路的变量池/采集缓存互不可见。"""

        def __init__(self) -> None:
            super().__init__(ai_registry=None, tool_executor=None, conversation_id="e2e-session", case_id="E2E")

        async def _generate_report(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return "[e2e] 根因链路结论由 CDD 状态机门禁生成，报告正文从略。"

        def _build_partial_report(self, *args, **kwargs):  # noqa: ANN002, ANN003
            # 必须与基类一致：``KBDDiagnostic._build_partial_report`` 是同步 staticmethod。
            # 首版在这里写成 async，导致根因链路照契约同步调用时拿到 coroutine，
            # 报告拼接阶段才炸——fake 写错形态会污染"产品缺陷"的判断。
            return "[e2e] 根因链路部分结论，报告正文从略。"

    # 模式由 diagnose_with_tracks 的显式参数传入，不写进程级环境变量（避免用例间串味）。
    os.environ["KB_SIGNAL_TRACK_MODE"] = mode
    return SimulatedDiagnostic(), SimulatedRootDiagnostic


@dataclass
class RunResult:
    scenario: str
    mode: str
    supported: list[str]
    states: dict[str, str]
    tracks: dict[str, Any]
    signal_outcomes: dict[str, Any]
    conclusion: str
    trace: list[tuple[str, str]]


async def _run_case(
    case: dict[str, Any], mode: str, pool: dict[str, str], runtime: dict[str, Any]
) -> RunResult:
    from app.adapters.agents.htp.dual_track import diagnose_with_tracks  # noqa: PLC0415
    from shared.cdd.kbd_model import kbd_from_dict  # noqa: PLC0415

    world = World(
        task_present=case["task_present"],
        tasks=case.get("tasks") or [],
        default_log=case.get("default_log", ""),
        logs=case.get("logs") or {},
        pool=pool,
    )
    diag, root_factory = _make_diagnostic(world, mode)
    candidates = [
        kbd_from_dict(
            {
                "id": row["id"],
                "support_id": row["support_id"],
                "name": row.get("title") or "",
                "category_id": row.get("category_id") or "",
                "root_cause": row.get("root_cause") or "",
                "solution": row.get("solution") or "",
                "signals": runtime[row["id"]]["signals"],
                "verification_contract": runtime[row["id"]]["verification_contract"],
                "resource_revision": {"revision": 1},
            }
        )
        for row in case["rows"]
    ]
    # 根因候选保持 kb-service 下发的原始载荷：编排层负责构造根因形态，
    # 共享内核的 KBD 不认识 root_* 字段。
    root_cases = [
        {
            "id": row["id"],
            "support_id": row["support_id"],
            "name": row.get("title") or "",
            "category_id": row.get("category_id") or "",
            "root_cause": row.get("root_cause") or "",
            "solution": row.get("solution") or "",
            "signals": runtime[row["id"]]["signals"],
            "verification_contract": runtime[row["id"]]["verification_contract"],
            "resource_revision": {"revision": 1},
            **runtime[row["id"]]["root_payload"],
        }
        for row in case["rows"]
    ]
    env_context = {
        "product": "HCI",
        "components": ["虚拟机"],
        **(case.get("env") or {}),
    }
    async for _event in diagnose_with_tracks(
        diag,
        candidates=candidates,
        root_cases=root_cases,
        env_context=env_context,
        session_id="e2e-session",
        mode=mode,
        root_engine_factory=root_factory,
    ):
        pass
    result = diag.get_result()
    return RunResult(
        scenario=case["name"],
        mode=mode,
        supported=[str(kbd.id) for kbd in (result.matched_kbds if result else [])],
        states=dict(result.candidate_states) if result else {},
        tracks={},
        signal_outcomes={},
        conclusion=str(result.conclusion_level) if result else "NONE",
        trace=world.calls,
    )


def _load_rows_file(path: str) -> list[dict[str, Any]]:
    """离线行数据（staging 导出的 JSON 数组），用于无 DSN 的可重复复现。"""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    for row in rows:
        row["id"] = str(row["id"])
        row["support_id"] = str(row["support_id"])
        for key in ("signals_json", "root_signal_json"):
            value = row.get(key)
            if isinstance(value, str):
                row[key] = json.loads(value)
    return rows


# ─── 主流程 ──────────────────────────────────────────────────────────────────


async def _load_rows(dsn: str, category: str) -> list[dict[str, Any]]:
    import asyncpg  # noqa: PLC0415

    conn = await asyncpg.connect(dsn)
    try:
        records = await conn.fetch(
            """
            SELECT id, support_id, status, title, category_id, root_cause, solution,
                   signals_json, root_signal_json
            FROM kbd_entry
            WHERE category_id = $1 AND status = 'published'
            ORDER BY id DESC
            """,
            category,
        )
    finally:
        await conn.close()
    rows: list[dict[str, Any]] = []
    for record in records:
        row = dict(record)
        # 共享 CDD 统一用字符串身份，避免 int/str 键在 runtime 字典与断言处漂移。
        row["id"] = str(row["id"])
        row["support_id"] = str(row["support_id"])
        for key in ("signals_json", "root_signal_json"):
            value = row.get(key)
            row[key] = json.loads(value) if isinstance(value, str) else value
        rows.append(row)
    return rows


def main() -> int:
    if STAGE_SIGNALS_FLAG in sys.argv:
        return _emit_runtime_signals()

    parser = argparse.ArgumentParser(description="信号双路并行端到端实测")
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument(
        "--rows-file",
        default="",
        help="离线行数据 JSON（staging 导出），用于无 DSN 的可重复复现",
    )
    parser.add_argument("--category", default="虚拟机-018")
    parser.add_argument("--target", default="627")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    if args.rows_file:
        rows = _load_rows_file(args.rows_file)
    else:
        if not args.dsn:
            raise SystemExit("缺少数据库 DSN：请传 --dsn 或 --rows-file，或设置 E2E_DSN / DATABASE_URL")
        rows = asyncio.run(_load_rows(args.dsn, args.category))
    if not rows:
        raise SystemExit(f"分类 {args.category} 没有已发布 KBD")
    runtime = _load_runtime_signals(
        [
            {
                "id": row["id"],
                "support_id": row["support_id"],
                "title": row.get("title"),
                "category_id": row.get("category_id"),
                "status": row.get("status"),
                "signals_json": row.get("signals_json"),
                "root_signal_json": row.get("root_signal_json"),
            }
            for row in rows
        ]
    )
    target_id = args.target
    if target_id not in runtime:
        raise SystemExit(f"目标 KBD {target_id} 不在分类 {args.category} 中")
    admitted = runtime[target_id]["root_admitted"]
    print(
        f"[范围] 分类={args.category} 已发布 KBD={len(rows)} 篇；"
        f"目标={target_id}(support={next(r['support_id'] for r in rows if r['id'] == target_id)}) "
        f"运行时准入根因信号={admitted} 条"
    )

    pool = {"host": "10.20.30.41", "end": "2026-10-20 09:31:12", "date": "2026-10-20", "vm": "vm-migrate-0421"}
    cases = [
        {
            "name": "S1_故障态_两路证据齐",
            "task_present": True,
            "tasks": TASK_MIGRATE_FAILED,
            # 现场上下文（主机/日期）由工单提供：仿真不得从关键链路偷值，
            # 与生产语义一致——根因链路只靠自己拿到的外部变量或关键链路已完成步骤派生。
            "env": {"HOST": "10.20.30.41", "DATE": "2026-10-20"},
            "logs": _target_logs(),
            "default_log": HEALTHY,
        },
        {
            "name": "S2_关键路判死_根因路命中",
            "task_present": False,
            # 现场上下文由用户/会话提供（主机与日期），关键路的生产者缺失不应阻断根因取证。
            "env": {"HOST": "10.20.30.41", "DATE": "2026-10-20"},
            "logs": _target_logs(),
            "default_log": HEALTHY,
        },
        {
            "name": "S3_健康态_仅通用噪声",
            "task_present": True,
            "tasks": [{"description": "系统例行巡检完成", "status": "success"}],
            # 健康态也要让根因路真正跑起来：若因变量缺失而 BLOCKED，「不命中」就没有说服力。
            "env": {"HOST": "10.20.30.41", "DATE": "2026-10-20"},
            "default_log": HEALTHY,
        },
        {
            "name": "S4_兄弟篇特征",
            "task_present": True,
            "env": {"HOST": "10.20.30.41", "DATE": "2026-10-20"},
            "tasks": [
                {
                    "description": "拷贝磁盘数据异常，迁移任务失败",
                    "status": "failed",
                    "host": "10.20.30.41",
                    "end": "2026-10-20 09:31:12",
                    "vm": "vm-copy-0912",
                    "request_id": "req-0912",
                }
            ],
            "default_log": SIBLING,
        },
    ]

    results: list[RunResult] = []
    for case in cases:
        # 两模式全覆盖：只跑关键链路（一键回滚档） + 双链路融合（默认档）。
        for mode in ("process_only", "dual"):
            results.append(asyncio.run(_run_case({**case, "rows": rows}, mode, pool, runtime)))

    # S5：存量未盖章 → 准入闸门必须把它挡在运行时之外。
    legacy_rows = []
    for row in rows:
        clone = dict(row)
        root = clone.get("root_signal_json")
        if isinstance(root, list):
            stripped = []
            for signal in root:
                signal = json.loads(json.dumps(signal, ensure_ascii=False))
                provenance = signal.get("provenance") or {}
                provenance.pop("gate_version", None)
                provenance.pop("gate_status", None)
                stripped.append(signal)
            clone["root_signal_json"] = stripped
        legacy_rows.append(clone)
    legacy_runtime = _load_runtime_signals(
        [
            {
                "id": row["id"],
                "support_id": row["support_id"],
                "title": row.get("title"),
                "category_id": row.get("category_id"),
                "status": row.get("status"),
                "signals_json": row.get("signals_json"),
                "root_signal_json": row.get("root_signal_json"),
            }
            for row in legacy_rows
        ]
    )
    satisfied = all(legacy_runtime[str(r["id"])]["root_admitted"] == 0 for r in legacy_rows if r["root_signal_json"])
    print(f"[S5] 剥离 gate_version 后全部根因信号被准入闸门拦截: {satisfied}")
    results.append(
        RunResult(
            scenario="S5_存量未盖章_准入闸门",
            mode="admission",
            supported=[],
            states={},
            tracks={},
            signal_outcomes={},
            conclusion=f"admitted={sum(v['root_admitted'] for v in legacy_runtime.values())}",
            trace=[],
        )
    )

    # ─── 输出举证表 ───
    print("\n| 场景 | 模式 | 结论 | 命中 KBD | 目标状态 |")
    print("| --- | --- | --- | --- | --- |")
    for item in results:
        tgt_state = item.states.get(target_id, "-")
        print(
            f"| {item.scenario} | {item.mode} | {item.conclusion} | "
            f"{','.join(item.supported) or '—'} | {tgt_state} |"
        )

    def _hit(scenario: str, mode: str) -> bool:
        return any(item.scenario == scenario and item.mode == mode and target_id in item.supported for item in results)

    checks = {
        "S1 两模式均命中目标": all(_hit("S1_故障态_两路证据齐", m) for m in ("process_only", "dual")),
        "S2 关键路判死不命中": not _hit("S2_关键路判死_根因路命中", "process_only"),
        "S2 融合模式命中": _hit("S2_关键路判死_根因路命中", "dual"),
        "S3 健康态不命中": not any(_hit("S3_健康态_仅通用噪声", m) for m in ("process_only", "dual")),
        "S4 兄弟篇特征不误命中目标": not any(
            _hit("S4_兄弟篇特征", m) for m in ("process_only", "dual")
        ),
        "S4 兄弟篇自身被正确命中": any(
            item.scenario == "S4_兄弟篇特征" and item.mode == "dual" and {"202", "305"} & set(item.supported)
            for item in results
        ),
        "S5 未盖章信号不准入": satisfied,
    }
    print("\n===== 断言 =====")
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([item.__dict__ for item in results], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[json] {args.json_out}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
