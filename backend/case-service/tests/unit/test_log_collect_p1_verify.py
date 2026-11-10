"""P1 归档核验的写入契约（2026-11-10 Q2026111000243）。

现场事实：工单 Q2026111000243 走 P1 直传，压缩包确实落到了日志服务器（/sf/log
下能读到），平台任务行却是 ``delivery_status=failed``，界面据此显示「交付失败」。

根因在写入契约，不在探测本身：``acli_direct`` 的 failed 只有 verify-archive 一个写入者，
而它把一次**瞬时**观测（采集刚结束时上传往往才刚开始，5888 也还没发布正式文件名）
直接当成终态失败写库，此后没有任何写入者会纠正它。

本文件锁定三条不变量：
1. 只有核验通过才写 delivered；
2. 否定结论不写交付终态、不写失败码，只留核验快照；
3. 已 delivered 的记录不会被一次否定观测降级。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.routes.log_collect import _judge_p1_archive, _p1_verify_patch


def _task(created_at: datetime | None = None, delivery_status: str = "none"):
    return SimpleNamespace(
        created_at=created_at or datetime.now(UTC) - timedelta(minutes=30),
        delivery_status=delivery_status,
    )


class TestJudge:
    """判据：存在 + 体积不小于记录值 + 不是历史遗留。"""

    def test_missing_archive_is_not_a_failure_conclusion(self):
        verified, reason, size, archived_at = _judge_p1_archive(_task(), {"found": False}, 100)
        assert (verified, reason, size, archived_at) == (False, "archive_missing", None, None)

    def test_truncated_archive_is_rejected(self):
        """截断只会让体积变小，所以"比记录值小"必须拦住。"""
        verified, reason, size, _ = _judge_p1_archive(
            _task(), {"found": True, "size": 80, "mtime": datetime.now(UTC).timestamp()}, 100
        )
        assert (verified, reason, size) == (False, "size_mismatch", 80)

    def test_archived_size_not_smaller_than_record_is_accepted(self):
        """期望值是平台观测到的峰值，可能停在 zip 收尾前一刻：更大的包不该被判失败。"""
        verified, reason, size, _ = _judge_p1_archive(
            _task(), {"found": True, "size": 120, "mtime": datetime.now(UTC).timestamp()}, 100
        )
        assert (verified, reason, size) == (True, "verified", 120)

    def test_unknown_expected_size_only_needs_non_empty_archive(self):
        verified, _, size, _ = _judge_p1_archive(
            _task(), {"found": True, "size": 120, "mtime": datetime.now(UTC).timestamp()}, None
        )
        assert (verified, size) == (True, 120)

    def test_archive_older_than_task_is_stale(self):
        created = datetime.now(UTC) - timedelta(minutes=5)
        stale = (created - timedelta(hours=6)).timestamp()
        verified, reason, _, _ = _judge_p1_archive(_task(created), {"found": True, "size": 100, "mtime": stale}, 100)
        assert (verified, reason) == (False, "stale_archive")

    def test_zero_size_archive_is_invalid(self):
        verified, reason, _, _ = _judge_p1_archive(_task(), {"found": True, "size": 0}, None)
        assert (verified, reason) == (False, "archive_size_invalid")


class TestVerifyPatch:
    """只有强证据成立才允许写交付终态。"""

    def test_verified_writes_delivered(self):
        checked_at = datetime.now(UTC)
        patch = _p1_verify_patch(
            _task(),
            verified=True,
            check={"verified": True, "reason": "verified"},
            checked_at=checked_at,
        )
        assert patch.delivery_status == "delivered"
        assert patch.delivery_finished_at == checked_at
        assert patch.detail["delivery_evidence"] == "archive_verified"

    def test_negative_does_not_write_terminal_state(self):
        """否定观测只落快照：写 failed 会把"还在传"永久钉成"交付失败"。"""
        patch = _p1_verify_patch(
            _task(),
            verified=False,
            check={"verified": False, "reason": "archive_missing"},
            checked_at=datetime.now(UTC),
        )
        assert patch.delivery_status is None
        assert patch.delivery_error is None
        assert patch.delivery_finished_at is None
        assert patch.error_code is None
        assert patch.detail["p1_archive_check"]["reason"] == "archive_missing"
        assert patch.detail["delivery_evidence"] == "archive_unverified"

    def test_negative_never_downgrades_delivered(self):
        patch = _p1_verify_patch(
            _task(delivery_status="delivered"),
            verified=False,
            check={"verified": False, "reason": "archive_missing"},
            checked_at=datetime.now(UTC),
        )
        assert patch.delivery_status is None
        # 交付事实不可逆：连"未核验"的证据标注都不该覆盖掉已有的核验结论
        assert "delivery_evidence" not in patch.detail
        assert patch.detail["p1_archive_check"]["reason"] == "archive_missing"

    def test_patch_round_trip_drops_none_fields(self):
        """服务端按 exclude_none 应用 patch：没写的字段必须真的不出现在写入集里。"""
        patch = _p1_verify_patch(
            _task(),
            verified=False,
            check={"verified": False, "reason": "size_mismatch"},
            checked_at=datetime.now(UTC),
        )
        written = patch.model_dump(exclude_none=True)
        assert "delivery_status" not in written
        assert "delivery_finished_at" not in written
