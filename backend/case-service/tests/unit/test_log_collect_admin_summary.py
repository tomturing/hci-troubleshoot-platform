"""管理端统计恒等式与证据解析测试。

事故形态：统计条 168，分桶 5+82+5+54=146，前端拿两套 SQL 口径自己拼。
现在所有分桶复用同一个结论函数，并由模型校验器强制
``total == Σ by_conclusion``；本测试同时验证派生结论没有被 SQL 层重新解释。
"""

from __future__ import annotations

import json

import pytest
from app.routes.log_collect_admin_tasks import _normalize_row, _summary_of
from app.schemas.log_collect_admin import AdminPathSummary


def row(**overrides) -> dict:
    base = {
        "upload_mode": "bridge_relay",
        "status": "completed",
        "progress": 100,
        "archive_size": None,
        "object_size": None,
        "delivery_status": "none",
        "relay_status": "none",
        "archive_path": None,
        "detail": {},
        "archive_check": None,
    }
    base.update(overrides)
    return base


class TestSummaryIdentity:
    def test_total_equals_bucket_sum(self):
        """5 种结论各一条：总数必须恰好等于五桶之和。"""
        rows = [
            row(
                delivery_status="delivered",
                relay_status="success",
                archive_size=2 * 1024**3,
                detail={"artifact_quality": "ok", "artifact_reason": "ok"},
            ),
            row(delivery_status="delivered", relay_status="success", archive_size=608),
            row(),  # 1130：零产物且未交付
            row(status="cancelled"),
            row(status="collecting", progress=40),
        ]

        summary = _summary_of(rows)

        assert summary.total == 5
        assert sum(summary.by_conclusion.values()) == summary.total
        assert summary.by_conclusion == {
            "pending": 1,
            "success": 1,
            "partial": 1,
            "unfinished": 0,
            "failed": 1,
            "cancelled": 1,
        }
        assert summary.transferring == summary.by_conclusion["pending"] == 1
        assert summary.failed == summary.by_conclusion["failed"]
        assert sum(summary.p1.by_conclusion.values()) == summary.p1.total
        assert sum(summary.p2.by_conclusion.values()) == summary.p2.total

    def test_p1_p2_groups_partition_the_total(self):
        rows = [
            row(
                upload_mode="acli_direct", delivery_status="delivered", detail={"p1_archive_check": {"verified": True}}
            ),
            row(upload_mode="bridge_relay", status="failed"),
            row(upload_mode="none", status="collecting"),
        ]

        summary = _summary_of(rows)

        assert summary.total == 3
        assert summary.p1.total + summary.p2.total == 2  # none 路径不进 P1/P2 分组
        assert summary.by_conclusion["partial"] == 1
        assert summary.by_conclusion["failed"] == 1
        assert summary.by_conclusion["pending"] == 1

    def test_validator_rejects_broken_identity(self):
        with pytest.raises(ValueError, match="统计恒等式被破坏"):
            AdminPathSummary(total=3, by_conclusion={"pending": 1, "success": 1})

    def test_empty_result_is_valid(self):
        summary = _summary_of([])
        assert summary.total == 0
        assert sum(summary.by_conclusion.values()) == 0


class TestRowNormalization:
    def test_jsonb_string_is_parsed_not_dropped(self):
        """驱动把 JSONB 返回成字符串时，证据不能被静默吞掉。"""
        item = _normalize_row(
            row(
                detail=json.dumps({"artifact_quality": "invalid", "artifact_reason": "missing"}),
                archive_check=json.dumps({"verified": True}),
            )
        )

        assert item["detail"]["artifact_quality"] == "invalid"
        assert item["archive_check"] == {"verified": True}

    def test_invalid_json_falls_back_to_empty_evidence(self):
        item = _normalize_row(row(detail="{not-json", archive_check=None))
        assert item["detail"] == {}
        assert item["archive_check"] is None
