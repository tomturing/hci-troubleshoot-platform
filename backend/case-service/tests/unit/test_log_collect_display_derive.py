"""三维展示派生（phase/conclusion/conditions）与写入收口的行为测试。

覆盖两个真实事故形态，避免以后有人"顺手"把结论改回用 status 推断：
    Q2026111201130  采集"完成"但零产物、从未交付 → failed + artifact_missing
    Q2026111200624  608 字节空壳包 + 交付成功     → partial（不得绿色成功）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.domain.log_collect_display import (
    assess_artifact,
    derive,
    derive_phase,
    snapshot_artifact_evidence,
)
from app.models.log_collect import LogCollectTask
from app.repositories.log_collect_repo import LogCollectTaskRepository
from app.schemas.log_collect import LogCollectTaskPatch
from app.services.log_collect_service import LogCollectService

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def make_task(**overrides) -> dict:
    data = {
        "status": "completed",
        "progress": 100,
        "upload_mode": "bridge_relay",
        "delivery_status": "none",
        "relay_status": "none",
        "archive_size": None,
        "object_size": None,
        "detail": {},
    }
    data.update(overrides)
    return data


def key_list(payload: dict) -> list[str]:
    return [item["key"] for item in payload["display"]["conditions"]]


class TestConclusion:
    def test_zero_artifact_never_delivered_is_failed(self):
        """1130：不能因为 status=completed 就显示「已交付」或「进行中」。"""
        payload = derive(make_task(), now=NOW)

        assert payload["display"]["conclusion"] == "failed"
        assert payload["display"]["phase"] == "settled"
        assert key_list(payload) == ["artifact_missing", "handoff_pending"]
        assert payload["artifact"]["quality"] == "invalid"
        assert payload["artifact"]["reason"] == "missing"
        assert payload["artifact"]["size_text"] == "—"

    def test_delivered_shell_package_is_partial_not_success(self):
        """0624：608 字节空壳包即使 delivery+relay 都成功，也只能是「有疑点」。"""
        payload = derive(
            make_task(delivery_status="delivered", relay_status="success", archive_size=608, object_size=608),
            now=NOW,
        )

        assert payload["display"]["conclusion"] == "partial"
        assert payload["display"]["conclusion_tone"] != "success"
        assert key_list(payload) == ["artifact_missing"]
        assert payload["artifact"]["size_text"] == "608 B"
        assert payload["artifact"]["reason"] == "too_small"

    def test_success_requires_content_evidence(self):
        payload = derive(
            make_task(delivery_status="delivered", relay_status="success", archive_size=2 * 1024**3),
            manifest={"quality": "ok", "reason": "ok", "entry_count": 982},
            now=NOW,
        )

        assert payload["display"]["conclusion"] == "success"
        assert key_list(payload) == ["artifact_verified"]
        assert payload["artifact"]["entry_count"] == 982

    def test_delivered_without_platform_evidence_is_partial(self):
        """只有 acli 自报、平台没回查过 → 不能宣称成功。"""
        payload = derive(
            make_task(
                delivery_status="delivered",
                relay_status="none",
                archive_size=2 * 1024**3,
                detail={"artifact_quality": "ok", "artifact_reason": "ok"},
            ),
            now=NOW,
        )

        assert payload["display"]["conclusion"] == "partial"
        assert key_list(payload) == ["artifact_verified", "delivered_unverified"]

    def test_failed_and_cancelled_short_circuit(self):
        assert derive(make_task(status="failed"), now=NOW)["display"]["conclusion"] == "failed"
        assert derive(make_task(status="cancelled"), now=NOW)["display"]["conclusion"] == "cancelled"

    def test_delivered_is_settled_even_if_node_never_finished(self):
        """节点卡在 uploading 但平台已收妥：不能永远算「传输中」。"""
        payload = derive(
            make_task(
                status="uploading",
                progress=80,
                delivery_status="delivered",
                relay_status="success",
                archive_size=2 * 1024**3,
            ),
            now=NOW,
        )

        assert payload["display"]["phase"] == "settled"
        assert payload["display"]["conclusion"] != "pending"

    @pytest.mark.parametrize(
        ("delivery_status", "expected_phase"),
        [
            ("none", "settled"),
            ("pulling", "fetching"),
            ("uploading", "uploading"),
            ("relaying", "relaying"),
        ],
    )
    def test_delivery_phase_wins_over_settled_status(self, delivery_status, expected_phase):
        task = make_task(status="completed", delivery_status=delivery_status)
        assert derive_phase(task) == expected_phase


class TestArtifactEvidence:
    def test_collecting_does_not_infer_missing_artifact(self):
        """采集途中还没有包是正常现象，不能提前报「未取到有效日志」。"""
        payload = derive(make_task(status="collecting", progress=40, archive_size=None), now=NOW)

        assert payload["artifact"]["quality"] == "unknown"
        assert key_list(payload) == []
        assert payload["display"]["conclusion"] == "pending"

    def test_p1_deleted_package_is_not_failed(self):
        """P1 直传成功后 acli 自删节点包：体积缺失不是失败证据。"""
        payload = derive(
            make_task(
                upload_mode="acli_direct",
                delivery_status="delivered",
                detail={"p1_archive_check": {"verified": True}},
            ),
            now=NOW,
        )

        assert payload["artifact"]["quality"] == "unknown"
        assert payload["artifact"]["source"] == "deleted_after_delivery"
        assert payload["display"]["conclusion"] == "partial"
        assert key_list(payload) == ["artifact_uninspected"]

    def test_manifest_beats_recorded_snapshot(self):
        quality, reason, source = assess_artifact(
            make_task(detail={"artifact_quality": "invalid", "artifact_reason": "missing"}),
            {"quality": "ok", "reason": "ok"},
        )

        assert (quality, reason, source) == ("ok", "ok", "manifest")

    def test_snapshot_only_writes_for_settled_tasks(self):
        assert snapshot_artifact_evidence(make_task(status="collecting", archive_size=None)) is None

        snapshot = snapshot_artifact_evidence(make_task(archive_size=608))

        assert snapshot is not None
        assert snapshot["artifact_quality"] == "invalid"
        assert snapshot["artifact_evidence"] == "size_inference"

    def test_snapshot_never_overrides_probe_result(self):
        task = make_task(detail={"artifact_quality": "ok", "artifact_reason": "ok"}, archive_size=None)
        assert snapshot_artifact_evidence(task) is None


class TestTransferAndNetwork:
    def test_composite_progress_uses_bands(self):
        metric = {
            "stage": "uploading",
            "bytes_done": 512 * 1024 * 1024,
            "bytes_total": 1024 * 1024 * 1024,
            "rate_bps": 8 * 1024 * 1024,
        }
        payload = derive(
            make_task(status="completed", delivery_status="uploading", archive_size=1024**3),
            metrics=metric,
            now=NOW,
        )

        assert payload["display"]["progress_percent"] == 75
        assert payload["transfer"]["rate_text"] == "8.00 MiB/s"
        assert payload["transfer"]["stage_label"] == "上传"

    def test_delivery_without_metrics_reports_unknown_progress(self):
        """老桥接端不上报时必须留空（界面显示 —），不许拿 0/100 顶替。"""
        payload = derive(
            make_task(status="completed", delivery_status="uploading", archive_size=1024**3),
            now=NOW,
        )

        assert payload["transfer"] is None
        assert payload["display"]["progress_percent"] is None

    def test_network_grading_only_uses_relevant_target(self):
        probes = [
            {"target": "log_server", "grade": "poor", "reachable": False, "created_at": NOW},
            {"target": "platform_data", "grade": "good", "reachable": True, "created_at": NOW},
        ]

        p2 = derive(make_task(status="collecting", progress=40), probes=probes, now=NOW)
        assert p2["network"]["grade"] == "good"
        assert "network_degraded" not in key_list(p2)

        p1 = derive(
            make_task(status="collecting", progress=40, upload_mode="acli_direct"),
            probes=probes,
            now=NOW,
        )
        assert p1["network"]["grade"] == "poor"
        assert "network_degraded" in key_list(p1)

    def test_stalled_transfer_surfaces_condition(self):
        metric = {"stage": "relaying", "stalled": True, "created_at": NOW - timedelta(seconds=180)}
        payload = derive(make_task(delivery_status="relaying", archive_size=1024**3), metrics=metric, now=NOW)

        assert "stalled" in key_list(payload)
        assert payload["transfer"]["stalled_seconds"] == 180


@pytest.fixture
def service() -> LogCollectService:
    return LogCollectService(MagicMock(spec=LogCollectTaskRepository))


def _mock_task(**overrides) -> MagicMock:
    task = MagicMock(spec=LogCollectTask)
    task.status = "pending"
    task.progress = 0
    task.detail = {}
    task.delivery_status = "none"
    task.relay_status = "none"
    task.upload_mode = "bridge_relay"
    task.archive_size = None
    task.object_size = None
    task.error_code = None
    task.error_message = None
    task.trace_id = "trace-test"
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


class TestWriteSide:
    async def test_node_observed_drives_status_and_keeps_audit(self, service):
        task = _mock_task()
        service.repository.get_by_id = AsyncMock(return_value=task)
        service.repository.flush = AsyncMock()
        service.repository.refresh = AsyncMock()

        result = await service.update_task(
            "11111111-1111-1111-1111-111111111111",
            LogCollectTaskPatch(node_observed={"status": "collecting", "progress": 25}),
        )

        assert result.status == "collecting"
        assert result.progress == 25
        assert result.detail["write_audit"]["node_observed_keys"] == ["progress", "status"]

    async def test_client_cannot_forge_artifact_evidence(self, service):
        """detail 是证据容器：客户端带 artifact_quality 必须被丢弃并留痕。"""
        task = _mock_task()
        service.repository.get_by_id = AsyncMock(return_value=task)
        service.repository.flush = AsyncMock()
        service.repository.refresh = AsyncMock()

        result = await service.update_task(
            "11111111-1111-1111-1111-111111111111",
            LogCollectTaskPatch(
                node_observed={
                    "status": "collecting",
                    "detail": {"artifact_quality": "ok", "note": "现场观察"},
                }
            ),
        )

        assert "artifact_quality" not in result.detail
        assert result.detail["note"] == "现场观察"
        assert "detail.artifact_quality" in result.detail["write_audit"]["ignored"]

    async def test_legacy_status_patch_is_audited(self, service):
        """老前端仍可直接写 status/progress，但必须留下「这是兼容入参」的痕迹。"""
        task = _mock_task()
        service.repository.get_by_id = AsyncMock(return_value=task)
        service.repository.flush = AsyncMock()
        service.repository.refresh = AsyncMock()

        result = await service.update_task(
            "11111111-1111-1111-1111-111111111111", LogCollectTaskPatch(status="collecting")
        )

        assert result.status == "collecting"
        assert result.detail["write_audit"]["legacy"] == {"status": "collecting"}
