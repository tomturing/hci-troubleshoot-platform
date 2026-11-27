"""P1/P2 共用的 5888 落盘判据：只认 O(1) 落盘事实，不伪造字节级摘要。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.domain.log_collect_archive import judge_archive

NOW = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)


def task_with_created(created_at=NOW - timedelta(minutes=5)):
    return SimpleNamespace(created_at=created_at)


def test_verified_when_found_and_size_is_enough():
    ok, reason, size, archived_at = judge_archive(
        task_with_created(),
        {"found": True, "size": 100, "mtime": (NOW - timedelta(minutes=1)).timestamp()},
        90,
    )
    assert (ok, reason, size) == (True, "verified", 100)
    assert archived_at is not None


def test_missing_archive_is_rejected():
    assert judge_archive(task_with_created(), {"found": False}, 90)[:2] == (
        False,
        "archive_missing",
    )


def test_truncated_archive_is_rejected():
    ok, reason, _, _ = judge_archive(task_with_created(), {"found": True, "size": 50}, 90)
    assert (ok, reason) == (False, "size_mismatch")


def test_historical_same_name_archive_is_rejected():
    ok, reason, _, _ = judge_archive(
        task_with_created(),
        {"found": True, "size": 100, "mtime": (NOW - timedelta(days=3)).timestamp()},
        90,
    )
    assert (ok, reason) == (False, "stale_archive")


def test_previous_archive_smaller_than_record_is_stale_not_truncated():
    """Q2026111700614 回归：新包（4.95 GB）上传中，规范名仍挂 14:31 的旧包（314 MB）。

    旧包同样"小于记录值"，旧顺序先查体积会把它误报成 size_mismatch 红卡；
    时间判定必须先于体积判定——历史包说明的是"新包还没发布"，走等待口径。
    """
    ok, reason, size, archived_at = judge_archive(
        task_with_created(),
        {"found": True, "size": 314_820_302, "mtime": (NOW - timedelta(hours=3)).timestamp()},
        4_952_749_247,
    )
    assert (ok, reason, size) == (False, "stale_archive", 314_820_302)
    assert archived_at is not None


def test_fresh_archive_smaller_than_record_is_truncation():
    """mtime 落在任务窗口内却体积不足：确定性截断，不能被等待口径洗白。"""
    ok, reason, _, _ = judge_archive(
        task_with_created(),
        {"found": True, "size": 50, "mtime": (NOW - timedelta(minutes=1)).timestamp()},
        90,
    )
    assert (ok, reason) == (False, "size_mismatch")


def test_untrusted_expected_size_is_not_truncation():
    """Q2026112700769：同工单换模块重收，固定路径上残留上一批次的旧包（4.17 GiB），
    新包只有 146 MiB。期望值来自采集中的观测、无法证明属于本次任务时，
    体积偏小只能待核对，不能判成截断红卡。"""
    ok, reason, size, _ = judge_archive(
        task_with_created(),
        {"found": True, "size": 152_850_599, "mtime": (NOW - timedelta(minutes=1)).timestamp()},
        4_478_420_847,
        expected_size_trustworthy=False,
    )
    assert (ok, reason, size) == (False, "archive_size_untrusted", 152_850_599)
