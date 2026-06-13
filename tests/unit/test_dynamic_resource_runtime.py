from types import SimpleNamespace

import pytest
from shared.dynamic_resource.cache import DynamicResourceCache
from shared.dynamic_resource.loader import DynamicResourceLoader, snapshot_revision_metadata
from shared.dynamic_resource.models import ResourceKey, ResourceSnapshot, UsageRecord
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.dynamic_resource.serialization import resource_checksum, sha256_json
from shared.models.dynamic_resource import (
    DynamicResourceActive,
    DynamicResourceRevision,
    DynamicResourceUsageAudit,
)


class FakeScalarResult:
    def __init__(self, value=None, values=None):
        self._value = value
        self._values = values if values is not None else ([] if value is None else [value])

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def all(self):
        return self._values


class FakeExecuteResult:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars if scalars is not None else ([] if scalar is None else [scalar])

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return FakeScalarResult(self._scalar, self._scalars)


class FakeDynamicResourceSession:
    def __init__(self):
        self.revisions = []
        self.active = {}
        self.audit_rows = []
        self._pending_existing = None
        self._pending_next_revision = None
        self._pending_loader_snapshot = None

    async def execute(self, stmt):
        text = str(stmt)
        if "pg_advisory_xact_lock" in text:
            return FakeExecuteResult(scalar=None)
        if "max(dynamic_resource_revision.revision)" in text:
            return FakeExecuteResult(scalar=self._pending_next_revision)
        if "JOIN dynamic_resource_active" in text:
            return FakeExecuteResult(scalar=self._pending_loader_snapshot, scalars=[self._pending_loader_snapshot])
        return FakeExecuteResult(scalar=self._pending_existing)

    async def get(self, model, key):
        if model is DynamicResourceActive:
            return self.active.get((key["resource_type"], key["resource_name"]))
        return None

    def add(self, row):
        if isinstance(row, DynamicResourceRevision):
            self.revisions.append(row)
            self._pending_loader_snapshot = row
        elif isinstance(row, DynamicResourceActive):
            self.active[(row.resource_type, row.resource_name)] = row
        elif isinstance(row, DynamicResourceUsageAudit):
            self.audit_rows.append(row)
        else:
            raise AssertionError(f"未知 fake row: {row!r}")

    async def flush(self):
        pass


@pytest.mark.asyncio
async def test_dynamic_resource_publisher_reuses_same_checksum_and_moves_active_pointer():
    session = FakeDynamicResourceSession()
    publisher = DynamicResourcePublisher(session)

    first_content = {"description": "执行 acli 命令"}
    first_checksum = resource_checksum(first_content, {"risk_level": 1}, [], version="1.0", status="published")
    session._pending_existing = None
    session._pending_next_revision = 1
    first = await publisher.ensure_published(
        resource_type="tool",
        resource_name="acli_exec",
        version="1.0",
        content=first_content,
        contract={"risk_level": 1},
        dependencies=[],
        trace_id="trace-1",
    )

    assert first.revision == 1
    assert first.checksum == first_checksum
    assert session.active[("tool", "acli_exec")].active_revision == 1
    assert len(session.revisions) == 1

    session._pending_existing = session.revisions[0]
    reused = await publisher.ensure_published(
        resource_type="tool",
        resource_name="acli_exec",
        version="1.0",
        content=first_content,
        contract={"risk_level": 1},
        dependencies=[],
        trace_id="trace-2",
    )

    assert reused.revision == 1
    assert len(session.revisions) == 1
    assert session.active[("tool", "acli_exec")].trace_id == "trace-2"

    changed_content = {"description": "执行 acli 命令，新描述"}
    session._pending_existing = None
    session._pending_next_revision = 2
    changed = await publisher.ensure_published(
        resource_type="tool",
        resource_name="acli_exec",
        version="1.1",
        content=changed_content,
        contract={"risk_level": 1},
        dependencies=[],
        trace_id="trace-3",
    )

    assert changed.revision == 2
    assert len(session.revisions) == 2
    assert session.active[("tool", "acli_exec")].active_revision == 2

    session._pending_existing = None
    session._pending_next_revision = 3
    version_changed = await publisher.ensure_published(
        resource_type="tool",
        resource_name="acli_exec",
        version="1.2",
        content=changed_content,
        contract={"risk_level": 1},
        dependencies=[],
        trace_id="trace-4",
    )

    assert version_changed.revision == 3
    assert len(session.revisions) == 3
    assert session.active[("tool", "acli_exec")].active_revision == 3


@pytest.mark.asyncio
async def test_dynamic_resource_loader_audit_usage_hashes_payloads():
    session = FakeDynamicResourceSession()
    row = SimpleNamespace(
        resource_type="skill",
        resource_name="hci-alert-parsing",
        revision=3,
        version="1.0",
        status="published",
        content_json={"instructions_md": "解析告警"},
        contract_json={"allowed_tools": []},
        dependency_json=[],
        checksum="abc",
        trace_id="trace-a",
        published_at=None,
    )
    session._pending_loader_snapshot = row

    loader = DynamicResourceLoader(session)
    snapshot = await loader.get_active("skill", "hci-alert-parsing")
    await loader.audit_usage(
        snapshot,
        UsageRecord(
            consumer="agent-service.dynamic_skill_runner",
            status="success",
            conversation_id="conv-1",
            case_id="case-1",
            trace_id="trace-a",
            exec_id="exec-1",
            input_payload={"alert_logs": [{"target": "node-1"}]},
            output_payload={"node_ip": "node-1"},
            metadata={"variable_name": "node_ip"},
        ),
    )

    assert snapshot_revision_metadata(snapshot) == {
        "resource_type": "skill",
        "resource_name": "hci-alert-parsing",
        "revision": 3,
        "version": "1.0",
        "checksum": "abc",
    }
    audit = session.audit_rows[0]
    assert audit.resource_type == "skill"
    assert audit.revision == 3
    assert audit.input_hash == sha256_json({"alert_logs": [{"target": "node-1"}]})
    assert audit.output_hash == sha256_json({"node_ip": "node-1"})
    assert audit.metadata_json == {"variable_name": "node_ip"}


@pytest.mark.asyncio
async def test_dynamic_resource_cache_ttl_and_invalidate():
    cache = DynamicResourceCache(ttl_seconds=60.0)
    key = ResourceKey("prompt", "base_identity_v1")
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return ResourceSnapshot(
            resource_type="prompt",
            resource_name="base_identity_v1",
            revision=calls,
            version="1.0",
            status="published",
            content={},
            contract={},
            dependencies=[],
            checksum=str(calls),
        )

    first = await cache.get_or_load(key, loader)
    second = await cache.get_or_load(key, loader)
    assert first is second
    assert calls == 1

    cache.invalidate("prompt", "base_identity_v1")
    third = await cache.get_or_load(key, loader)
    assert third.revision == 2
    assert calls == 2
