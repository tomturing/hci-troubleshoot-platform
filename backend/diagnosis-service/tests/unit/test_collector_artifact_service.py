"""Collector Artifact 目标选择和脚本生成测试。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.auth import ActorContext
from app.errors import DiagnosisError
from app.schemas.collector_artifact import CollectorArtifactGenerateRequest, _load_execution_specs
from app.services.collector_artifact_service import CollectorArtifactService


def plan_item(*, target: dict, activation_state: str = "active"):
    """构造采集计划项。"""

    return SimpleNamespace(
        item_id=uuid4(),
        collector_id="hci.task.failed",
        activation_state=activation_state,
        target=target,
    )


def test_multinode_plan_requires_explicit_target():
    """多节点计划生成制品时必须显式选择节点。"""

    items = [
        plan_item(target={"type": "vm", "id": "vm-1", "source_node": "node-1"}),
        plan_item(target={"type": "vm", "id": "vm-2", "source_node": "node-2"}),
    ]

    with pytest.raises(DiagnosisError) as exc_info:
        CollectorArtifactService._select_items(items, None)

    assert exc_info.value.code == "TARGET_NODE_REQUIRED"


def test_target_selection_excludes_deferred_items():
    """首轮制品不能包含 deferred 深度补采项。"""

    active = plan_item(target={"type": "node", "id": "node-1"})
    deferred = plan_item(target={"type": "node", "id": "node-1"}, activation_state="deferred")

    selected, target_key = CollectorArtifactService._select_items([active, deferred], "node-1")

    assert selected == [active]
    assert target_key == "node-1"


def test_unknown_target_node_is_rejected():
    """显式目标节点必须属于当前采集计划。"""

    items = [plan_item(target={"type": "node", "id": "node-1"})]

    with pytest.raises(DiagnosisError) as exc_info:
        CollectorArtifactService._select_items(items, "node-2")

    assert exc_info.value.code == "TARGET_NODE_NOT_FOUND"


def test_unresolved_source_node_accepts_explicit_target():
    """source_node 变量必须由制品请求显式解析。"""

    item = plan_item(target={"type": "variable", "id": "source_node"})

    selected, target_key = CollectorArtifactService._select_items([item], "node-2")

    assert selected == [item]
    assert target_key == "node-2"


def test_structured_artifact_keeps_direct_argv_without_shell():
    """结构化制品必须保留 argv 数组，不生成 Shell 文本。"""

    item = plan_item(target={"type": "node", "id": "node-1"})
    definition = SimpleNamespace(
        collector_id="hci.task.failed",
        timeout_seconds=30,
        max_output_mb=10,
    )
    artifact = CollectorArtifactService._build_artifact_document(
        artifact_id="artifact-1",
        session_id="session-1",
        collection_plan_id="plan-1",
        target_key="node-1",
        resolved_items=[
            {
                "plan_item": item,
                "definition": definition,
                "snapshot": SimpleNamespace(revision=3, checksum="checksum-3"),
                "rendered_command": "/opt/hci/task list --failed",
                "execution_spec": {"executor": "command", "argv": ["/opt/hci/task", "list", "--failed"]},
            }
        ],
    )

    assert artifact["schema_version"] == "1.0"
    assert artifact["execution_items"] == [
        {
            "item_id": str(item.item_id),
            "collector_id": "hci.task.failed",
            "collector_revision": 3,
            "collector_checksum": "checksum-3",
            "executor": "command",
            "timeout_seconds": 30,
            "max_output_bytes": 10 * 1024 * 1024,
            "argv": ["/opt/hci/task", "list", "--failed"],
        }
    ]
    assert "/bin/sh" not in str(artifact)


def test_artifact_response_exposes_only_signed_execution_spec_fields():
    """执行清单展示必须来自签名正文，且不得透传未批准的额外字段。"""

    specs = _load_execution_specs(
        json.dumps(
            {
                "execution_items": [
                    {
                        "item_id": "item-1",
                        "executor": "command",
                        "argv": ["/bin/echo", "node-1"],
                        "authorization": "must-not-leak",
                    },
                    {
                        "item_id": "item-2",
                        "executor": "http",
                        "method": "GET",
                        "path": "/api/v1/tasks?status=failed",
                        "token": "must-not-leak",
                    },
                ]
            }
        )
    )

    assert specs == {
        "item-1": {"executor": "command", "argv": ["/bin/echo", "node-1"]},
        "item-2": {"executor": "http", "method": "GET", "path": "/api/v1/tasks?status=failed"},
    }


def test_structured_artifact_supports_hci_api_and_manual_attachment_without_credentials():
    """结构化制品可编排只读 HCI API 和人工附件，且不会固化凭据。"""

    api_item = plan_item(target={"type": "node", "id": "node-1"})
    manual_item = plan_item(target={"type": "node", "id": "node-1"})
    artifact = CollectorArtifactService._build_artifact_document(
        artifact_id="artifact-1",
        session_id="session-1",
        collection_plan_id="plan-1",
        target_key="node-1",
        resolved_items=[
            {
                "plan_item": api_item,
                "definition": SimpleNamespace(
                    collector_id="hci.api.tasks",
                    executor="http",
                    timeout_seconds=30,
                    max_output_mb=8,
                ),
                "snapshot": SimpleNamespace(revision=1, checksum="api-checksum"),
                "rendered_command": "GET /api/v1/tasks?status=failed",
                "execution_spec": {
                    "executor": "http",
                    "method": "GET",
                    "path": "/api/v1/tasks?status=failed",
                },
            },
            {
                "plan_item": manual_item,
                "definition": SimpleNamespace(
                    collector_id="hci.manual.export",
                    executor="manual",
                    timeout_seconds=300,
                    max_output_mb=1024,
                ),
                "snapshot": SimpleNamespace(revision=1, checksum="manual-checksum"),
                "rendered_command": "请导出支持信息到 attachments/support.zip",
                "execution_spec": {
                    "executor": "manual",
                    "guide": "请导出支持信息到 attachments/support.zip",
                },
            },
        ],
    )

    items = artifact["execution_items"]
    assert items[0]["executor"] == "http"
    assert items[0]["method"] == "GET"
    assert items[1]["executor"] == "manual"
    assert "attachments/support.zip" in items[1]["guide"]
    assert "HCI_API_TOKEN" not in str(artifact)
    assert "Bearer test-token" not in str(artifact)


def test_structured_artifact_freezes_exact_kbd_signal_refs():
    """KBD 同步 Collector 的来源必须进入签名正文，禁止靠命令猜关联。"""

    item = plan_item(target={"type": "node", "id": "node-1"})
    artifact = CollectorArtifactService._build_artifact_document(
        artifact_id="artifact-1",
        session_id="session-1",
        collection_plan_id="plan-1",
        target_key="node-1",
        resolved_items=[
            {
                "plan_item": item,
                "definition": SimpleNamespace(
                    collector_id="kbd_qfk_task_example",
                    timeout_seconds=30,
                    max_output_mb=4,
                ),
                "snapshot": SimpleNamespace(revision=2, checksum="collector-checksum"),
                "rendered_command": "acli task get -s failed -l 10",
                "execution_spec": {
                    "executor": "command",
                    "argv": ["acli", "task", "get", "-s", "failed", "-l", "10"],
                },
            }
        ],
        kbd_ruleset_snapshot=[
            {
                "support_id": "SAMPLE-SIG-VM",
                "offline_signal_mappings": [
                    {
                        "collector_id": "kbd_qfk_task_example",
                        "source_signal_id": "s_task_failed",
                    },
                    {
                        "collector_id": "kbd_qfk_task_example",
                        "source_signal_id": "s_task_failed",
                    },
                ],
            },
            {
                "support_id": "SAMPLE-SIG-CORE",
                "offline_signal_mappings": [
                    {"collector_id": "other", "source_signal_id": "s_other"}
                ],
            },
        ],
    )

    assert artifact["execution_items"][0]["source_signal_refs"] == [
        {"support_id": "SAMPLE-SIG-VM", "signal_id": "s_task_failed"}
    ]


@pytest.mark.asyncio
async def test_artifact_generation_fails_before_resolution_when_encryption_is_unavailable():
    """缺少证据包加密密钥时不得生成一个到客户侧才失败的制品。"""

    session_id = uuid4()
    plan_id = uuid4()
    diagnosis_session = SimpleNamespace(session_id=session_id, status="plan_ready")
    plan = SimpleNamespace(plan_id=plan_id, session_id=session_id, status="ready")
    item = plan_item(target={"type": "node", "id": "node-1"})

    class SessionRepository:
        async def get_by_id_for_update(self, requested_session_id, tenant_id):
            return diagnosis_session

    class PlanRepository:
        async def get_by_id_for_tenant(self, requested_plan_id, tenant_id):
            return plan

        async def list_items(self, requested_plan_id):
            return [item]

    service = CollectorArtifactService(
        session=SimpleNamespace(),
        session_repository=SessionRepository(),
        plan_repository=PlanRepository(),
        artifact_repository=SimpleNamespace(),
        signer=SimpleNamespace(),
        envelope_encryption=None,
    )
    actor = ActorContext(
        tenant_id="tenant-a",
        user_id="support-a",
        roles=frozenset({"support_engineer"}),
    )
    with pytest.raises(DiagnosisError) as exc_info:
        await service.generate(
            actor=actor,
            session_id=str(session_id),
            command=CollectorArtifactGenerateRequest(
                collection_plan_id=plan_id,
                target_node="node-1",
            ),
            idempotency_key="missing-encryption-test",
        )
    assert exc_info.value.code == "ARTIFACT_ENCRYPTION_UNAVAILABLE"
