"""五篇诊断样例的真实批量审核发布 PostgreSQL 回归。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from app.routes import admin
from app.services.hci_sim_resolver import HciSimKbdResolver
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_KB_POSTGRES_INTEGRATION") != "1",
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


def _documents() -> dict[str, dict]:
    sql = SEED_PATH.read_text(encoding="utf-8")
    documents = [
        json.loads(payload) for payload in re.findall(r"\$signals\$\s*(\{.*?\})\s*\$signals\$::jsonb", sql, re.DOTALL)
    ]
    return {document["verification_contract"]["case_id"]: document for document in documents}


async def _reconcile_tool_snapshots(session: AsyncSession, tool_names: list[str]) -> None:
    """把样例引用的 Tool Registry 事实冻结为 hci-sim 可消费的修订。"""

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


class _TransactionDatabase:
    """让被测路由的多次 commit 只提交 Savepoint（保存点）。"""

    def __init__(self, connection):
        self._connection = connection

    def async_session_factory(self) -> AsyncSession:
        return AsyncSession(
            bind=self._connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )


@pytest.mark.asyncio
async def test_five_samples_batch_approve_and_publish_immutable_revisions():
    """真实批量任务必须把五篇样例全部发布，并保留 Expert/Runtime 修订。"""

    documents = _documents()
    assert set(documents) == SAMPLE_IDS
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    connection = await engine.connect()
    transaction = await connection.begin()
    database = _TransactionDatabase(connection)
    previous_database = admin._db_manager
    previous_embedding = admin._embedding_service
    batch_id = None
    try:
        admin.set_dependencies(database)
        async with database.async_session_factory() as session:
            # 冷启动 CI 只加载服务运行必需的 SQL seed，不会预先导入由 KBD
            # pipeline 管理的分类树。回归用分类必须跟随外层事务创建并回滚，
            # 不能把“环境中恰好已有 kb_category”作为五篇样例闭环的隐式前提。
            category_id = f"TEST-DIAG-{uuid4().hex[:12]}"
            category_id = (
                await session.execute(
                    text(
                        """
                        INSERT INTO kb_category (
                            code, name, domain, path_labels, level,
                            source, version, is_active
                        ) VALUES (
                            :category_id, '诊断样例回归分类', '测试',
                            CAST('["诊断样例回归分类"]' AS jsonb), 1,
                            'integration_test', '1.0', true
                        )
                        RETURNING code
                        """
                    ),
                    {"category_id": category_id},
                )
            ).scalar_one()
            tool_names = sorted(
                {
                    signal["acquire"]["tool"]
                    for document in documents.values()
                    for signal in document["signals"]
                }
            )
            await _reconcile_tool_snapshots(session, tool_names)
            inserted: dict[int, str] = {}
            for support_id, document in sorted(documents.items()):
                kbd_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO kbd_entry (
                                support_id, title, problem_description, alert_info,
                                steps_text, root_cause, solution, signals_json,
                                content_md, content_raw, metadata, category_id,
                                ai_category_id, status
                            ) VALUES (
                                :runtime_support_id, :title, :problem_description, :alert_info,
                                :steps_text, :root_cause, :solution, CAST(:signals_json AS jsonb),
                                :content_md, :content_raw, CAST(:metadata AS jsonb), :category_id,
                                :category_id, 'draft'
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "runtime_support_id": f"T{uuid4().hex[:16]}",
                            "title": f"【回归副本】{support_id}",
                            "problem_description": "验证在线与离线诊断共同使用的样例。",
                            "alert_info": "诊断回归告警。",
                            "steps_text": "按 Signal v2 契约采集并判断证据。",
                            "root_cause": "仅用于回归验证。",
                            "solution": "不执行处置动作。",
                            "signals_json": json.dumps(document, ensure_ascii=False),
                            "content_md": f"# {support_id}\n\n五篇诊断样例发布回归。",
                            "content_raw": f"{support_id} 五篇诊断样例发布回归",
                            "metadata": json.dumps(
                                {
                                    "sample_suite": "diagnosis-signal-matrix-v1",
                                    "source_support_id": support_id,
                                    "test_only": True,
                                },
                                ensure_ascii=False,
                            ),
                            "category_id": category_id,
                        },
                    )
                ).scalar_one()
                inserted[int(kbd_id)] = support_id
            await session.commit()

        request_snapshot = {
            "reviewer_id": 95394,
            "review_note": "五篇在线/离线诊断样例自动回归",
            "entries": {str(kbd_id): {"lock_version": 1, "category_id": category_id} for kbd_id in inserted},
        }
        batch_id = await admin._create_batch_job(
            list(inserted),
            "approve",
            "diagnosis-sample-postgres-preflight",
            request_json=request_snapshot,
        )
        # 单连接外层事务无法并发创建多个 Savepoint；生产并发逻辑已有独立批次测试，
        # 本测试把处理并发度收为 1，专注验证五篇样例的完整发布生命周期。
        with (
            patch("app.routes.admin._check_auth"),
            patch("app.routes.admin.asyncio.Semaphore", return_value=asyncio.Semaphore(1)),
        ):
            await admin._run_batch_job(
                batch_id,
                admin._approve_one,
                "diagnosis-sample-postgres-preflight",
            )

        async with database.async_session_factory() as session:
            job = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, total_count, completed_count,
                               succeeded_count, failed_count, interrupted_count
                        FROM kbd_batch_job
                        WHERE batch_id = CAST(:batch_id AS uuid)
                        """
                        ),
                        {"batch_id": str(batch_id)},
                    )
                )
                .mappings()
                .one()
            )
            assert dict(job) == {
                "status": "completed",
                "total_count": 5,
                "completed_count": 5,
                "succeeded_count": 5,
                "failed_count": 0,
                "interrupted_count": 0,
            }
            items = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT kbd_id, status, result_json, error_json
                        FROM kbd_batch_job_item
                        WHERE batch_id = CAST(:batch_id AS uuid)
                        ORDER BY item_id
                        """
                        ),
                        {"batch_id": str(batch_id)},
                    )
                )
                .mappings()
                .all()
            )
            assert len(items) == 5
            assert all(item["status"] == "succeeded" for item in items), items
            assert all(item["error_json"] == {} for item in items), items
            assert all(item["result_json"]["status"] == "published" for item in items)

            published = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT id, status, signals_json
                        FROM kbd_entry
                        WHERE id = ANY(CAST(:kbd_ids AS bigint[]))
                        ORDER BY id
                        """
                        ),
                        {"kbd_ids": list(inserted)},
                    )
                )
                .mappings()
                .all()
            )
            assert len(published) == 5
            assert all(row["status"] == "published" for row in published)
            assert all((row["signals_json"].get("publish_validation") or {}).get("status") for row in published)

            expert_revision_count = (
                await session.execute(
                    text(
                        """
                        SELECT count(*) FROM kbd_revision
                        WHERE kbd_entry_id = ANY(CAST(:kbd_ids AS bigint[]))
                          AND revision_type = 'expert'
                        """
                    ),
                    {"kbd_ids": list(inserted)},
                )
            ).scalar_one()
            runtime_revision_count = (
                await session.execute(
                    text(
                        """
                        SELECT count(*) FROM dynamic_resource_revision
                        WHERE resource_type = 'kbd'
                          AND resource_name = ANY(CAST(:resource_names AS varchar[]))
                          AND status = 'published'
                        """
                    ),
                    {"resource_names": [str(kbd_id) for kbd_id in inserted]},
                )
            ).scalar_one()
            assert expert_revision_count == 5
            assert runtime_revision_count == 5

            # 发布不是终点：同一事务内直接验证实验室控制面能从真实不可变修订和
            # Tool Registry 编译全部 Signal，不允许样例“发布成功但运行时不可用”。
            report = await HciSimKbdResolver().resolve_all(session)
            sample_results = {
                item.resolved.metadata.get("source_support_id", item.support_id): item
                for item in report.results
                if item.resolved is not None
                and item.resolved.metadata.get("sample_suite") == "diagnosis-signal-matrix-v1"
                and item.support_id.startswith("T")
            }
            assert set(sample_results) == SAMPLE_IDS
            assert all(item.status == "ready_for_artifact_binding" for item in sample_results.values())
            for source_support_id, result in sample_results.items():
                assert result.resolved is not None
                expected = {signal["id"] for signal in documents[source_support_id]["signals"]}
                actual = {route.signal_id for route in result.resolved.synthetic_routes}
                assert actual == expected, (source_support_id, actual, expected)
    finally:
        admin._db_manager = previous_database
        admin._embedding_service = previous_embedding
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
