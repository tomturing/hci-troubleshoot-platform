"""5888 归档巡检判定的回归测试。

历史上脚本把 datetime 类当成模块使用 datetime.UTC，只有样本同时带
mtime 与 created_at 才会抛 AttributeError，导致真实对账中途失败。
这里直接测纯函数 verdict_of，覆盖判定分支而不依赖网络。
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit" / "audit_log_collect_5888.py"
_SPEC = importlib.util.spec_from_file_location("audit_log_collect_5888", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

_BASE = datetime(2026, 11, 26, 2, 0, tzinfo=UTC)


def _item(*, expected_size: int = 100, created_at: str | None = None) -> dict[str, object]:
    return {
        "created_at": created_at or _BASE.isoformat().replace("+00:00", "Z"),
        "expected_size": expected_size,
    }


def _archive(*, size: int = 1000, mtime: datetime | None = None, found: bool = True) -> dict[str, object]:
    return {
        "found": found,
        "size": size,
        "mtime": int((mtime or _BASE).timestamp()),
    }


def test_verdict_with_mtime_returns_verified() -> None:
    """带 mtime 的归档必须走完判定，而不是抛 AttributeError。"""
    assert _MODULE.verdict_of(_item(), _archive()) == "verified"


def test_stale_archive_is_rejected() -> None:
    """早于任务创建时间 5 分钟以上的包不能当成本轮归档。"""
    old = _BASE - timedelta(hours=1)
    assert _MODULE.verdict_of(_item(), _archive(mtime=old)) == "stale_archive"


def test_naive_created_at_is_treated_as_utc() -> None:
    """无时区的 created_at 按 UTC 解释，避免与归档 mtime 错比。"""
    naive = _BASE.replace(tzinfo=None).isoformat()
    assert _MODULE.verdict_of(_item(created_at=naive), _archive()) == "verified"


def test_size_mismatch_is_rejected() -> None:
    """时间对得上但体积小于记录值，按截断包处理。"""
    assert _MODULE.verdict_of(_item(expected_size=2000), _archive()) == "size_mismatch"


def test_missing_archive() -> None:
    """5888 明确没有归档或体积非法时返回 missing。"""
    assert _MODULE.verdict_of(_item(), {"found": False, "size": None, "mtime": None}) == "missing"
    assert _MODULE.verdict_of(_item(), _archive(size=0)) == "missing"
