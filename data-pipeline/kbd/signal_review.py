"""KBD 全量关键信号统一审查。

本模块只负责 Pipeline 场景的输入/报告适配。所有活动 Signal 的最低审查均委托给
``shared.resolution.review``，因此 QKV、QFK 以及未来 Resolver 都与 Agent 最终执行
共享同一运行时规则。Pipeline 特有规则只有：保留并报告 LLM rejected candidates。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from shared.resolution.review import (
    SignalReviewFeature,
    SignalReviewStatus,
    review_signal_document,
)


def _signals_document(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        return {"schema_version": 2, "signals": raw}
    return raw if isinstance(raw, dict) else {}


def load_rows(stream: TextIO) -> list[dict[str, Any]]:
    rows = json.load(stream)
    if not isinstance(rows, list):
        raise ValueError("输入必须是 JSON 数组")
    invalid_indexes = [index for index, row in enumerate(rows) if not isinstance(row, dict)]
    if invalid_indexes:
        preview = ",".join(str(index) for index in invalid_indexes[:5])
        raise ValueError(f"输入数组元素必须是对象，非法下标: {preview}")
    return rows


def load_rows_file(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        return load_rows(stream)


async def load_rows_from_db(
    pool: Any,
    support_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """从 kbd_entry 只读加载审查输入。"""

    if support_ids is not None and not support_ids:
        return []
    if support_ids is None:
        records = await pool.fetch(
            """SELECT support_id, signals_json
               FROM kbd_entry
               ORDER BY support_id"""
        )
    else:
        records = await pool.fetch(
            """SELECT support_id, signals_json
               FROM kbd_entry
               WHERE support_id = ANY($1)
               ORDER BY support_id""",
            list(support_ids),
        )
    return [
        {
            "support_id": str(record["support_id"]),
            "signals_json": record["signals_json"],
        }
        for record in records
    ]


def review_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """用 Shared Resolution Runtime 审查所有活动 Signal。"""

    issues: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    status_ids: dict[str, list[str]] = {}
    signal_counts: Counter[str] = Counter()
    runtime_status_counts: Counter[str] = Counter()

    for row in sorted(rows, key=lambda item: str(item.get("support_id") or "")):
        support_id = str(row.get("support_id") or "")
        document_invalid = False
        try:
            document = _signals_document(row.get("signals_json"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            document = {}
            document_invalid = True
            issues.append(
                {
                    "support_id": support_id,
                    "signal_id": None,
                    "code": "SIGNAL_DOCUMENT_INVALID",
                    "detail": f"signals_json 无法解析: {exc}",
                    "level": "error",
                    "source": "pipeline",
                }
            )
        signals = document.get("signals") if isinstance(document.get("signals"), list) else []
        rejected = (
            document.get("rejected_candidates")
            if isinstance(document.get("rejected_candidates"), list)
            else []
        )
        for signal in signals:
            if not isinstance(signal, dict):
                signal_counts["<invalid>"] += 1
                continue
            tool = str(((signal.get("acquire") or {}).get("tool")) or "<missing_tool>")
            signal_counts[tool] += 1

        result = review_signal_document(
            document,
            feature=SignalReviewFeature.PIPELINE,
        )
        runtime_status_counts.update(result.runtime_status_counts)
        for issue in result.issues:
            issues.append(
                {
                    "support_id": support_id,
                    "signal_id": issue.signal_id,
                    "code": issue.code,
                    "detail": issue.message,
                    "level": issue.level,
                    "source": issue.source,
                    "field": issue.field,
                }
            )

        for index, rejected_item in enumerate(rejected):
            candidate = (
                rejected_item.get("candidate")
                if isinstance(rejected_item, dict) and isinstance(rejected_item.get("candidate"), dict)
                else {}
            )
            issues.append(
                {
                    "support_id": support_id,
                    "signal_id": str(candidate.get("id") or f"rejected_candidates[{index}]"),
                    "code": "REJECTED_SIGNAL_CANDIDATE",
                    "detail": str(
                        rejected_item.get("reason")
                        if isinstance(rejected_item, dict)
                        else "候选信号被生产门禁拒绝"
                    ),
                    "level": "warning",
                    "source": "llm_generation",
                }
            )

        if document_invalid or result.status is SignalReviewStatus.BLOCKED:
            status = "BLOCKED_SIGNAL_REVIEW"
        elif result.status is SignalReviewStatus.NEEDS_REVIEW or rejected:
            status = "NEEDS_SIGNAL_REVIEW"
        elif result.status is SignalReviewStatus.EMPTY:
            status = "NO_ACTIVE_SIGNAL"
        else:
            status = "PASS_SIGNAL_REVIEW"
        status_counts[status] += 1
        status_ids.setdefault(status, []).append(support_id)

    issue_counts = Counter(item["code"] for item in issues)
    issue_case_ids: dict[str, list[str]] = {}
    for item in issues:
        case_ids = issue_case_ids.setdefault(str(item["code"]), [])
        if item["support_id"] not in case_ids:
            case_ids.append(item["support_id"])
    return {
        "schema_version": 2,
        "review_engine": "shared_resolution_runtime",
        "feature": SignalReviewFeature.PIPELINE.value,
        "case_count": len(rows),
        "signal_count": sum(signal_counts.values()),
        "signal_type_counts": dict(sorted(signal_counts.items())),
        "runtime_status_counts": dict(sorted(runtime_status_counts.items())),
        "case_status_counts": dict(sorted(status_counts.items())),
        "case_status_ids": dict(sorted(status_ids.items())),
        "issue_counts": dict(sorted(issue_counts.items())),
        "issue_case_ids": dict(sorted(issue_case_ids.items())),
        "issues": issues,
        "interpretation": {
            "PASS_SIGNAL_REVIEW": "全部活动信号通过 Shared Resolution Runtime 静态审查",
            "NEEDS_SIGNAL_REVIEW": "存在 needs_probe、运行时警告或被生产门禁拒绝的候选",
            "BLOCKED_SIGNAL_REVIEW": "活动信号被 Shared Resolution Runtime 阻断",
            "NO_ACTIVE_SIGNAL": "当前 Proposal 没有活动 Signal",
        },
    }


def dump_report(report: dict[str, Any], output: TextIO) -> None:
    json.dump(report, output, ensure_ascii=False, indent=2)
    output.write("\n")
