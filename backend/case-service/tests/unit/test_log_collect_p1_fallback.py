"""日志收集失败兜底 P1：参数快照、失败产物补登记、{date} 格式统一、空包体检。

第一性原理：这四件事的共同点是「事后能不能自证」——
- 没有参数快照，重试就是在靠用户记忆重建一次采集；
- 失败任务不补登记产物，用户只剩「重新采集」这条唯一会丢日志的路；
- {date} 不按声明格式展开，猜错的那一半路径会被静默判假；
- 空包不细分码，排查者只能看到「未归因」。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from app.domain.log_collect_display import _default_reason, _normalize_quality
from app.schemas.log_collect import LogCollectTaskCreate
from app.services.log_collect_custom_scenario_service import expand_scenario_paths
from app.services.log_collect_date_placeholders import (
    expand_path_with_format,
    format_day,
    item_date_format,
    validate_item_placeholders,
)
from app.services.log_collect_evidence_service import artifact_reason_code
from app.services.log_collect_service import _build_task_detail, build_artifact_backfill_patch

pytestmark = pytest.mark.unit


class TestArtifactReasonCode:
    def test_each_empty_package_reason_has_its_own_code(self):
        # 「空包体检」的价值全在细分：三种原因给同一个码就等于没细分。
        assert artifact_reason_code("invalid", "too_small") == "LC-TRANSFER-013"
        assert artifact_reason_code("invalid", "content_too_small") == "LC-TRANSFER-013"
        assert artifact_reason_code("invalid", "empty_archive") == "LC-TRANSFER-012"
        assert artifact_reason_code("invalid", "metadata_only") == "LC-TRANSFER-012"
        assert artifact_reason_code("invalid", "unreadable") == "LC-TRANSFER-014"

    def test_reason_codes_never_overlap_between_empty_and_small(self):
        empty = artifact_reason_code("invalid", "empty_archive")
        small = artifact_reason_code("invalid", "too_small")
        unreadable = artifact_reason_code("invalid", "unreadable")
        assert len({empty, small, unreadable}) == 3

    def test_unknown_or_uninspected_has_no_code(self):
        # 没探测过就没有结论：默认码会把「未探测」说成「包坏了」。
        assert artifact_reason_code("unknown", "not_inspected") == ""
        assert artifact_reason_code("", "too_small") == ""

    def test_missing_hosts_maps_to_stale_artifact_code(self):
        assert artifact_reason_code("incomplete", "missing_hosts") == "LC-TRANSFER-011"


class TestArtifactQualityIncomplete:
    def test_incomplete_is_a_first_class_quality(self):
        # 修复前 incomplete 被归一化成 unknown，页面显示「尚未探测包内容」，
        # 与「缺少部分节点的产物」这条真实结论互相打脸。
        assert _normalize_quality("incomplete") == "incomplete"
        assert _default_reason("incomplete") == "missing_hosts"


class TestDatePlaceholders:
    def test_format_day_matches_declared_semantics(self):
        day = date(2026, 3, 7)
        assert format_day(day, "%Y-%m-%d") == "2026-03-07"
        assert format_day(day, "%Y%m%d") == "20260307"
        assert format_day(day, "%Y%m") == "202603"
        assert format_day(day, "%d") == "07"

    def test_unsupported_format_raises_instead_of_guessing(self):
        with pytest.raises(ValueError):
            format_day(date(2026, 3, 7), "%s")

    def test_expand_uses_per_item_format(self):
        # 现场同一份清单里两种格式并存：/sf/log/{date}.tar.gz 用 %d，
        # blackbox 用 %Y%m%d。旧实现两者都展开成 ISO 日期。
        items = [
            {"path": "/sf/log/{date}.tar.gz", "date_placeholders": [{"placeholder": "{date}", "format": "%d"}]},
            {
                "path": "/sf/log/blackbox/{date}.tar.gz",
                "date_placeholders": [{"placeholder": "{date}", "format": "%Y%m%d"}],
            },
            {"path": "/sf/log/log_new.db"},
        ]
        paths = expand_scenario_paths(items, date(2026, 11, 1), date(2026, 11, 2))
        assert paths == [
            "/sf/log/01.tar.gz",
            "/sf/log/02.tar.gz",
            "/sf/log/blackbox/20261101.tar.gz",
            "/sf/log/blackbox/20261102.tar.gz",
            "/sf/log/log_new.db",
        ]

    def test_missing_declaration_keeps_historical_iso_format(self):
        assert item_date_format({"path": "/sf/log/{date}.tar.gz"}) == "%Y-%m-%d"
        assert expand_path_with_format(
            "/sf/log/{date}.tar.gz", "%Y-%m-%d", date(2026, 11, 1), date(2026, 11, 2)
        ) == ["/sf/log/2026-11-01.tar.gz", "/sf/log/2026-11-02.tar.gz"]

    def test_unsupported_placeholder_is_rejected(self):
        problems = validate_item_placeholders(
            {"path": "/sf/log/{start}-{end}.tar.gz", "date_placeholders": [{"placeholder": "{start}"}]}
        )
        assert problems, "{start} 从未被实现，必须在保存阶段拒绝"

    def test_unsupported_format_is_rejected(self):
        problems = validate_item_placeholders(
            {
                "path": "/sf/log/{date}.tar.gz",
                "date_placeholders": [{"placeholder": "{date}", "format": "%c"}],
            }
        )
        assert problems

    def test_valid_declaration_passes(self):
        assert (
            validate_item_placeholders(
                {
                    "path": "/sf/log/{date}.tar.gz",
                    "date_placeholders": [{"placeholder": "{date}", "format": "%d"}],
                }
            )
            == []
        )

    def test_filter_pattern_validated_with_same_rules(self):
        # spc 的 exclude 过滤规则用 pattern 承载 {date}，同一口径必须一起校验。
        assert validate_item_placeholders(
            {"pattern": "/sf/log/{date}/vs/", "date_placeholders": [{"placeholder": "{date}", "format": "%d"}]}
        ) == []


class TestRequestSnapshot:
    def _payload(self, **overrides) -> LogCollectTaskCreate:
        data = {
            "case_id": "Q2026112300134",
            "origin_node_ip": "192.168.0.2",
            "modules": ["core"],
            "date_from": date(2026, 11, 1),
            "date_to": date(2026, 11, 2),
            "ips": ["192.168.0.11", "192.168.0.12"],
            "extra_paths": ["/sf/log/custom"],
            "remote_url": "http://192.168.0.2:5888/upload",
            "upload_mode": "acli_direct",
            "arch": "x86_64",
            "acli_version": "1.0.3",
        }
        data.update(overrides)
        return LogCollectTaskCreate(**data)

    def test_snapshot_carries_replayable_params(self):
        detail = _build_task_detail(self._payload(), upload_mode="acli_direct", config_sha256="a" * 64, path_count=3)
        request = detail["request"]
        assert request["modules"] == ["core"]
        assert request["date_from"] == "2026-11-01"
        assert request["ips"] == ["192.168.0.11", "192.168.0.12"]
        assert request["extra_paths"] == ["/sf/log/custom"]
        assert request["upload_mode"] == "acli_direct"
        assert request["acli_version"] == "1.0.3"
        assert len(request["sha256"]) == 64

    def test_snapshot_never_stores_credentials_or_url(self):
        request = _build_task_detail(self._payload(), upload_mode="acli_direct")["request"]
        assert request["remote_url_set"] is True
        # 只记录"是否设置"，URL 本体与票据一律不入库。
        assert "remote_url" not in request
        assert "p1_ticket" not in request

    def test_snapshot_digest_is_stable_and_changes_with_params(self):
        first = _build_task_detail(self._payload(), upload_mode="bridge_relay")["request"]
        second = _build_task_detail(self._payload(), upload_mode="bridge_relay")["request"]
        assert first["sha256"] == second["sha256"]
        changed = _build_task_detail(self._payload(modules=["other"]), upload_mode="bridge_relay")["request"]
        assert changed["sha256"] != first["sha256"]

    def test_ips_are_truncated_with_explicit_flag(self):
        ips = [f"192.168.1.{index}" for index in range(1, 251)]
        request = _build_task_detail(self._payload(ips=ips), upload_mode="bridge_relay")["request"]
        assert len(request["ips"]) == 200
        assert request["ips_count"] == 250
        assert request["ips_truncated"] is True


class TestFailedArtifactBackfill:
    def _task(self, **overrides) -> SimpleNamespace:
        data = {"status": "failed", "archive_path": None, "detail": {}, "task_id": "t-1"}
        data.update(overrides)
        return SimpleNamespace(**data)

    def _manifest(self, **overrides) -> dict:
        data = {
            "archive_path": "/sf/data/local/acli/tmp/data/log_collect/Q2026112300134.zip",
            "archive_size": 4096,
            "archive_sha256": "b" * 64,
            "quality": "ok",
            "reason": "ok",
            "source": "node_zip",
            "inspected_at": None,
        }
        data.update(overrides)
        return data

    def test_registers_location_without_touching_delivery(self):
        patch = build_artifact_backfill_patch(self._task(), self._manifest())
        assert patch is not None
        assert patch.archive_path == self._manifest()["archive_path"]
        assert patch.archive_size == 4096
        # 产物还在 ≠ 已交付：补登记绝不能顺手写交付终态。
        assert patch.delivery_status is None
        assert patch.status is None
        assert patch.detail["artifact_recovered"]["found"] is True

    @pytest.mark.parametrize("quality", ["invalid", "unknown", ""])
    def test_unusable_artifact_is_not_registered(self, quality):
        assert build_artifact_backfill_patch(self._task(), self._manifest(quality=quality)) is None

    def test_only_terminal_failure_states_are_backfilled(self):
        assert build_artifact_backfill_patch(self._task(status="completed"), self._manifest()) is None
        assert build_artifact_backfill_patch(self._task(status="collecting"), self._manifest()) is None
        assert build_artifact_backfill_patch(self._task(status="cancelled"), self._manifest()) is not None

    def test_existing_archive_path_is_never_overwritten(self):
        task = self._task(archive_path="/platform/object/key")
        assert build_artifact_backfill_patch(task, self._manifest()) is None

    def test_missing_path_or_size_is_skipped(self):
        assert build_artifact_backfill_patch(self._task(), self._manifest(archive_path="")) is None
        assert build_artifact_backfill_patch(self._task(), self._manifest(archive_size=0)) is None
