#!/usr/bin/env python3
"""只读评估多 Agent 关键信号抽取。

脚本只读取已发布 KBD、Prompt 和最佳实践，不调用写回、Proposal、Revision、
Batch 或失败复盘接口。评估结果只能写入本地 JSON 文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "kb-service"))

from app.models.kb_category import KbCategory  # noqa: E402
from app.routes.extract_signals import _acquirer_catalog_prompt_text, _validate_and_collect_signals  # noqa: E402
from app.services.signal_orchestrator import SignalExtractionOrchestrator  # noqa: E402
from app.services.sop_tool_contract_validator import get_acli_catalog_commands  # noqa: E402
from shared.database.postgres import DatabaseManager  # noqa: E402
from shared.models.system_prompt import SystemPrompt  # noqa: E402

PROMPT_NAMES = (
    "kbd_signal_count_v1",
    "kbd_signal_classify_v1",
    "kbd_signal_model_v1",
    "kbd_signal_verify_v1",
)
DEFAULT_PROMPT_MIGRATION = (
    PROJECT_ROOT / "database" / "atlas-migrations" / "20260904000001_seed_multi_agent_extract_prompts.sql"
)


def _json_signals(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("signals")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _signal_key(signal: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(signal, dict):
        return ("<invalid>", repr(signal), "")
    acquire = signal.get("acquire") or {}
    if not isinstance(acquire, dict):
        return ("<invalid-acquire>", repr(acquire), "")
    args = acquire.get("args") or {}
    if not isinstance(args, dict):
        return (str(acquire.get("tool") or "<invalid>"), repr(args), "")
    identity = args.get("keyword") or args.get("command") or args.get("file") or ""
    normalized_identity = re.sub(r"\s+", " ", str(identity)).strip().casefold()
    return (
        str(acquire.get("tool") or ""),
        normalized_identity,
        str((signal.get("match") or {}).get("type") or ""),
    )


def _diff_signals(expert: list[dict[str, Any]], predicted: list[dict[str, Any]]) -> dict[str, Any]:
    expert_keys = Counter(_signal_key(item) for item in expert)
    predicted_keys = Counter(_signal_key(item) for item in predicted)
    exact_matches = sum((expert_keys & predicted_keys).values())
    missing = list((expert_keys - predicted_keys).elements())
    extra = list((predicted_keys - expert_keys).elements())
    expert_tools = Counter(key[0] for key in expert_keys.elements())
    predicted_tools = Counter(key[0] for key in predicted_keys.elements())
    return {
        "expert_count": len(expert),
        "predicted_count": len(predicted),
        "count_matches": len(expert) == len(predicted),
        "tool_matches": sum((expert_tools & predicted_tools).values()),
        "missing_from_prediction": [list(key) for key in sorted(missing)],
        "extra_in_prediction": [list(key) for key in sorted(extra)],
        "exact_key_matches": exact_matches,
    }


def _evaluation_status(raw_count: int, predicted_count: int, rejected_count: int) -> str:
    """只有完整通过门禁的样本才计为成功，避免部分结果虚高成功率。"""
    return "ok" if raw_count > 0 and predicted_count == raw_count and rejected_count == 0 else "failed"


def _agreement_status(diff: dict[str, Any]) -> str:
    """将专家 Gold Label 对账与管线门禁状态分离。"""
    return "exact" if diff["expert_count"] == diff["predicted_count"] == diff["exact_key_matches"] else "mismatch"


def _build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """生成可审计的总体统计，避免仅看单条 JSON 才能判断质量。"""
    completed = [item for item in records if item.get("diff") is not None]
    expert_total = sum(int(item["diff"]["expert_count"]) for item in completed)
    predicted_total = sum(int(item["diff"]["predicted_count"]) for item in completed)
    exact_total = sum(int(item["diff"]["exact_key_matches"]) for item in completed)
    tool_total = sum(int(item["diff"]["tool_matches"]) for item in completed)
    precision = exact_total / predicted_total if predicted_total else 0.0
    recall = exact_total / expert_total if expert_total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    reason_counts: dict[str, int] = {}
    for item in records:
        for rejected in item.get("rejected") or []:
            reason = str(rejected.get("reason") or "未知原因")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    diagnostic_totals: dict[str, int] = {}
    for item in records:
        for key, value in (item.get("diagnostics") or {}).items():
            if isinstance(value, int):
                diagnostic_totals[key] = diagnostic_totals.get(key, 0) + value
    return {
        "pipeline_status_counts": {
            status: sum(1 for item in records if item.get("status") == status) for status in ("ok", "failed", "error")
        },
        "expert_agreement_counts": {
            status: sum(1 for item in records if item.get("agreement_status") == status)
            for status in ("exact", "mismatch")
        },
        "count_exact_kbds": sum(1 for item in completed if item["diff"]["count_matches"]),
        "signal_totals": {
            "expert": expert_total,
            "predicted": predicted_total,
            "tool_matches": tool_total,
            "exact_key_matches": exact_total,
            "missing": sum(len(item["diff"]["missing_from_prediction"]) for item in completed),
            "extra": sum(len(item["diff"]["extra_in_prediction"]) for item in completed),
            "rejected": sum(int(item.get("rejected_count") or 0) for item in records),
        },
        "tool_recall": round(tool_total / expert_total, 4) if expert_total else 0.0,
        "micro_precision": round(precision, 4),
        "micro_recall": round(recall, 4),
        "micro_f1": round(f1, 4),
        "failure_reasons": dict(sorted(reason_counts.items(), key=lambda pair: (-pair[1], pair[0]))),
        "stage_totals": diagnostic_totals,
    }


def _load_candidate_prompts(path: Path) -> dict[str, str]:
    """从候选迁移文件读取完整 Prompt，确保 PR 验证覆盖未部署的新版本。"""
    content = path.read_text(encoding="utf-8")
    prompts: dict[str, str] = {}
    for name in PROMPT_NAMES:
        marker = f"'{name}',"
        marker_pos = content.index(marker)
        template_start = content.index("$TEMPLATE$", marker_pos) + len("$TEMPLATE$")
        template_end = content.index("$TEMPLATE$", template_start)
        prompts[name] = content[template_start:template_end]
    return prompts


async def _load_read_only_assets(
    db: DatabaseManager,
) -> tuple[dict[str, str], dict[str, list[dict[str, Any]]]]:
    async with db.async_session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        prompts_result = await session.execute(
            select(SystemPrompt.name, SystemPrompt.content_template).where(
                SystemPrompt.name.in_(PROMPT_NAMES),
                SystemPrompt.is_active.is_(True),
            )
        )
        prompts = {str(row.name): str(row.content_template) for row in prompts_result}
        missing = set(PROMPT_NAMES) - set(prompts)
        if missing:
            raise RuntimeError(f"缺少多 Agent Prompt: {sorted(missing)}")
        best_result = await session.execute(
            text(
                """
                SELECT tool_name, pattern_category, signal_json, design_notes
                FROM signal_best_practice WHERE is_active = TRUE ORDER BY id
                """
            )
        )
        best: dict[str, list[dict[str, Any]]] = {}
        for item in best_result.mappings().all():
            best.setdefault(str(item["tool_name"]), []).append(
                {
                    "pattern_category": item["pattern_category"],
                    "signal_json": item["signal_json"],
                    "design_notes": item["design_notes"],
                }
            )
        return prompts, best


async def _load_kbds(db: DatabaseManager, input_path: Path | None, limit: int | None) -> list[dict[str, Any]]:
    if input_path is not None:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("kbds", [])
        return rows[:limit] if limit else rows
    async with db.async_session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        result = await session.execute(
            text(
                """
                SELECT id, support_id, title, problem_description, alert_info, steps_text,
                       category_id, ai_category_id, signals_json
                FROM kbd_entry WHERE status = 'published' ORDER BY id ASC
                """
            )
        )
        rows = [
            {
                "id": row["id"],
                "support_id": row["support_id"],
                "title": row["title"] or "",
                "problem_description": row["problem_description"] or "",
                "alert_info": row["alert_info"] or "",
                "steps_text": row["steps_text"] or "",
                "category_id": row["category_id"] or row["ai_category_id"] or "",
                "expert_signals": _json_signals(row["signals_json"]),
            }
            for row in result.mappings().all()
        ]
        return rows[:limit] if limit else rows


async def _category_baseline(db: DatabaseManager, code: str) -> str:
    if not code:
        return ""
    async with db.async_session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        result = await session.execute(select(KbCategory).where(KbCategory.code == code))
        item = result.scalar_one_or_none()
        if item is None:
            return code
        return json.dumps(
            {"code": item.code, "name": item.name, "domain": item.domain, "path": item.path_labels or []},
            ensure_ascii=False,
        )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.write_db:
        raise SystemExit("安全策略：评估脚本禁止写数据库；不要使用 --write-db")
    db = DatabaseManager(args.database_url)
    prompts, best = await _load_read_only_assets(db)
    prompt_source = "database"
    if args.prompt_migration:
        prompt_path = Path(args.prompt_migration).resolve()
        prompts = _load_candidate_prompts(prompt_path)
        prompt_source = str(prompt_path)
    kbds = await _load_kbds(db, Path(args.input) if args.input else None, args.limit)
    orchestrator = SignalExtractionOrchestrator(
        db,
        persist_failures=False,
        prompt_templates=prompts,
        best_practices=best,
    )
    acli_catalog = "\n".join(f"- {command}" for command in sorted(get_acli_catalog_commands()))
    records: list[dict[str, Any]] = []
    for kbd in kbds:
        kbd_id = int(kbd["id"])
        category = str(kbd.get("category_id") or "")
        entry = {
            "title": kbd.get("title", ""),
            "problem_description": kbd.get("problem_description", ""),
            "alert_info": kbd.get("alert_info", ""),
            "steps_text": kbd.get("steps_text", ""),
            "category_id": category,
            "category_baseline": await _category_baseline(db, category),
        }

        def gate_checker(candidates: list[dict[str, Any]], current_kbd_id: int = kbd_id):
            validated, rejected = _validate_and_collect_signals(
                candidates,
                f"dry-run:kbd:{current_kbd_id}",
                enforce_kbd_read_only=True,
            )
            issues = [str(item.get("reason") or "") for item in rejected if item.get("reason")]
            return validated, rejected, issues

        try:
            predicted, rejected, raw_count = await orchestrator.extract_kbd_signals_pipeline(
                None,
                kbd_id,
                entry,
                _acquirer_catalog_prompt_text(),
                acli_catalog,
                gate_checker,
            )
            expert = _json_signals(kbd.get("expert_signals") or kbd.get("final_signals"))
            # ``ok`` 只表示整条管线完成且没有任何候选被拒绝；否则必须显式归为
            # failed，避免把“部分信号通过门禁”的结果误报成成功并虚高准确率。
            status = _evaluation_status(raw_count, len(predicted), len(rejected))
            diff = _diff_signals(expert, predicted)
            diagnostics = dict(orchestrator.last_diagnostics.get(kbd_id) or {})
            records.append(
                {
                    "id": kbd_id,
                    "support_id": kbd.get("support_id"),
                    "status": status,
                    "agreement_status": _agreement_status(diff),
                    "raw_count": raw_count,
                    "rejected_count": len(rejected),
                    "diff": diff,
                    "predicted_signals": predicted,
                    "rejected": rejected,
                    "diagnostics": diagnostics,
                }
            )
        except Exception as exc:
            records.append(
                {
                    "id": kbd_id,
                    "support_id": kbd.get("support_id"),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc!r}",
                }
            )
    result = {
        "mode": "read_only_dry_run",
        "prompt_source": prompt_source,
        "total_kbds": len(kbds),
        "summary": _build_summary(records),
        "records": records,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    await db.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="只读评估多 Agent 关键信号抽取")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SIGNAL_EVAL_DATABASE_URL") or os.environ.get("DATABASE_URL"),
    )
    parser.add_argument("--input", help="离线 JSON 输入；提供后不读取数据库 KBD")
    parser.add_argument("--output", default="/tmp/signal-multi-agent-evaluation.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--prompt-migration", help=f"候选 Prompt 迁移文件，PR 验证建议使用：{DEFAULT_PROMPT_MIGRATION}")
    parser.add_argument("--write-db", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("必须提供 --database-url 或 SIGNAL_EVAL_DATABASE_URL")
    result = asyncio.run(run(args))
    ok = sum(1 for item in result["records"] if item["status"] == "ok")
    print(
        json.dumps(
            {"mode": result["mode"], "total_kbds": result["total_kbds"], "ok": ok, "output": args.output},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
