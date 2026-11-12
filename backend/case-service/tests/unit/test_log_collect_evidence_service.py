"""证据数据面：票据、产物质量推导、网络分级与写入契约测试。

第一性原理：客户端只能上报事实。本组测试专门验证「客户端声称成功」
不会污染平台结论（608 字节空壳包必须被推导为 invalid）。
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from app.domain.log_collect_display_generated import MIN_ARCHIVE_BYTES
from app.routes import log_collect
from app.routes import log_collect_evidence as routes
from app.services.log_collect_evidence_service import (
    LogCollectEvidenceTicket,
    derive_manifest_quality,
    probe_grade,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

TASK_ID = "11111111-1111-1111-1111-111111111111"
PROBE_ID = "22222222-2222-2222-2222-222222222222"


class TestTicket:
    def test_roundtrip_and_binding(self):
        ticket = LogCollectEvidenceTicket().issue(TASK_ID)
        assert LogCollectEvidenceTicket().verify(TASK_ID, ticket)
        assert not LogCollectEvidenceTicket().verify(PROBE_ID, ticket)

    def test_malformed_and_expired_are_rejected(self):
        assert not LogCollectEvidenceTicket().verify(TASK_ID, "garbage")
        with patch("app.services.log_collect_evidence_service.time.time", return_value=1.0):
            expired = LogCollectEvidenceTicket().issue(TASK_ID)
        assert not LogCollectEvidenceTicket().verify(TASK_ID, expired)

    def test_probe_ticket_has_shorter_lifetime(self):
        service = LogCollectEvidenceTicket()
        task_ttl = int(service.issue(TASK_ID).split(".")[0])
        probe_ttl = int(service.issue(PROBE_ID, probe=True).split(".")[0])
        assert task_ttl > probe_ttl


class TestManifestQuality:
    @pytest.mark.parametrize(
        ("size", "entries", "entry_bytes", "readable", "quality", "reason"),
        [
            (608, 2, 1024, True, "invalid", "too_small"),
            (None, 0, 0, True, "invalid", "missing"),
            (2 * 1024**3, 0, 0, True, "invalid", "empty_archive"),
            (2 * 1024**3, 2, 1024, True, "invalid", "metadata_only"),
            (2 * 1024**3, 20, 1024, True, "suspect", "content_too_small"),
            (2 * 1024**3, 2, 2 * 1024**3, True, "suspect", "entry_count_low"),
            (2 * 1024**3, 982, 9 * 1024**3, True, "ok", "ok"),
            (2 * 1024**3, 982, 9 * 1024**3, False, "invalid", "unreadable"),
        ],
    )
    def test_matrix(self, size, entries, entry_bytes, readable, quality, reason):
        assert derive_manifest_quality(
            archive_size=size,
            entry_count=entries,
            entry_total_bytes=entry_bytes,
            readable=readable,
        ) == (quality, reason)

    def test_threshold_comes_from_contract(self):
        assert (
            derive_manifest_quality(
                archive_size=MIN_ARCHIVE_BYTES - 1,
                entry_count=100,
                entry_total_bytes=MIN_ARCHIVE_BYTES,
                readable=True,
            )[0]
            == "invalid"
        )


class TestProbeGrade:
    @pytest.mark.parametrize(
        ("kwargs", "grade"),
        [
            ({"reachable": False, "loss_rate": 0, "rtt_avg_ms": 10, "jitter_ms": 1}, "poor"),
            ({"reachable": True, "loss_rate": 0.05, "rtt_avg_ms": 10, "jitter_ms": 1}, "poor"),
            ({"reachable": True, "loss_rate": 0, "rtt_avg_ms": 250, "jitter_ms": 1}, "poor"),
            ({"reachable": True, "loss_rate": 0, "rtt_avg_ms": 100, "jitter_ms": 1}, "fair"),
            ({"reachable": True, "loss_rate": 0, "rtt_avg_ms": 10, "jitter_ms": 100}, "fair"),
            ({"reachable": True, "loss_rate": 0, "rtt_avg_ms": 10, "jitter_ms": 1}, "good"),
            ({"reachable": True, "loss_rate": None, "rtt_avg_ms": None, "jitter_ms": None}, "unknown"),
        ],
    )
    def test_grade_is_derived_from_facts(self, kwargs, grade):
        assert probe_grade(**kwargs) == grade


class FakeResult:
    def __init__(self, rows=None, first=None, scalar=None):
        self._rows = rows or []
        self._first = first
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._first

    def scalar(self):
        return self._scalar


class FakeSession:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, sql, params):
        text = str(sql)
        self.calls.append((text, params))
        if "FROM log_collect_task WHERE task_id" in text and "SELECT" in text:
            return FakeResult(rows=[{"task_id": TASK_ID, "case_id": "Q2026111200624", "trace_id": "tr-1"}])
        if "RETURNING" in text:
            return FakeResult(first=("id-1",))
        if "SUM(size)" in text:
            return FakeResult(scalar=0)
        return FakeResult()

    async def commit(self):
        return None


@pytest.fixture
def client():
    session = FakeSession()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[log_collect.get_log_collect_service] = lambda: type(
        "FakeService", (), {"repository": type("Repo", (), {"session": session})()}
    )()
    with TestClient(app) as test_client:
        yield test_client, session
    app.dependency_overrides.clear()


class TestEvidenceRoutes:
    def test_invalid_ticket_is_rejected_before_db(self, client):
        test_client, session = client
        response = test_client.post(
            f"/api/direct/log-collect/tasks/{TASK_ID}/metrics",
            json={"seq": 1, "stage": "uploading", "bytes_done": 10},
            headers={"X-Evidence-Ticket": "bad.token"},
        )
        assert response.status_code == 401
        assert session.calls == []

    def test_client_cannot_claim_good_quality_for_shell_package(self, client):
        """608 字节空壳包：即使上报方声称 ok，平台也必须推导 invalid。"""
        test_client, session = client
        response = test_client.post(
            f"/api/direct/log-collect/tasks/{TASK_ID}/artifact-manifest",
            json={
                "source": "node_zip",
                "archive_size": 608,
                "entry_count": 2,
                "entry_total_bytes": 1024,
                "quality": "ok",
                "conclusion": "success",
            },
            headers={"X-Evidence-Ticket": routes.tickets.issue(TASK_ID)},
        )

        assert response.status_code == 200
        inserts = [params for sql, params in session.calls if "INSERT INTO log_collect_artifact_manifest" in sql]
        assert inserts and inserts[0]["quality"] == "invalid"
        assert inserts[0]["reason"] == "too_small"
        detail_updates = [params for sql, params in session.calls if "artifact_quality" in sql]
        assert detail_updates and detail_updates[0]["quality"] == "invalid"

    def test_process_log_rejects_path_outside_whitelist(self, client):
        test_client, _ = client
        response = test_client.post(
            f"/api/direct/log-collect/tasks/{TASK_ID}/process-logs",
            json={"host": "node-1", "path": "/tmp/evil.log", "size": 10},
            headers={"X-Evidence-Ticket": routes.tickets.issue(TASK_ID)},
        )
        assert response.status_code == 400

    def test_process_log_accepts_gzipped_whitelisted_path(self, client):
        test_client, session = client
        import base64

        payload = base64.b64encode(gzip.compress(b"collect ok")).decode()
        response = test_client.post(
            f"/api/direct/log-collect/tasks/{TASK_ID}/process-logs",
            json={
                "host": "node-1",
                "path": f"{routes.PROCESS_LOG_DIR}run.log",
                "size": 10,
                "content_gz_b64": payload,
                "collected_at": datetime.now(UTC).isoformat(),
            },
            headers={"X-Evidence-Ticket": routes.tickets.issue(TASK_ID)},
        )
        assert response.status_code == 200
        assert any("INSERT INTO log_collect_process_log" in sql for sql, _ in session.calls)

    def test_bandwidth_endpoints_require_probe_ticket(self, client):
        test_client, _ = client
        assert (
            test_client.post(
                f"/api/direct/log-collect/network-probe/{PROBE_ID}/uplink",
                content=b"0123456789",
            ).status_code
            == 401
        )
        assert test_client.get(f"/api/direct/log-collect/network-probe/{PROBE_ID}/downlink?bytes=64").status_code == 401

        ticket = routes.tickets.issue(PROBE_ID, probe=True)
        headers = {"X-Probe-Ticket": ticket}
        up = test_client.post(
            f"/api/direct/log-collect/network-probe/{PROBE_ID}/uplink",
            content=b"0123456789",
            headers=headers,
        )
        down = test_client.get(
            f"/api/direct/log-collect/network-probe/{PROBE_ID}/downlink?bytes=64",
            headers=headers,
        )
        assert up.json()["bytes"] == 10
        assert len(down.content) == 64

    def test_uplink_rejects_oversized_payload(self, client):
        test_client, _ = client
        response = test_client.post(
            f"/api/direct/log-collect/network-probe/{PROBE_ID}/uplink",
            content=b"x" * (routes.MAX_BANDWIDTH_BYTES + 1),
            headers={"X-Probe-Ticket": routes.tickets.issue(PROBE_ID, probe=True)},
        )
        assert response.status_code == 413
