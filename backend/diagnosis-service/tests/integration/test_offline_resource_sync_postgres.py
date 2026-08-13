"""KBD 与 Tool Registry 驱动离线资源同步 PostgreSQL 集成测试。"""

import json
import os
from uuid import uuid4

import pytest
from app.auth import ActorContext
from app.errors import DiagnosisError
from app.schemas.collection_profile import CollectionProfilePublishRequest
from app.schemas.collector_definition import CollectorDefinitionWrite
from app.services.collection_profile_service import CollectionProfileService
from app.services.collector_definition_service import CollectorDefinitionService
from app.services.offline_resource_sync_service import OfflineResourceSyncService
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DIAGNOSIS_POSTGRES_INTEGRATION") != "1",
        reason="需要显式启用本地 PostgreSQL 集成测试",
    ),
]


async def _reconcile_tool_snapshots(session: AsyncSession, tool_names: list[str]) -> None:
    """把事务内 Tool 事实源发布为不可变修订，避免依赖服务启动顺序。"""

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
            trace_id="sync-integration-tool-reconcile",
        )


@pytest.mark.asyncio
async def test_kbd_sync_publish_and_batch_rollback_are_auditable():
    """验证增量候选、原子发布、整批回滚和动作历史。"""

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        # 隔离当前环境中的已发布 KBD；外层事务最终统一回滚。
        await session.execute(text("UPDATE kbd_entry SET status = 'draft' WHERE status = 'published'"))
        await session.execute(text("DELETE FROM offline_resource_sync_event"))
        await session.execute(text("DELETE FROM offline_resource_sync_change"))
        await session.execute(text("DELETE FROM offline_resource_sync_batch"))
        await session.execute(text("DELETE FROM offline_resource_sync_state"))
        category_id = (
            await session.execute(
                text(
                    """
                    SELECT code FROM kb_category
                    WHERE code IS NOT NULL AND is_active = true
                    ORDER BY level DESC, code
                    LIMIT 1
                    """
                )
            )
        ).scalar_one()

        signals = {
            "schema_version": "2.0",
            "signals": [
                {"id": "task-failed", "acquire": {"tool": "qkv_task", "args": {"keyword": "备份"}}},
                {
                    "id": "log-error",
                    "acquire": {"tool": "qfk_log", "args": {"file": "vtpdaemon.log"}},
                    "match": {"type": "keyword", "pattern": "error", "expected": True},
                },
            ],
        }
        kbd_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO kbd_entry (
                        support_id, title, problem_description, root_cause, solution,
                        signals_json, metadata, category_id, status
                    ) VALUES (
                        :support_id, '虚拟机备份失败同步测试', '备份失败', '服务异常', '恢复服务',
                        CAST(:signals AS jsonb), '{}'::jsonb, :category_id, 'published'
                    ) RETURNING id
                    """
                ),
                {
                    "support_id": f"T{uuid4().hex[:12]}",
                    "signals": json.dumps(signals),
                    "category_id": category_id,
                },
            )
        ).scalar_one()
        await DynamicResourcePublisher(session).ensure_published(
            resource_type="kbd",
            resource_name=str(kbd_id),
            version="1.0",
            content={
                "id": kbd_id,
                "support_id": "sync-test",
                "title": "虚拟机备份失败同步测试",
                "category_id": category_id,
                "signals_json": signals,
                "status": "published",
            },
            contract={"metadata": {}},
            trace_id="sync-integration-test",
        )

        # 第二篇 KBD 复用完全相同的 qfk_log 采集要求，候选映射必须聚合而不能重复插入。
        duplicate_kbd_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO kbd_entry (
                        support_id, title, problem_description, root_cause, solution,
                        signals_json, metadata, category_id, status
                    ) VALUES (
                        :support_id, '虚拟机备份失败重复映射测试', '备份失败', '日志异常', '恢复服务',
                        CAST(:signals AS jsonb), '{}'::jsonb, :category_id, 'published'
                    ) RETURNING id
                    """
                ),
                {
                    "support_id": f"T{uuid4().hex[:12]}",
                    "category_id": category_id,
                    "signals": json.dumps(
                        {
                            "schema_version": "2.0",
                            "signals": [
                                {
                                    "id": "same-log-error",
                                    "acquire": {
                                        "tool": "qfk_log",
                                        "args": {"file": "vtpdaemon.log"},
                                    },
                                    "match": {"type": "keyword", "pattern": "error", "expected": True},
                                }
                            ],
                        }
                    ),
                },
            )
        ).scalar_one()
        await DynamicResourcePublisher(session).ensure_published(
            resource_type="kbd",
            resource_name=str(duplicate_kbd_id),
            version="1.0",
            content={
                "id": duplicate_kbd_id,
                "support_id": "sync-duplicate-test",
                "title": "虚拟机备份失败重复映射测试",
                "category_id": category_id,
                "signals_json": {
                    "schema_version": "2.0",
                    "signals": [
                        {
                            "id": "same-log-error",
                            "acquire": {
                                "tool": "qfk_log",
                                "args": {"file": "vtpdaemon.log"},
                            },
                            "match": {"type": "keyword", "pattern": "error", "expected": True},
                        }
                    ],
                },
                "status": "published",
            },
            contract={"metadata": {}},
            trace_id="sync-integration-duplicate-test",
        )
        await _reconcile_tool_snapshots(session, ["qkv_task", "qfk_log"])

        actor = ActorContext(
            tenant_id="sync-test",
            user_id="sync-admin",
            roles=frozenset({"platform_admin"}),
        )
        service = OfflineResourceSyncService(session)
        candidate = await service.preview(actor=actor, mode="incremental")
        assert candidate["status"] == "candidate"
        assert candidate["kbd_change_count"] >= 1
        assert candidate["target_tool_cursor"] >= candidate["base_tool_cursor"]
        assert not [item for item in candidate["validation_json"] if item.get("severity") == "error"]
        assert candidate["summary_json"]["scenario_source"] == "kbd.category_id"
        assert candidate["summary_json"]["published_kbd_count"] == 2
        assert candidate["summary_json"]["shared_scenario_kbd_count"] == 2
        assert candidate["summary_json"]["unresolved_category_kbd_count"] == 0
        assert candidate["summary_json"]["skipped_non_offline_kbd_count"] == 0
        changed_resource_types = {item["resource_type"] for item in candidate["changes"]}
        # 共享开发库可能已经发布相同内容的 Collector；增量同步应复用它，不制造重复修订。
        assert {"collection_profile", "signal_mapping"}.issubset(changed_resource_types)
        assert changed_resource_types.issubset({"collector", "collection_profile", "signal_mapping"})
        mapping_changes = [item for item in candidate["changes"] if item["resource_type"] == "signal_mapping"]
        assert len(mapping_changes) == 3
        assert {item["candidate_json"]["source_kbd_id"] for item in mapping_changes} == {
            kbd_id,
            duplicate_kbd_id,
        }
        assert all(len(item["source_kbd_ids"]) == 1 for item in mapping_changes)
        assert all(item["candidate_json"]["source_signal_id"] for item in mapping_changes)
        assert all(item["candidate_json"]["execution_contract_checksum"] for item in mapping_changes)
        assert all(item["source_tool_revisions"] for item in mapping_changes)

        # 旧版本候选可能已按离线专属白名单跳过 KBD，升级后不能继续误发布。
        await session.execute(
            text(
                """
                UPDATE offline_resource_sync_batch
                SET summary_json = summary_json - 'scenario_source'
                WHERE batch_id = :batch_id
                """
            ),
            {"batch_id": candidate["batch_id"]},
        )
        with pytest.raises(DiagnosisError) as outdated_policy_error:
            await service.publish(actor=actor, batch_id=candidate["batch_id"], reason="旧策略候选不得发布")
        assert outdated_policy_error.value.code == "SYNC_BATCH_POLICY_OUTDATED"
        await session.execute(
            text(
                """
                UPDATE offline_resource_sync_batch
                SET summary_json = jsonb_set(summary_json, '{scenario_source}', '"kbd.category_id"'::jsonb)
                WHERE batch_id = :batch_id
                """
            ),
            {"batch_id": candidate["batch_id"]},
        )

        published = await service.publish(
            actor=actor,
            batch_id=candidate["batch_id"],
            reason="集成测试批准发布",
        )
        assert published["status"] == "published", published.get("error_json")
        assert all(item["status"] == "published" for item in published["changes"])
        assert (
            await session.execute(
                text(
                    """
                    SELECT active_revision FROM dynamic_resource_active
                    WHERE resource_type = 'collection_profile' AND resource_name = :category_id
                    """
                ),
                {"category_id": category_id},
            )
        ).scalar_one() >= 1
        qfk_collector = next(
            item["candidate_json"]
            for item in published["changes"]
            if item["resource_type"] == "collector"
            and item["candidate_json"].get("collector_id", "").startswith("kbd_qfk_log_")
        )
        # 命令由 Shared Resolver + Catalog 编译，不依赖 Tool 的展示模板。
        assert qfk_collector["command_template"].startswith("acli log get -E -k {keyword} -f {file}")
        assert "journalctl" not in qfk_collector["command_template"]
        assert qfk_collector["managed_by"] == "kbd_sync"
        assert qfk_collector["generation_metadata"]["tool_name"] == "qfk_log"
        assert qfk_collector["generation_metadata"]["resolution_catalog_version"]
        assert qfk_collector["generation_metadata"]["resolution_snapshot"]["argv"][0:3] == ["acli", "log", "get"]
        profile_content = (
            await session.execute(
                text(
                    """
                    SELECT r.content_json
                    FROM dynamic_resource_active a
                    JOIN dynamic_resource_revision r
                      ON r.resource_type = a.resource_type
                     AND r.resource_name = a.resource_name
                     AND r.revision = a.active_revision
                    WHERE a.resource_type = 'collection_profile'
                      AND a.resource_name = :category_id
                    """
                ),
                {"category_id": category_id},
            )
        ).scalar_one()
        qfk_profile_item = next(
            item for item in profile_content["items"] if item["collector_id"] == qfk_collector["collector_id"]
        )
        assert qfk_profile_item["parameters"]["keyword"] == "error"
        assert qfk_profile_item["parameters"]["file"] == "vtpdaemon.log"

        with pytest.raises(DiagnosisError) as collector_edit_error:
            await CollectorDefinitionService(session).save_draft(
                actor=actor,
                collector_id=qfk_collector["collector_id"],
                command=CollectorDefinitionWrite.model_validate(qfk_collector),
                if_match=None,
            )
        assert collector_edit_error.value.code == "SYNC_MANAGED_COLLECTOR_READ_ONLY"
        with pytest.raises(DiagnosisError) as profile_edit_error:
            await CollectionProfileService(session).publish(
                actor=actor,
                profile_id=category_id,
                command=CollectionProfilePublishRequest.model_validate(
                    {"version": published["summary_json"].get("version", "1.0.0"), "profile": profile_content}
                ),
            )
        assert profile_edit_error.value.code == "SYNC_MANAGED_PROFILE_READ_ONLY"

        # 仅发布 Tool 新修订，不变更 KBD；增量同步仍必须自动重算受影响场景。
        tool_snapshot = (
            (
                await session.execute(
                    text(
                        """
                    SELECT r.content_json, r.contract_json, r.version
                    FROM dynamic_resource_active a
                    JOIN dynamic_resource_revision r
                      ON r.resource_type = a.resource_type
                     AND r.resource_name = a.resource_name
                     AND r.revision = a.active_revision
                    WHERE a.resource_type = 'tool' AND a.resource_name = 'qfk_log'
                    """
                    )
                )
            )
            .mappings()
            .one()
        )
        changed_tool_content = dict(tool_snapshot["content_json"])
        changed_tool_content["description"] = str(changed_tool_content.get("description") or "") + " 审计测试"
        await DynamicResourcePublisher(session).ensure_published(
            resource_type="tool",
            resource_name="qfk_log",
            version=str(tool_snapshot["version"]),
            content=changed_tool_content,
            contract=dict(tool_snapshot["contract_json"]),
            trace_id="sync-tool-only-change-test",
        )
        tool_only_candidate = await service.preview(actor=actor, mode="incremental")
        assert tool_only_candidate["kbd_change_count"] == 0
        assert tool_only_candidate["tool_change_count"] == 1
        assert tool_only_candidate["summary_json"]["changed_tools"] == ["qfk_log"]
        assert not [item for item in tool_only_candidate["validation_json"] if item.get("severity") == "error"]
        tool_only_published = await service.publish(
            actor=actor,
            batch_id=tool_only_candidate["batch_id"],
            reason="集成测试验证 Tool 单源变更",
        )
        assert tool_only_published["status"] == "published"
        assert any(
            item["resource_type"] == "collector"
            and item["resource_name"] == qfk_collector["collector_id"]
            and item["change_type"] == "update"
            for item in tool_only_published["changes"]
        )
        assert any(
            item["resource_type"] == "collection_profile" and item["resource_name"] == category_id
            for item in tool_only_published["changes"]
        )
        tool_only_rolled_back = await service.rollback(
            actor=actor,
            batch_id=tool_only_candidate["batch_id"],
            reason="集成测试先回滚 Tool 单源变更",
        )
        assert tool_only_rolled_back["status"] == "rolled_back"

        # 已发布 KBD 退出运行态时追加 disabled tombstone；增量同步必须立即清理全部下游资源，
        # 不能依赖下一次全量检测偶然纠偏。
        for retired_kbd_id in (kbd_id, duplicate_kbd_id):
            active_kbd = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT r.content_json, r.contract_json
                            FROM dynamic_resource_active a
                            JOIN dynamic_resource_revision r
                              ON r.resource_type = a.resource_type
                             AND r.resource_name = a.resource_name
                             AND r.revision = a.active_revision
                            WHERE a.resource_type = 'kbd' AND a.resource_name = :resource_name
                            """
                        ),
                        {"resource_name": str(retired_kbd_id)},
                    )
                )
                .mappings()
                .one()
            )
            tombstone_contract = dict(active_kbd["contract_json"] or {})
            tombstone_contract["lifecycle"] = {"state": "draft", "tombstone": True}
            tombstone_content = dict(active_kbd["content_json"] or {})
            tombstone_content["status"] = "draft"
            await DynamicResourcePublisher(session).ensure_published(
                resource_type="kbd",
                resource_name=str(retired_kbd_id),
                version="1.0",
                content=tombstone_content,
                contract=tombstone_contract,
                status="disabled",
                trace_id="sync-kbd-tombstone-test",
            )
            await session.execute(
                text("DELETE FROM dynamic_resource_active WHERE resource_type = 'kbd' AND resource_name = :name"),
                {"name": str(retired_kbd_id)},
            )
            await session.execute(
                text("UPDATE kbd_entry SET status = 'draft' WHERE id = :id"),
                {"id": retired_kbd_id},
            )

        tombstone_candidate = await service.preview(actor=actor, mode="incremental")
        assert tombstone_candidate["kbd_change_count"] == 2
        assert not [item for item in tombstone_candidate["validation_json"] if item.get("severity") == "error"]
        assert any(
            item["resource_type"] == "collector" and item["change_type"] == "disable"
            for item in tombstone_candidate["changes"]
        )
        assert any(
            item["resource_type"] == "signal_mapping" and item["change_type"] == "disable"
            for item in tombstone_candidate["changes"]
        )
        assert any(
            item["resource_type"] == "collection_profile"
            and item["resource_name"] == category_id
            and item["change_type"] == "disable"
            for item in tombstone_candidate["changes"]
        )
        tombstone_published = await service.publish(
            actor=actor,
            batch_id=tombstone_candidate["batch_id"],
            reason="集成测试验证 KBD 停用墓碑增量清理",
        )
        assert tombstone_published["status"] == "published"
        assert (
            await session.execute(
                text(
                    """
                    SELECT is_enabled FROM collection_profile_definition
                    WHERE profile_id = :category_id
                    """
                ),
                {"category_id": category_id},
            )
        ).scalar_one() is False

        tombstone_rolled_back = await service.rollback(
            actor=actor,
            batch_id=tombstone_candidate["batch_id"],
            reason="集成测试恢复 KBD 停用前资源",
        )
        assert tombstone_rolled_back["status"] == "rolled_back"

        rolled_back = await service.rollback(
            actor=actor,
            batch_id=candidate["batch_id"],
            reason="集成测试验证整批回滚",
        )
        assert rolled_back["status"] == "rolled_back"
        assert all(item["status"] == "rolled_back" for item in rolled_back["changes"])
        assert [(item["action"], item["result"]) for item in rolled_back["events"]] == [
            ("preview", "started"),
            ("preview", "succeeded"),
            ("publish", "started"),
            ("publish", "succeeded"),
            ("rollback", "started"),
            ("rollback", "succeeded"),
        ]
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
