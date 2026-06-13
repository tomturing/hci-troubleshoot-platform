from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.skills.dynamic_runner import build_skill_name_candidates, extract_output_value


def test_build_skill_name_candidates():
    assert build_skill_name_candidates("disk_vendor_lifetime") == [
        "disk_vendor_lifetime",
        "disk-vendor-lifetime",
        "hci-disk-vendor-lifetime",
    ]
    assert build_skill_name_candidates("hci-alert-parsing") == ["hci-alert-parsing"]


def test_extract_output_value_with_path_and_variable_name():
    payload = {"ok": True, "values": {"node_ip": "172.28.24.4"}, "nested": {"value": "x"}}

    assert extract_output_value(payload, output_path="nested.value", variable_name="node_ip") == "x"
    assert extract_output_value(payload, output_path=None, variable_name="node_ip") == "172.28.24.4"
    assert extract_output_value({"value": "返修"}, output_path=None, variable_name="check_meth") == "返修"


@pytest.mark.asyncio
async def test_dynamic_skill_runner_executes_active_db_skill(monkeypatch):
    from app.skills import dynamic_runner
    from app.skills.dynamic_runner import DynamicSkillRunner
    from shared.dynamic_resource.models import ResourceSnapshot

    skill_row = SimpleNamespace(
        id=7,
        skill_name="hci-alert-parsing",
        display_name="告警解析",
        description="解析告警",
        instructions_md="请输出 node_ip",
        compatibility=None,
        license=None,
        allowed_tools="",
        metadata_json={},
        assets_json=[],
        references_json=[],
        is_active=True,
        updated_at=None,
    )
    scalars = MagicMock()
    scalars.all.return_value = [skill_row]
    result = MagicMock()
    result.scalars.return_value = scalars
    session = AsyncMock()
    session.execute.return_value = result
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    def session_factory():
        return session

    ai_client = MagicMock()
    ai_client.invoke = AsyncMock(return_value=SimpleNamespace(content='{"ok": true, "node_ip": "172.28.24.4"}'))
    ai_registry = MagicMock()
    ai_registry.get_client.return_value = ai_client

    async def fake_ensure_published(self, **kwargs):
        return ResourceSnapshot(
            resource_type=kwargs["resource_type"],
            resource_name=kwargs["resource_name"],
            revision=9,
            version=kwargs["version"],
            status=kwargs["status"],
            content=kwargs["content"],
            contract=kwargs["contract"],
            dependencies=kwargs["dependencies"],
            checksum="skill-checksum",
        )

    audit_calls = []

    async def fake_audit_skill_usage(self, snapshot, **kwargs):
        audit_calls.append((snapshot, kwargs))

    monkeypatch.setattr(dynamic_runner.DynamicResourcePublisher, "ensure_published", fake_ensure_published)
    monkeypatch.setattr(DynamicSkillRunner, "_audit_skill_usage", fake_audit_skill_usage)

    runner = DynamicSkillRunner(db_session_factory=session_factory, ai_registry=ai_registry)
    output = await runner.execute(
        "alert-parsing",
        {"alert_logs": [{"target": "SVR_aCloud_670"}]},
        variable_name="node_ip",
        conversation_id="c09eefb0-3bc7-4ad5-9909-8f00a3856763",
    )

    assert output["ok"] is True
    assert output["value"] == "172.28.24.4"
    assert output["skill_name"] == "hci-alert-parsing"
    assert output["resource_revision"]["revision"] == 9
    assert audit_calls[0][1]["variable_name"] == "node_ip"
    ai_client.invoke.assert_awaited_once()
