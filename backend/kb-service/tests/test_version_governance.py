"""统一版本治理的对抗性单元测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import version_governance
from app.routes.version_governance import VerificationAssetRequest
from app.services.version_governance import (
    SnapshotConflictError,
    _check_observed,
    _digest,
    append_verification_asset,
    verification_asset_digest,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from shared.dynamic_resource.adapters import kbd_resource_payload

_AUTH_HEADER = {"Authorization": "Bearer hci-dev-internal-token"}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(version_governance.router)
    version_governance.set_dependencies(MagicMock())
    return TestClient(app, raise_server_exceptions=True)


def test_digest_is_stable_for_mapping_order():
    """同一语义输入的 JSON 键顺序变化不能生成两个快照。"""

    assert _digest({"b": 2, "a": 1}) == _digest({"a": 1, "b": 2})


def test_verification_asset_digest_binds_snapshot_context():
    asset = {
        "signal_id": "sig-1",
        "processing_index": 0,
        "dataset_id": "dataset-1",
        "input_digest": "sha256:" + "1" * 64,
        "deterministic_input": {"input": "value"},
        "ai_input": {},
        "raw_response_hash": None,
        "output_json": {"value": 1},
        "evidence_json": {},
        "downstream_result": {},
        "model": "deterministic",
        "prompt_revision": "prompt-1",
        "contract_version": "contract-1",
        "run_id": "run-1",
        "result_status": "pass",
    }
    snapshot = {
        "knowledge_snapshot_digest": "sha256:" + "2" * 64,
        "signal_spec_digest": "sha256:" + "3" * 64,
        "simulation_spec_digest": "sha256:" + "4" * 64,
        "prompt_revision": "prompt-1",
        "tool_contract_revision": "tool-1",
        "policy_revision": "policy-1",
        "compiler_revision": "compiler-1",
    }

    first = verification_asset_digest("CASE-1", asset, snapshot)
    changed = verification_asset_digest(
        "CASE-1",
        asset,
        {**snapshot, "knowledge_snapshot_digest": "sha256:" + "5" * 64},
    )

    assert first.startswith("sha256:")
    assert first != changed


@pytest.mark.asyncio
async def test_repeated_verification_asset_does_not_advance_workspace():
    support_id = "CASE-1"
    snapshot_fields = {
        "knowledge_snapshot_digest": "sha256:" + "2" * 64,
        "signal_spec_digest": "sha256:" + "3" * 64,
        "simulation_spec_digest": "sha256:" + "4" * 64,
        "prompt_revision": "prompt-1",
        "tool_contract_revision": "tool-1",
        "policy_revision": "policy-1",
        "compiler_revision": "compiler-1",
    }
    asset = {
        "asset_digest": None,
        "signal_id": "sig-1",
        "processing_index": 0,
        "dataset_id": "dataset-1",
        "input_digest": "sha256:" + "1" * 64,
        "deterministic_input": {},
        "ai_input": {},
        "raw_response_hash": None,
        "output_json": {"value": 1},
        "evidence_json": {},
        "downstream_result": {},
        "model": "deterministic",
        "prompt_revision": "prompt-1",
        "contract_version": "contract-1",
        "run_id": "run-1",
        "result_status": "pass",
    }
    digest = verification_asset_digest(support_id, asset, snapshot_fields)
    package = SimpleNamespace(working_snapshot_digest="sha256:" + "9" * 64)
    current = SimpleNamespace(
        package_snapshot_digest=package.working_snapshot_digest,
        verification_set_digest="sha256:" + "8" * 64,
        manifest_json={"source_knowledge_revision_no": 7},
        **snapshot_fields,
    )
    row = SimpleNamespace(asset_digest=digest, support_id=support_id, result_status="pass")
    current_set = SimpleNamespace(asset_digests=[digest])
    session = AsyncMock()
    session.scalar.side_effect = [current, row, current_set]

    with (
        patch("app.services.version_governance._lock_package", new=AsyncMock(return_value=package)),
        patch("app.services.version_governance.create_snapshot", new=AsyncMock()) as create_mock,
    ):
        returned_row, returned_snapshot = await append_verification_asset(
            session,
            support_id=support_id,
            observed_snapshot_digest=package.working_snapshot_digest,
            asset=asset,
            snapshot_fields=snapshot_fields,
            actor_id="expert-1",
            trace_id="trace-1",
        )

    assert returned_row is row
    assert returned_snapshot is current
    create_mock.assert_not_awaited()


def test_kbd_payload_does_not_serialize_sqlalchemy_class_metadata():
    kbd = SimpleNamespace(id=1, support_id="CASE-1", status="published", entry_metadata={}, metadata=object())
    assert kbd_resource_payload(kbd)["contract"]["metadata"] == {}


def test_stale_observed_snapshot_is_rejected():
    """并发专家保存不能覆盖当前工作头。"""

    package = SimpleNamespace(working_snapshot_digest="sha256:current")
    with pytest.raises(SnapshotConflictError):
        _check_observed(package, "sha256:stale")


def test_missing_observed_snapshot_is_allowed_for_initial_workspace():
    package = SimpleNamespace(working_snapshot_digest=None)
    _check_observed(package, None)


def test_missing_observed_snapshot_is_rejected_for_existing_workspace():
    package = SimpleNamespace(working_snapshot_digest="sha256:current")
    with pytest.raises(SnapshotConflictError):
        _check_observed(package, None)


def test_verification_asset_status_is_fail_closed():
    with pytest.raises(ValidationError):
        VerificationAssetRequest(
            asset_digest="sha256:" + "a" * 64,
            signal_id="signal-1",
            processing_index=0,
            dataset_id="dataset-1",
            input_digest="sha256:" + "b" * 64,
            model="model-1",
            prompt_revision="prompt-1",
            contract_version="contract-1",
            result_status="unknown",
            knowledge_snapshot_digest="sha256:" + "c" * 64,
            signal_spec_digest="sha256:" + "d" * 64,
            simulation_spec_digest="sha256:" + "e" * 64,
            tool_contract_revision="tool-1",
            policy_revision="policy-1",
            compiler_revision="compiler-1",
            actor_id="expert-1",
        )


def test_version_governance_requires_internal_token(client: TestClient):
    response = client.get("/api/v1/kbd/CASE-1/context")
    assert response.status_code == 401


def test_snapshot_endpoint_returns_409_for_stale_workspace(client: TestClient):
    body = {
        "observed_snapshot_digest": "sha256:" + "0" * 64,
        "knowledge_snapshot_digest": "sha256:" + "c" * 64,
        "signal_spec_digest": "sha256:" + "d" * 64,
        "simulation_spec_digest": "sha256:" + "e" * 64,
        "prompt_revision": "prompt-1",
        "tool_contract_revision": "tool-1",
        "policy_revision": "policy-1",
        "compiler_revision": "compiler-1",
        "actor_id": "expert-1",
    }
    with patch.object(version_governance, "create_snapshot", new=AsyncMock(side_effect=SnapshotConflictError("stale"))):
        response = client.post("/api/v1/kbd/CASE-1/working-draft/snapshots", headers=_AUTH_HEADER, json=body)
    assert response.status_code == 409
