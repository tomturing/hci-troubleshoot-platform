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
