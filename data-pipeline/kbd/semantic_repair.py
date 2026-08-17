"""KBD 结构语义异常扫描与受控三方修复工具。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import asyncpg
import httpx
from bs4 import BeautifulSoup, Tag
from dotenv import dotenv_values
from prometheus_client import Counter

from .config import settings
from .converter import (
    _MD_TITLE_TO_FIELD,
    _build_image_seq_map,
    _normalize_code_payload,
    _parse_sections,
    convert_kbd_structured,
)
from .observability import new_trace_id, set_trace_id, traceparent

logger = logging.getLogger("kbd.semantic_repair")

_SCAN_RESULTS = Counter(
    "kbd_semantic_repair_scan_total",
    "KBD 结构语义扫描结果",
    ("status",),
)
_APPLY_RESULTS = Counter(
    "kbd_semantic_repair_apply_total",
    "KBD 结构语义修复结果",
    ("status",),
)

SECTION_FIELDS = tuple(_MD_TITLE_TO_FIELD.values())
_ORDERED_ITEM_RE = re.compile(r"(?m)^\s*\d+\.\s+")


@dataclass(frozen=True)
class RuntimeConfig:
    database_url: str
    cache_dir: Path
    kb_service_url: str
    internal_api_token: str


@dataclass
class RepairPlan:
    report: dict[str, Any]
    updates: dict[str, Any]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_array(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, str):
        parsed = json.loads(value)
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
    return []


def _runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    values: dict[str, Any] = {}
    if args.env_file:
        values.update({key: value for key, value in dotenv_values(args.env_file).items() if value is not None})
    values.update(os.environ)
    database_url = str(values.get("DATABASE_URL") or settings.DATABASE_URL)
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return RuntimeConfig(
        database_url=database_url,
        cache_dir=Path(args.cache_dir or values.get("KBD_CACHE_DIR") or settings.KBD_CACHE_DIR),
        kb_service_url=str(values.get("KB_SERVICE_URL") or settings.KB_SERVICE_URL).rstrip("/"),
        internal_api_token=str(values.get("INTERNAL_API_TOKEN") or settings.INTERNAL_API_TOKEN),
    )


def _current_payload(row: asyncpg.Record) -> dict[str, Any]:
    working_payload = _json_object(row.get("working_payload"))
    if row["status"] == "published" and working_payload:
        return working_payload
    payload = {field: row.get(field) or "" for field in SECTION_FIELDS}
    payload.update(
        {
            "title": row.get("title") or "",
            "images_json": _json_array(row.get("images_json")),
        }
    )
    return payload


async def _load_entries(
    database_url: str,
    support_ids: list[str],
) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(database_url)
    try:
        return await connection.fetch(
            """
            SELECT e.id, e.support_id, e.status, e.lock_version, e.title,
                   e.problem_description, e.alert_info, e.steps_text, e.root_cause,
                   e.solution, e.operational_impact, e.is_temporary, e.recommendations,
                   e.images_json, e.working_revision_id,
                   working.payload_json AS working_payload,
                   baseline.payload_json AS baseline_payload
            FROM kbd_entry AS e
            LEFT JOIN kbd_revision AS working ON working.id = e.working_revision_id
            LEFT JOIN LATERAL (
                SELECT payload_json
                FROM kbd_revision
                WHERE kbd_entry_id = e.id AND revision_type = 'proposal'
                ORDER BY revision_no ASC
                LIMIT 1
            ) AS baseline ON TRUE
            WHERE e.support_id = ANY($1::text[])
            ORDER BY e.id
            """,
            support_ids,
        )
    finally:
        await connection.close()


def _direct_child_under(node: Tag, ancestor: Tag) -> Tag | None:
    current = node
    while isinstance(current.parent, Tag) and current.parent is not ancestor:
        current = current.parent
    return current if current.parent is ancestor else None


def _image_anchor_risks(section_html: str, image_map: dict[str, dict]) -> list[int]:
    soup = BeautifulSoup(section_html, "lxml")
    risks: list[int] = []
    for image in soup.find_all("img"):
        item = image.find_parent("li")
        if item is None:
            continue
        image_child = _direct_child_under(image, item)
        if image_child is None:
            continue
        previous = list(image_child.find_previous_siblings())
        if not any(
            sibling.name in {"ul", "ol", "pre", "table"} or sibling.find(["ul", "ol", "pre", "table"]) is not None
            for sibling in previous
            if isinstance(sibling, Tag)
        ):
            continue
        src = image.get("src") or image.get("data-src") or ""
        absolute = urljoin(settings.SANGFOR_API_BASE, src) if src else ""
        entry = image_map.get(absolute)
        if entry is not None:
            risks.append(int(entry["seq"]))
    return risks


def _section_issues(
    section_html: str,
    current_text: str,
    generated_text: str,
    image_map: dict[str, dict],
) -> list[dict[str, Any]]:
    if current_text == generated_text:
        return []

    soup = BeautifulSoup(section_html, "lxml")
    issues: list[dict[str, Any]] = []
    normalized_current = re.sub(r"\s+", " ", current_text).strip()
    fenced_payloads: list[str] = []
    active_fence: tuple[str, int] | None = None
    active_lines: list[str] = []
    for line in current_text.splitlines():
        stripped = line.lstrip()
        fence_match = re.match(r"^(`{3,}|~{3,})([^`]*)$", stripped)
        if active_fence is None and fence_match:
            active_fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            active_lines = []
            continue
        if active_fence is not None:
            closing = re.match(rf"^{re.escape(active_fence[0])}{{{active_fence[1]},}}\s*$", stripped)
            if closing:
                fenced_payloads.append(re.sub(r"\s+", " ", "\n".join(active_lines)).strip())
                active_fence = None
                active_lines = []
            else:
                active_lines.append(line)
    missing_code_hashes = []
    boundary_lost_hashes = []
    for pre in soup.find_all("pre"):
        payload = _normalize_code_payload(pre)
        normalized_payload = re.sub(r"\s+", " ", payload).strip()
        if normalized_payload not in normalized_current:
            missing_code_hashes.append(_sha256_text(payload))
        elif not any(normalized_payload in fenced for fenced in fenced_payloads):
            boundary_lost_hashes.append(_sha256_text(payload))
    if missing_code_hashes:
        issues.append({"kind": "missing_code", "count": len(missing_code_hashes), "hashes": missing_code_hashes})
    if boundary_lost_hashes:
        issues.append(
            {
                "kind": "code_block_boundary_lost",
                "count": len(boundary_lost_hashes),
                "hashes": boundary_lost_hashes,
            }
        )

    source_ordered_items = sum(len(tag.find_all("li", recursive=False)) for tag in soup.find_all("ol"))
    current_ordered_items = len(_ORDERED_ITEM_RE.findall(current_text))
    if source_ordered_items > current_ordered_items:
        issues.append(
            {
                "kind": "ordered_list_lost",
                "source_count": source_ordered_items,
                "current_count": current_ordered_items,
            }
        )

    risky_images = _image_anchor_risks(section_html, image_map)
    misplaced = []
    for seq in risky_images:
        marker = f"![img:{seq}]"
        if marker not in current_text:
            misplaced.append(seq)
            continue
        generated_before = generated_text.split(marker, 1)[0]
        current_before = current_text.split(marker, 1)[0]
        expected_lines = [line.strip() for line in generated_before.splitlines() if line.strip()]
        expected_anchor = expected_lines[-1] if expected_lines else ""
        if expected_anchor and re.sub(r"\s+", " ", expected_anchor) not in re.sub(r"\s+", " ", current_before):
            misplaced.append(seq)
    if misplaced:
        issues.append({"kind": "image_anchor_shifted", "seqs": misplaced})
    return issues


def _merge_text(current: str, baseline: str, generated: str) -> tuple[str | None, str]:
    if current == generated:
        return current, "unchanged"
    if current == baseline:
        return generated, "source_replaced"
    if generated == baseline:
        return current, "expert_preserved"
    with tempfile.TemporaryDirectory(prefix="kbd-semantic-merge-") as temporary:
        root = Path(temporary)
        paths = {
            "current": root / "current.md",
            "baseline": root / "baseline.md",
            "generated": root / "generated.md",
        }
        for name, value in (("current", current), ("baseline", baseline), ("generated", generated)):
            paths[name].write_text(value, encoding="utf-8")
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "--diff3",
                "-p",
                str(paths["current"]),
                str(paths["baseline"]),
                str(paths["generated"]),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    if result.returncode == 0:
        return result.stdout.rstrip("\n"), "merged"
    if result.returncode == 1:
        return None, "conflict"
    logger.error(
        "git merge-file 无法处理三方文本，转为阻断 support_id 不可用；stderr=%s",
        result.stderr.strip(),
    )
    return None, "merge_error"


def _merge_image_contexts(
    current_images: list[dict[str, Any]],
    generated_images: list[dict[str, Any]],
    repaired_fields: set[str],
) -> list[dict[str, Any]]:
    merged = {int(item["seq"]): dict(item) for item in current_images if isinstance(item.get("seq"), int)}
    for generated in generated_images:
        if generated.get("section") not in repaired_fields:
            continue
        seq = int(generated["seq"])
        target = merged.setdefault(seq, dict(generated))
        for key in ("section", "context_before", "context_after"):
            target[key] = generated.get(key, "")
        target.setdefault("desc", "")
    return [merged[seq] for seq in sorted(merged)]


def _apply_explicit_resolutions(
    *,
    support_id: str,
    conflicts: list[str],
    current: dict[str, Any],
    generated: dict[str, Any],
    resolution: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    expected_hashes = _json_object(resolution.get("expected_current_hashes"))
    field_rules = _json_object(resolution.get("fields"))
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for field_name in conflicts:
        expected = str(expected_hashes.get(field_name) or "")
        actual = _sha256_text(str(current.get(field_name) or ""))
        rule = _json_object(field_rules.get(field_name))
        if not expected or expected != actual or rule.get("strategy") != "generated":
            unresolved.append(field_name)
            continue
        value = str(generated.get(field_name) or "")
        for replacement in rule.get("replacements") or []:
            if not isinstance(replacement, dict):
                raise ValueError(f"{support_id}.{field_name} 冲突决议 replacement 必须是对象")
            source = str(replacement.get("from") or "")
            target = str(replacement.get("to") or "")
            if not source or value.count(source) != 1:
                raise ValueError(f"{support_id}.{field_name} 冲突决议无法唯一匹配 replacement.from")
            value = value.replace(source, target, 1)
        resolved[field_name] = value
    return resolved, unresolved


def _plan_entry(
    row: asyncpg.Record,
    cache_dir: Path,
    resolutions: dict[str, Any] | None = None,
) -> RepairPlan:
    support_id = str(row["support_id"])
    raw_path = cache_dir / support_id / "raw.json"
    base_report: dict[str, Any] = {
        "kbd_id": int(row["id"]),
        "support_id": support_id,
        "status": str(row["status"]),
        "lock_version": int(row["lock_version"]),
    }
    if not raw_path.exists():
        return RepairPlan({**base_report, "plan_status": "missing_source", "issues": []}, {})
    raw_bytes = raw_path.read_bytes()
    raw = json.loads(raw_bytes)
    content_html = str(raw.get("content") or "")
    base_report["raw_sha256"] = hashlib.sha256(raw_bytes).hexdigest()

    original_cache = settings.KBD_CACHE_DIR
    settings.KBD_CACHE_DIR = cache_dir
    try:
        generated = convert_kbd_structured(
            support_id,
            include_image_data=False,
            strict_integrity=False,
        )
    except Exception as exc:
        logger.exception("案例 %s 转换失败", support_id)
        return RepairPlan({**base_report, "plan_status": "conversion_failed", "error": str(exc), "issues": []}, {})
    finally:
        settings.KBD_CACHE_DIR = original_cache
    if generated is None:
        return RepairPlan(
            {**base_report, "plan_status": "conversion_failed", "error": "转换结果为空", "issues": []}, {}
        )
    conversion_integrity = _json_object(generated.get("conversion_integrity"))
    if conversion_integrity.get("valid") is False:
        return RepairPlan(
            {
                **base_report,
                "plan_status": "conversion_integrity_failed",
                "issues": [],
                "conversion_integrity": conversion_integrity,
            },
            {},
        )

    current = _current_payload(row)
    baseline = _json_object(row.get("baseline_payload"))
    if not baseline:
        return RepairPlan({**base_report, "plan_status": "missing_baseline", "issues": []}, {})
    sections = _parse_sections(content_html)
    image_map = _build_image_seq_map(content_html)
    issues_by_field: dict[str, list[dict[str, Any]]] = {}
    for title, field_name in _MD_TITLE_TO_FIELD.items():
        section_html = sections.get(title, "")
        if not section_html:
            continue
        issues = _section_issues(
            section_html,
            str(current.get(field_name) or ""),
            str(generated.get(field_name) or ""),
            image_map,
        )
        if issues:
            issues_by_field.setdefault(field_name, []).extend(issues)

    troubleshooting_html = sections.get("排查内容", "")
    if troubleshooting_html:
        issues = _section_issues(
            troubleshooting_html,
            str(current.get("steps_text") or ""),
            str(generated.get("steps_text") or ""),
            image_map,
        )
        if issues:
            issues_by_field.setdefault("steps_text", []).extend(issues)
    if not issues_by_field:
        return RepairPlan({**base_report, "plan_status": "clean", "issues": []}, {})

    updates: dict[str, Any] = {}
    merge_results: dict[str, str] = {}
    conflicts: list[str] = []
    for field_name in sorted(issues_by_field):
        merged, status = _merge_text(
            str(current.get(field_name) or ""),
            str(baseline.get(field_name) or ""),
            str(generated.get(field_name) or ""),
        )
        merge_results[field_name] = status
        if merged is None:
            conflicts.append(field_name)
        elif merged != current.get(field_name):
            updates[field_name] = merged
    explicit_resolution = _json_object((resolutions or {}).get(support_id))
    if conflicts and explicit_resolution:
        resolved, conflicts = _apply_explicit_resolutions(
            support_id=support_id,
            conflicts=conflicts,
            current=current,
            generated=generated,
            resolution=explicit_resolution,
        )
        updates.update(resolved)
        for field_name in resolved:
            merge_results[field_name] = "explicit_resolution"
    if conflicts:
        plan_status = "conflict"
        updates = {}
    elif not updates:
        plan_status = "already_repaired"
    else:
        plan_status = "ready_manual" if explicit_resolution else "ready"
        repaired_fields = set(updates)
        merged_images = _merge_image_contexts(
            _json_array(current.get("images_json")),
            _json_array(generated.get("images_json")),
            repaired_fields,
        )
        if merged_images != _json_array(current.get("images_json")):
            updates["images_json"] = merged_images
            updates["reviewed_image_seqs"] = []
        updates["lock_version"] = int(row["lock_version"])

    return RepairPlan(
        {
            **base_report,
            "plan_status": plan_status,
            "issues": issues_by_field,
            "merge_results": merge_results,
            "conflicts": conflicts,
            "explicit_resolution": bool(explicit_resolution),
            "current_hashes": {
                field_name: _sha256_text(str(current.get(field_name) or "")) for field_name in issues_by_field
            },
        },
        updates,
    )


def _report_payload(plans: list[RepairPlan], trace_id: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for plan in plans:
        status = str(plan.report["plan_status"])
        counts[status] = counts.get(status, 0) + 1
        _SCAN_RESULTS.labels(status=status).inc()
    return {
        "schema_version": 1,
        "trace_id": trace_id,
        "created_at": datetime.now(UTC).isoformat(),
        "summary": counts,
        "entries": [plan.report for plan in plans],
    }


async def _write_update(
    client: httpx.AsyncClient,
    config: RuntimeConfig,
    plan: RepairPlan,
) -> str:
    report = plan.report
    kbd_id = int(report["kbd_id"])
    headers = {
        "Authorization": f"Bearer {config.internal_api_token}",
        "Content-Type": "application/json",
        **traceparent(),
    }
    if report["status"] == "published":
        opened = await client.post(
            f"{config.kb_service_url}/api/admin/kbd/{kbd_id}/maintenance",
            headers=headers,
        )
        opened.raise_for_status()
        opened_payload = opened.json()
        body = dict(plan.updates)
        body["lock_version"] = int(opened_payload["lock_version"])
        response = await client.patch(
            f"{config.kb_service_url}/api/admin/kbd/{kbd_id}/maintenance",
            headers=headers,
            json=body,
        )
        response.raise_for_status()
        return "maintenance_staged"
    response = await client.patch(
        f"{config.kb_service_url}/api/admin/kbd/{kbd_id}",
        headers=headers,
        json=plan.updates,
    )
    response.raise_for_status()
    return "updated"


async def _run(args: argparse.Namespace) -> int:
    config = _runtime_config(args)
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    support_ids = args.ids or sorted(path.parent.name for path in config.cache_dir.glob("*/raw.json"))
    rows = await _load_entries(config.database_url, support_ids)
    found_ids = {str(row["support_id"]) for row in rows}
    logger.info(
        "开始 KBD 结构语义扫描 trace_id=%s requested=%d database_rows=%d",
        trace_id,
        len(support_ids),
        len(rows),
    )
    resolutions: dict[str, Any] = {}
    if args.resolutions:
        resolutions = _json_object(json.loads(Path(args.resolutions).read_text(encoding="utf-8")))
    plans = [_plan_entry(row, config.cache_dir, resolutions) for row in rows]
    for support_id in sorted(set(support_ids) - found_ids):
        plans.append(
            RepairPlan(
                {
                    "support_id": support_id,
                    "plan_status": "missing_database_entry",
                    "issues": [],
                },
                {},
            )
        )
    report = _report_payload(plans, trace_id)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    report_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
    logger.info(
        "扫描完成 trace_id=%s report=%s sha256=%s summary=%s", trace_id, output, report_sha256, report["summary"]
    )

    if not args.apply:
        print(
            json.dumps(
                {"trace_id": trace_id, "report": str(output), "sha256": report_sha256, "summary": report["summary"]},
                ensure_ascii=False,
            )
        )
        return 0
    if not config.internal_api_token:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，禁止写入")

    selected = [plan for plan in plans if plan.report.get("plan_status") in {"ready", "ready_manual"}]
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
        for plan in selected:
            try:
                result = await _write_update(client, config, plan)
                plan.report["apply_status"] = result
                _APPLY_RESULTS.labels(status=result).inc()
                logger.info(
                    "KBD 结构语义修复成功 trace_id=%s support_id=%s result=%s",
                    trace_id,
                    plan.report["support_id"],
                    result,
                )
            except Exception as exc:
                plan.report["apply_status"] = "failed"
                plan.report["apply_error"] = str(exc)
                _APPLY_RESULTS.labels(status="failed").inc()
                logger.exception("KBD 结构语义修复失败 support_id=%s", plan.report["support_id"])
    temporary.write_text(json.dumps(_report_payload(plans, trace_id), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    failed = [plan for plan in selected if plan.report.get("apply_status") == "failed"]
    print(
        json.dumps(
            {
                "trace_id": trace_id,
                "report": str(output),
                "selected": len(selected),
                "failed": len(failed),
            },
            ensure_ascii=False,
        )
    )
    return 1 if failed else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="扫描并三方修复 KBD 结构语义损失")
    parser.add_argument("--env-file", type=Path, help="运行环境配置文件")
    parser.add_argument("--cache-dir", type=Path, help="KBD 原始缓存目录")
    parser.add_argument("--ids", nargs="*", help="仅处理指定 support_id")
    parser.add_argument("--output", required=True, help="扫描与修复报告路径")
    parser.add_argument("--resolutions", type=Path, help="显式冲突决议 JSON；必须绑定当前字段哈希")
    parser.add_argument("--apply", action="store_true", help="显式执行无冲突修复；默认仅扫描")
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
