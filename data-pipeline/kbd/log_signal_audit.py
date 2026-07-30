"""KBD 日志关键信号只读审计领域逻辑。

本模块属于 KBD 数据生产后的质量校验层：它复用 agent 运行时使用的 Signal Schema、
日志源 Catalog、parser 和 predicate 契约，判断 ``qfk_log`` Proposal 是否可构建。
审计不会写数据库，也不会修改 ``signals_json``，因此可以被 Pipeline、统一 CLI、CI
和单元测试共同复用。

重要边界：契约通过只说明信号在当前运行时可执行，不代表故障语义正确，也不替代专家复核。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from shared.schemas.acquirer_args import validate_acquire_args
from shared.schemas.log_source_catalog import (
    REQUEST_ARTIFACT_ROOT,
    normalize_log_path,
    resolve_log_source,
)


def _issue_code(error: str) -> str:
    if "缺少必填字段: file" in error or "file 必须" in error:
        return "MISSING_FILE"
    if "external_bmc_event_log" in error or "BMC" in error:
        return "CAPABILITY_GAP"
    if "time_window" in error or "日志时间" in error or "日志路径" in error or "path" in error:
        return "INVALID_TIME_OR_PATH"
    return "INVALID_LOG_CONTRACT"


def _signals_document(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        return {"schema_version": 2, "signals": raw}
    return raw if isinstance(raw, dict) else {}


def load_rows(stream: TextIO) -> list[dict[str, Any]]:
    """从文本流加载审计输入，并在领域边界统一验证输入形态。"""

    rows = json.load(stream)
    if not isinstance(rows, list):
        raise ValueError("输入必须是 JSON 数组")
    invalid_indexes = [index for index, row in enumerate(rows) if not isinstance(row, dict)]
    if invalid_indexes:
        preview = ",".join(str(index) for index in invalid_indexes[:5])
        raise ValueError(f"输入数组元素必须是对象，非法下标: {preview}")
    return rows


def load_rows_file(path: Path) -> list[dict[str, Any]]:
    """从 UTF-8 JSON 文件加载审计输入。"""

    with path.open("r", encoding="utf-8") as stream:
        return load_rows(stream)


async def load_rows_from_db(
    pool: Any,
    support_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """从 ``kbd_entry`` 只读加载审计输入。

    ``support_ids=None`` 表示全量；空列表表示零条。函数仅执行 SELECT，调用方不能借由
    审计命令写回 Proposal 或状态。
    """

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


def audit_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按当前共享契约审计，不对语义正确性作无证据猜测。"""

    issues: list[dict[str, str]] = []
    status_counts: Counter[str] = Counter()
    status_ids: dict[str, list[str]] = {}
    signal_counts: Counter[str] = Counter()

    for row in sorted(rows, key=lambda item: str(item.get("support_id") or "")):
        support_id = str(row.get("support_id") or "")
        document = _signals_document(row.get("signals_json"))
        signals = document.get("signals") if isinstance(document.get("signals"), list) else []
        rejected = (
            document.get("rejected_candidates")
            if isinstance(document.get("rejected_candidates"), list)
            else []
        )
        case_issue_start = len(issues)
        active_log_count = 0
        rejected_log_count = 0

        for index, signal in enumerate(signals):
            if not isinstance(signal, dict):
                continue
            acquire = signal.get("acquire") if isinstance(signal.get("acquire"), dict) else {}
            tool = str(acquire.get("tool") or "")
            signal_counts[tool or "<missing_tool>"] += 1
            signal_id = str(signal.get("id") or f"signals[{index}]")

            if tool != "qfk_log":
                continue

            active_log_count += 1
            args = acquire.get("args") if isinstance(acquire.get("args"), dict) else {}
            ok, error = validate_acquire_args(tool, args)
            if not ok:
                detail = error or "未知 qfk_log 参数错误"
                issues.append(
                    {
                        "support_id": support_id,
                        "signal_id": signal_id,
                        "code": _issue_code(detail),
                        "detail": detail,
                    }
                )
                continue

            matcher = signal.get("match") if isinstance(signal.get("match"), dict) else None
            produces = (
                (signal.get("orchestrate") or {}).get("produces")
                if isinstance(signal.get("orchestrate"), dict)
                else []
            )
            normalized_path = normalize_log_path(str(args.get("path"))) if args.get("path") else None
            if normalized_path and (
                normalized_path == REQUEST_ARTIFACT_ROOT
                or normalized_path.startswith(f"{REQUEST_ARTIFACT_ROOT}/")
            ):
                source = {
                    "source_id": "request_artifact_scope",
                    "parser": "plain_text",
                    "predicates": ["keyword", "regex", "state", "exists"],
                }
            else:
                source = resolve_log_source(
                    str(args.get("file") or ""),
                    source_family=str(args.get("source_family") or "auto"),
                    path=normalized_path,
                    parser=str(args.get("parser")) if args.get("parser") else None,
                )
            if matcher:
                matcher_type = str(matcher.get("type") or "")
                if matcher_type not in source.get("predicates", []):
                    issues.append(
                        {
                            "support_id": support_id,
                            "signal_id": signal_id,
                            "code": "UNSUPPORTED_PREDICATE",
                            "detail": (
                                f"{source.get('source_id')} / parser={source.get('parser')} "
                                f"不支持 matcher.type={matcher_type}"
                            ),
                        }
                    )
            elif produces and not (args.get("resource_keyword") or args.get("request_id")):
                issues.append(
                    {
                        "support_id": support_id,
                        "signal_id": signal_id,
                        "code": "UNBOUNDED_PRODUCER",
                        "detail": "日志变量产出缺少 resource_keyword/request_id，禁止无界回传整文件",
                    }
                )

        for index, rejected_item in enumerate(rejected):
            candidate = (
                rejected_item.get("candidate")
                if isinstance(rejected_item, dict) and isinstance(rejected_item.get("candidate"), dict)
                else {}
            )
            acquire = candidate.get("acquire") if isinstance(candidate.get("acquire"), dict) else {}
            if acquire.get("tool") != "qfk_log":
                continue
            rejected_log_count += 1
            reason = str(rejected_item.get("reason") or "候选日志信号被生产门禁拒绝")
            issues.append(
                {
                    "support_id": support_id,
                    "signal_id": str(candidate.get("id") or f"rejected_candidates[{index}]"),
                    "code": "REJECTED_LOG_CANDIDATE",
                    "detail": reason,
                }
            )

        case_issues = issues[case_issue_start:]
        active_blockers = [item for item in case_issues if item["code"] != "REJECTED_LOG_CANDIDATE"]
        if active_blockers:
            status = "BLOCKED_ACTIVE_SIGNAL"
        elif rejected_log_count:
            status = "NEEDS_EXPERT_REVIEW"
        elif active_log_count:
            status = "PASS_LOG_CONTRACT"
        else:
            status = "NO_ACTIVE_LOG_SIGNAL"
        status_counts[status] += 1
        status_ids.setdefault(status, []).append(support_id)

    issue_counts = Counter(item["code"] for item in issues)
    issue_case_ids: dict[str, list[str]] = {}
    for item in issues:
        case_ids = issue_case_ids.setdefault(item["code"], [])
        if item["support_id"] not in case_ids:
            case_ids.append(item["support_id"])
    return {
        "schema_version": 1,
        "case_count": len(rows),
        "signal_count": sum(signal_counts.values()),
        "signal_type_counts": dict(sorted(signal_counts.items())),
        "case_status_counts": dict(sorted(status_counts.items())),
        "case_status_ids": dict(sorted(status_ids.items())),
        "issue_counts": dict(sorted(issue_counts.items())),
        "issue_case_ids": dict(sorted(issue_case_ids.items())),
        "issues": issues,
        "interpretation": {
            "PASS_LOG_CONTRACT": "仅证明当前日志信号符合 Schema/Catalog/运行时构建契约，不证明故障语义正确或现场已复现",
            "NEEDS_EXPERT_REVIEW": "活动日志信号可构建，但仍存在被生产门禁拒绝的日志候选，须确认是漏信号还是非本机数据源",
            "BLOCKED_ACTIVE_SIGNAL": "活动 Proposal 含不可执行信号，发布前必须修复或明确 Capability Gap",
            "NO_ACTIVE_LOG_SIGNAL": "当前 Proposal 无活动 qfk_log；不等于原案例没有日志语义",
        },
    }


def dump_report(report: dict[str, Any], output: TextIO) -> None:
    """以稳定、可读的 JSON 形式输出审计报告。"""

    json.dump(report, output, ensure_ascii=False, indent=2)
    output.write("\n")
