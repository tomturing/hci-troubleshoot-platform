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
        # 终态未交付不能再合成 100%，否则管理端又是「已完成 100% + 交付未开始」同屏。
        assert payload["display"]["progress_percent"] is None
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
        # 已交付终态仍按分段给 100：进度表示链路走完，不代表产物合格。
        assert payload["display"]["progress_percent"] == 100
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

    def test_queued_delivery_is_unfinished_not_failed(self):
        """01229：包在节点上、交付已登记但没拉走 → 「未完成·待补交付」。

        判 failed 会与「交付已登记、可点上传到平台重试」同屏，用户只能理解成
        "这次白采了"，于是去重新收集——覆盖掉节点上那份 454MB 的包。
        """
        payload = derive(
            make_task(delivery_status="queued", archive_size=475_693_225),
            now=NOW,
        )

        assert payload["display"]["conclusion"] == "unfinished"
        # 语气必须是警告而不是错误：这是可续传的中间态，不是终态失败
        assert payload["display"]["conclusion_tone"] == "warning"
        assert payload["display"]["conclusion_label"] == "未完成 · 待补交付"
        # 结论里的"可补交付"必须与 handoff_pending 条件一致，不能各说各话
        assert "handoff_pending" in key_list(payload)

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


class TestScopeComponentAttribution:
    """三段五组件 + S1-S8 + 首个异常点：用户「一眼定界」的可回归验收。"""

    def waiting_task(self, minutes_ago: int = 0, **overrides) -> dict:
        checked_at = (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
        data = {
            "status": "completed",
            "upload_mode": "acli_direct",
            "delivery_status": "none",
            "relay_status": "none",
            "trace_id": "trace-Q2026111300155",
            "detail": {
                "p1_archive_check": {
                    "verified": False,
                    "reason": "archive_missing",
                    "checked_at": checked_at,
                }
            },
        }
        data.update(overrides)
        return make_task(**data)

    def test_archive_verify_window_is_pending_not_failed(self):
        """上线首错：31 秒归档命名竞态不是失败，结论必须是「核验中」。"""
        display = derive(self.waiting_task(), now=NOW)["display"]

        assert display["phase"] == "verifying"
        assert display["phase_label"] == "核验中"
        assert display["conclusion"] == "pending"
        assert display["progress_percent"] is None
        assert key_list({"display": display}) == ["verifying_archive"]
        assert display["fault_attribution"]["code"] == "LC-HTP-001"
        assert display["fault_attribution"]["customer_text"] == (
            "htp平台：归档核验未完成，正在等待日志服务器发布归档名；"
            "请等待最多10分钟或点「重新核验归档」，不要重新收集"
        )
        assert display["trace_id"] == "trace-Q2026111300155"
        assert next(s for s in display["stages"] if s["key"] == "S6")["status"] == "waiting"

    def test_archive_verify_window_expires_to_honest_terminal(self):
        """等满 10 分钟仍查不到才落终态，且归因到日志服务器而非采集端。"""
        payload = derive(self.waiting_task(minutes_ago=11), now=NOW)
        display = payload["display"]

        assert display["phase"] == "settled"
        assert display["conclusion"] == "failed"
        assert display["fault_attribution"]["code"] == "LC-5888-004"
        # P1 平台不留副本、节点包自删：体积缺失不能被写成「未取到有效日志」。
        assert "artifact_missing" not in key_list(payload)
        assert "handoff_pending" in key_list(payload)

    def test_delivered_flips_waiting_to_delivered_conclusion(self):
        """核验通过后同一任务必须自动刷新结论，不能停在等待态。"""
        assert derive(self.waiting_task(), now=NOW)["display"]["conclusion"] == "pending"

        result = derive(
            self.waiting_task(
                delivery_status="delivered",
                detail={"p1_archive_check": {"verified": True, "reason": "verified"}},
            ),
            now=NOW,
        )["display"]

        assert result["phase"] == "settled"
        assert result["conclusion"] == "partial"
        assert result["fault_attribution"]["code"] == "LC-BRIDGE-005"
        assert next(s for s in result["stages"] if s["key"] == "S7")["status"] == "failed"

    def test_no_checked_at_never_hangs_in_waiting(self):
        """对抗：缺时间戳不得无限等待，否则任务会被永久挂起。"""
        payload = derive(
            self.waiting_task(detail={"p1_archive_check": {"verified": False, "reason": "archive_missing"}}),
            now=NOW,
        )
        assert payload["display"]["phase"] != "verifying"

    def test_stages_are_three_scopes_and_five_components(self):
        stages = derive(make_task(), now=NOW)["display"]["stages"]

        assert [stage["key"] for stage in stages] == [f"S{i}" for i in range(1, 9)]
        assert stages[4]["scope_label"] == "内网 → 云端"
        assert stages[6]["scope_label"] == "本地 → 云端"
        components = {c["key"]: c for stage in stages for c in stage["components"]}
        assert set(components) == {"hci", "acli", "bridge", "htp", "log_server"}
        assert components["hci"]["tag"] == "[内网·HCI环境]"
        assert components["log_server"]["scope_label"] == "云端"

    def test_user_supplied_error_texts_render_exactly(self):
        hci = derive(make_task(error_message="SSH连接失败"), now=NOW)["display"]
        assert hci["fault_attribution"]["customer_text"] == (
            "HCI环境：SSH连接失败；请尝试其他终端连接HCI验证连接是否正常"
        )

        acli = derive(make_task(error_message="未知的命令或者命名空间(log collect create)"), now=NOW)["display"]
        assert acli["fault_attribution"]["customer_text"] == (
            "acli工具：未知的命令或者命名空间(log collect create)；"
            "请在HCI后台执行acli --version确认acli版本"
        )

    def test_specific_error_beats_generic_component_name(self):
        display = derive(
            make_task(error_message="terminal_bridge 证据上报失败：x509: certificate signed by unknown authority"),
            now=NOW,
        )["display"]
        assert display["fault_attribution"]["code"] == "LC-BRIDGE-002"
        assert display["fault_attribution"]["stage"] == "S7"

    def test_response_model_accepts_new_fields(self):
        from app.schemas.log_collect_display import build_display_payload

        payload = build_display_payload(self.waiting_task(), now=NOW)
        assert payload.display.fault_attribution is not None
        assert payload.display.stages[5].status == "waiting"
        assert payload.display.trace_id == "trace-Q2026111300155"
