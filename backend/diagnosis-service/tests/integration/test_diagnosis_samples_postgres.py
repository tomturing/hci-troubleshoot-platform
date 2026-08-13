"""五篇诊断样例的离线同步、资源发布与诊断 PostgreSQL 回归。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import uuid4

import pytest
from app.auth import ActorContext
from app.services.offline_analysis_service import OfflineAnalysisService
from app.services.offline_resource_sync_service import OfflineResourceSyncService
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.schemas.signal_schema import certify_publishable_signals_json
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DIAGNOSIS_POSTGRES_INTEGRATION") != "1",
        reason="需要显式启用本地 PostgreSQL 集成测试",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[4]
SEED_PATH = REPO_ROOT / "database" / "seeds" / "04_kbd_diagnosis_samples.sql"
SAMPLE_IDS = {
    "SAMPLE-SIG-VM",
    "SAMPLE-SIG-CORE",
    "SAMPLE-SIG-LOG",
    "SAMPLE-SIG-NET-STO",
    "SAMPLE-SIG-HW-PLT",
}


async def _reconcile_tool_snapshots(session: AsyncSession, tool_names: list[str]) -> None:
    """把 Tool Registry（工具注册表）事实源冻结为同步可消费的修订。"""

    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT tool_name, display_name, category, description, usage_template,
                           parameters_schema, examples, risk_level, is_active, version
                    FROM tool_definition
                    WHERE tool_name = ANY(:tool_names)
                    ORDER BY tool_name
                    """
                ),
                {"tool_names": tool_names},
            )
        )
        .mappings()
        .all()
    )
    assert {row["tool_name"] for row in rows} == set(tool_names)
    publisher = DynamicResourcePublisher(session)
    for row in rows:
        await publisher.ensure_published(
            resource_type="tool",
            resource_name=row["tool_name"],
            version=str(row["version"] or "1.0"),
            content={
                "tool_name": row["tool_name"],
                "display_name": row["display_name"],
                "category": row["category"],
                "description": row["description"],
                "usage_template": row["usage_template"],
                "examples": row["examples"] or [],
                "is_active": bool(row["is_active"]),
            },
            contract={
                "parameters_schema": row["parameters_schema"] or {},
                "risk_level": int(row["risk_level"] or 1),
            },
            status="published" if row["is_active"] else "disabled",
            trace_id="diagnosis-sample-tool-reconcile",
        )


def _documents() -> dict[str, dict]:
    sql = SEED_PATH.read_text(encoding="utf-8")
    documents = [
        json.loads(payload) for payload in re.findall(r"\$signals\$\s*(\{.*?\})\s*\$signals\$::jsonb", sql, re.DOTALL)
    ]
    return {document["verification_contract"]["case_id"]: document for document in documents}


def _matching_evidence(signal: dict) -> object:
    matcher = signal.get("match") or {}
    matcher_type = matcher.get("type")
    if matcher_type == "keyword":
        pattern = matcher.get("pattern")
        return " ".join(pattern if isinstance(pattern, list) else [str(pattern)])
    if matcher_type == "regex":
        return "eth0    link up"
    if matcher_type == "state":
        return "running"
    if matcher_type == "threshold":
        return "Filesystem Use%\n/sf/log 83%\n"
    if matcher_type == "delta":
        if signal["acquire"]["tool"] == "qfk_storage":
            return "Volume IOPS\nsample-volume 10\nsample-volume 240\n"
        return "Metric Value\nrx_dropped 0\nrx_dropped 120\n"
    if matcher_type == "trend":
        if signal["acquire"]["tool"] == "qfk_hardware":
            return "Metric Value\ngpu_temperature 40\ngpu_temperature 45\ngpu_temperature 52\n"
        return "Metric Value\ntx_dropped 1\ntx_dropped 3\ntx_dropped 8\n"
    if matcher_type == "exists":
        return '{"data":[{"id":"sample"}]}'
    return "collected"


@pytest.mark.asyncio
async def test_five_samples_full_sync_publish_resources_and_reach_supported_diagnosis():
    """五篇样例必须经全量同步生成资源，并能用冻结映射完成离线诊断。"""

    documents = _documents()
    assert set(documents) == SAMPLE_IDS
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        # 隔离共享开发库现有发布数据，测试退出时由外层事务完整回滚。
        await session.execute(text("UPDATE kbd_entry SET status = 'draft' WHERE status = 'published'"))
        await session.execute(text("DELETE FROM offline_resource_sync_event"))
        await session.execute(text("DELETE FROM offline_resource_sync_change"))
        await session.execute(text("DELETE FROM offline_resource_sync_batch"))
        await session.execute(text("DELETE FROM offline_resource_sync_state"))
        # 分类树由 KBD pipeline 独立导入，冷启动数据库不会预置业务分类。
        # 本回归在外层事务内创建五个独立场景分类，以验证每篇样例均生成 Profile；
        # finally 回滚后不会污染开发库或改变分类生命周期。
        categories: list[str] = []
        category_suffix = uuid4().hex[:8]
        for index in range(5):
            category_id = f"TEST-DIAG-{index + 1}-{category_suffix}"
            categories.append(
                (
                    await session.execute(
                        text(
                            """
                            INSERT INTO kb_category (
                                code, name, domain, path_labels, level,
                                source, version, is_active
                            ) VALUES (
                                :category_id, :name, '测试',
                                CAST(:path_labels AS jsonb), 1,
                                'integration_test', '1.0', true
                            )
                            RETURNING code
                            """
                        ),
                        {
                            "category_id": category_id,
                            "name": f"诊断样例回归分类 {index + 1}",
                            "path_labels": json.dumps([f"诊断样例回归分类 {index + 1}"], ensure_ascii=False),
                        },
                    )
                ).scalar_one()
            )

        inserted: dict[int, dict] = {}
        for (source_support_id, raw_document), category_id in zip(sorted(documents.items()), categories, strict=True):
            document = certify_publishable_signals_json(raw_document)
            runtime_support_id = f"T{uuid4().hex[:16]}"
            kbd_id = (
                await session.execute(
                    text(
                        """
                        INSERT INTO kbd_entry (
                            support_id, title, problem_description, alert_info,
                            steps_text, root_cause, solution, signals_json,
                            content_md, content_raw, metadata, category_id,
                            ai_category_id, status, published_at
                        ) VALUES (
                            :support_id, :title, '离线同步与诊断回归', '诊断样例告警',
                            '按冻结 Signal Mapping 采集', '样例根因', '样例解决方案',
                            CAST(:signals_json AS jsonb), :content_md, :content_raw,
                            CAST(:metadata AS jsonb), :category_id, :category_id,
                            'published', CURRENT_TIMESTAMP
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "support_id": runtime_support_id,
                        "title": f"【回归副本】{source_support_id}",
                        "signals_json": json.dumps(document, ensure_ascii=False),
                        "content_md": f"# {source_support_id}\n\n离线同步与诊断回归。",
                        "content_raw": f"{source_support_id} 离线同步与诊断回归",
                        "metadata": json.dumps(
                            {
                                "sample_suite": "diagnosis-signal-matrix-v1",
                                "source_support_id": source_support_id,
                                "test_only": True,
                            },
                            ensure_ascii=False,
                        ),
                        "category_id": category_id,
                    },
                )
            ).scalar_one()
            resource_revision = await DynamicResourcePublisher(session).ensure_published(
                resource_type="kbd",
                resource_name=str(kbd_id),
                version="2",
                content={
                    "id": int(kbd_id),
                    "support_id": runtime_support_id,
                    "title": f"【回归副本】{source_support_id}",
                    "category_id": category_id,
                    "signals_json": document,
                    "status": "published",
                },
                contract={
                    "metadata": {
                        "sample_suite": "diagnosis-signal-matrix-v1",
                        "source_support_id": source_support_id,
                        "test_only": True,
                    }
                },
                trace_id="diagnosis-sample-offline-postgres",
            )
            inserted[int(kbd_id)] = {
                "source_support_id": source_support_id,
                "runtime_support_id": runtime_support_id,
                "category_id": category_id,
                "document": document,
                "revision": int(resource_revision.revision),
                "checksum": resource_revision.checksum,
            }

        tools = sorted(
            {signal["acquire"]["tool"] for item in inserted.values() for signal in item["document"]["signals"]}
        )
        await _reconcile_tool_snapshots(session, tools)

        actor = ActorContext(
            tenant_id="diagnosis-sample-test",
            user_id="sample-admin",
            roles=frozenset({"platform_admin"}),
        )
        sync_service = OfflineResourceSyncService(session)
        candidate = await sync_service.preview(actor=actor, mode="full")
        blocking = [item for item in candidate["validation_json"] if item.get("severity") == "error"]
        assert not blocking, json.dumps(blocking, ensure_ascii=False, indent=2)
        assert candidate["summary_json"]["published_kbd_count"] == 5
        assert candidate["summary_json"]["shared_scenario_kbd_count"] == 5
        assert candidate["summary_json"]["unresolved_category_kbd_count"] == 0
        mapping_changes = [item for item in candidate["changes"] if item["resource_type"] == "signal_mapping"]
        expected_mapping_count = sum(
            len(signal["acquire"]["args"].get("paths") or []) or 1 if signal["acquire"]["tool"] == "qkv_dialog" else 1
            for item in inserted.values()
            for signal in item["document"]["signals"]
        )
        assert len(mapping_changes) == expected_mapping_count
        assert {item["candidate_json"]["source_kbd_id"] for item in mapping_changes} == set(inserted)
        assert {item["resource_type"] for item in candidate["changes"]} >= {
            "collector",
            "collection_profile",
            "signal_mapping",
        }

        published = await sync_service.publish(
            actor=actor,
            batch_id=candidate["batch_id"],
            reason="五篇诊断样例离线全量回归",
        )
        assert published["status"] == "published", published.get("error_json")
        assert all(item["status"] == "published" for item in published["changes"])
        active_profile_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM dynamic_resource_active
                    WHERE resource_type = 'collection_profile'
                      AND resource_name = ANY(CAST(:categories AS varchar[]))
                    """
                ),
                {"categories": categories},
            )
        ).scalar_one()
        assert active_profile_count == 5

        mappings = (
            (
                await session.execute(
                    text(
                        """
                    SELECT * FROM offline_signal_collector_mapping
                    WHERE source_kbd_id = ANY(CAST(:kbd_ids AS bigint[]))
                      AND is_enabled = true
                    ORDER BY source_kbd_id, source_signal_id, priority, collector_id
                    """
                    ),
                    {"kbd_ids": list(inserted)},
                )
            )
            .mappings()
            .all()
        )
        assert len(mappings) == expected_mapping_count
        analysis_service = OfflineAnalysisService(session)
        for kbd_id, item in inserted.items():
            kbd_mappings = [dict(mapping) for mapping in mappings if int(mapping["source_kbd_id"]) == kbd_id]
            mapping_by_signal = {str(mapping["source_signal_id"]): mapping for mapping in kbd_mappings}
            assert len(mapping_by_signal) == len(item["document"]["signals"])
            evidence = []
            for signal in item["document"]["signals"]:
                mapping = mapping_by_signal[signal["id"]]
                structured_data = (
                    "platform version supported" if signal.get("role") == "exclude" else _matching_evidence(signal)
                )
                evidence.append(
                    {
                        "evidence_id": str(uuid4()),
                        "collector_id": mapping["collector_id"],
                        "evidence_status": "available",
                        "structured_data": structured_data,
                    }
                )
            ruleset = {
                "kbd_id": kbd_id,
                "support_id": item["runtime_support_id"],
                "title": f"【回归副本】{item['source_support_id']}",
                "root_cause": "样例根因",
                "solution": "样例解决方案",
                "operational_impact": "无",
                "recommendations": "仅用于回归",
                "category_id": item["category_id"],
                "metadata": {"source_support_id": item["source_support_id"]},
                "signals": item["document"]["signals"],
                "verification_contract": item["document"].get("verification_contract") or {},
                "generation_metadata": item["document"].get("generation_metadata") or {},
                "publish_validation": item["document"]["publish_validation"],
                "revision": item["revision"],
                "resource_checksum": item["checksum"],
                "offline_signal_mappings": kbd_mappings,
            }
            candidates, evaluations = await analysis_service._evaluate_kbds(
                {"session_id": f"sample-{kbd_id}"},
                {
                    "kbd_ruleset_snapshot": [ruleset],
                    "context_snapshot": {
                        "HOST": "sample-host",
                        "VM_ID": 1,
                        "END": "2026-08-12T00:00:00Z",
                        "REQUEST_ID": "sample-request",
                        "ALERT_ID": "sample-alert",
                    },
                },
                evidence,
            )
            assert len(evaluations) == len(item["document"]["signals"])
            assert candidates[0]["cdd_state"] == "SUPPORTED", {
                item["source_support_id"]: evaluations,
            }
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
