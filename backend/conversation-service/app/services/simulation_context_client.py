"""Read-only client for authoritative hci-sim TestRun context."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx
from shared.observability.logger import get_logger

logger = get_logger("simulation-context-client")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class SimulationContextError(RuntimeError):
    """The TestRun context cannot be trusted or is not active."""


class SimulationContextClient:
    def __init__(
        self,
        base_url: str,
        control_token: str,
        timeout_sec: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.control_token = control_token.strip()
        self.timeout_sec = timeout_sec
        self.transport = transport

    async def get_active_context(self, test_run_id: str, case_id: str) -> dict[str, Any]:
        test_run_id = test_run_id.strip()
        case_id = case_id.strip()
        if not test_run_id or not case_id:
            raise SimulationContextError("SIM_CONTEXT_BINDING_MISSING")
        if not self.base_url or not self.control_token:
            raise SimulationContextError("SIM_CONTEXT_CLIENT_NOT_CONFIGURED")

        url = f"{self.base_url}/v1/simulations/test-runs/{quote(test_run_id, safe='')}/context"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec, transport=self.transport) as client:
                response = await client.get(
                    url,
                    params={"case_id": case_id},
                    headers={"Authorization": f"Bearer {self.control_token}"},
                )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                event="simulation_context_request_failed",
                message="hci-sim TestRun context request failed before receiving a response",
                test_run_id=test_run_id,
                case_id=case_id,
                error_type=type(exc).__name__,
            )
            raise SimulationContextError("SIM_CONTEXT_UNAVAILABLE") from exc
        if response.status_code != 200:
            logger.warning(
                event="simulation_context_rejected",
                message="hci-sim rejected TestRun context lookup",
                test_run_id=test_run_id,
                case_id=case_id,
                status_code=response.status_code,
            )
            raise SimulationContextError(f"SIM_CONTEXT_REJECTED_{response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise SimulationContextError("SIM_CONTEXT_INVALID_JSON") from exc
        context = body.get("context") if isinstance(body, dict) else None
        if not isinstance(context, dict):
            raise SimulationContextError("SIM_CONTEXT_MISSING")

        required_strings = (
            "test_run_id",
            "case_id",
            "scenario_id",
            "support_id",
            "bundle_digest",
            "execution_mode",
            "product",
            "version",
            "virtual_node_id",
            "node_ip",
            "container",
            "authority_scope",
        )
        if any(not isinstance(context.get(key), str) or not context[key].strip() for key in required_strings):
            raise SimulationContextError("SIM_CONTEXT_REQUIRED_FIELD_INVALID")
        if context["test_run_id"] != test_run_id or context["case_id"] != case_id:
            raise SimulationContextError("SIM_CONTEXT_BINDING_MISMATCH")
        if context["execution_mode"] != "sim-ssh" or context.get("simulation") is not True:
            raise SimulationContextError("SIM_CONTEXT_MODE_INVALID")
        if not _DIGEST_RE.fullmatch(context["bundle_digest"]):
            raise SimulationContextError("SIM_CONTEXT_DIGEST_INVALID")
        revision = context.get("kbd_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise SimulationContextError("SIM_CONTEXT_REVISION_INVALID")
        components = context.get("components")
        topology = context.get("topology")
        if not isinstance(components, list) or not components or not all(
            isinstance(value, str) and value.strip() for value in components
        ):
            raise SimulationContextError("SIM_CONTEXT_COMPONENTS_INVALID")
        if not isinstance(topology, list):
            raise SimulationContextError("SIM_CONTEXT_TOPOLOGY_INVALID")

        # Return a copy so no downstream code can mutate the decoded authority response.
        result = dict(context)
        result["components"] = list(components)
        result["topology"] = list(topology)
        logger.info(
            event="simulation_context_loaded",
            message="Authoritative hci-sim TestRun context loaded",
            test_run_id=test_run_id,
            case_id=case_id,
            support_id=context["support_id"],
            kbd_revision=revision,
        )
        return result
