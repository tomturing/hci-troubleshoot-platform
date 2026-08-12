"""Authoritative sim-ssh context validation tests."""

import httpx
import pytest
from app.services.simulation_context_client import SimulationContextClient, SimulationContextError


def _context(**overrides):
    value = {
        "simulation": True,
        "execution_mode": "sim-ssh",
        "test_run_id": "run-27123",
        "case_id": "Q27123",
        "scenario_id": "kbd-27123-positive-realistic",
        "support_id": "27123",
        "kbd_revision": 24,
        "bundle_digest": "sha256:" + "a" * 64,
        "product": "HCI",
        "version": "6.11.1_R1",
        "components": ["虚拟机"],
        "topology": [],
        "virtual_node_id": "SIM-HCI-NODE-01",
        "node_ip": "hci-sim.hci-sim-dev.svc",
        "container": "host",
        "authority_scope": "dev_golden",
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_context_client_returns_bound_context_without_lease():
    async def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer context-token"
        assert request.url.params["case_id"] == "Q27123"
        return httpx.Response(200, json={"status": "preparing", "context": _context()})

    client = SimulationContextClient(
        "http://hci-sim",
        "context-token",
        transport=httpx.MockTransport(handler),
    )

    result = await client.get_active_context("run-27123", "Q27123")

    assert result["support_id"] == "27123"
    assert "password" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"case_id": "QOTHER"}, "SIM_CONTEXT_BINDING_MISMATCH"),
        ({"execution_mode": "ssh"}, "SIM_CONTEXT_MODE_INVALID"),
        ({"bundle_digest": "client-claimed"}, "SIM_CONTEXT_DIGEST_INVALID"),
        ({"kbd_revision": True}, "SIM_CONTEXT_REVISION_INVALID"),
        ({"components": []}, "SIM_CONTEXT_COMPONENTS_INVALID"),
    ],
)
async def test_context_client_rejects_untrusted_context(overrides, reason):
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"context": _context(**overrides)})
    )
    client = SimulationContextClient("http://hci-sim", "context-token", transport=transport)

    with pytest.raises(SimulationContextError, match=reason):
        await client.get_active_context("run-27123", "Q27123")


@pytest.mark.asyncio
async def test_context_client_rejects_inactive_runtime_response():
    client = SimulationContextClient(
        "http://hci-sim",
        "context-token",
        transport=httpx.MockTransport(lambda _request: httpx.Response(409, text="expired")),
    )

    with pytest.raises(SimulationContextError, match="SIM_CONTEXT_REJECTED_409"):
        await client.get_active_context("run-27123", "Q27123")


@pytest.mark.asyncio
async def test_context_client_reports_transport_failure_as_unavailable():
    async def handler(_request: httpx.Request):
        raise httpx.ConnectError("blocked by network policy")

    client = SimulationContextClient(
        "http://hci-sim",
        "context-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SimulationContextError, match="SIM_CONTEXT_UNAVAILABLE"):
        await client.get_active_context("run-27123", "Q27123")
