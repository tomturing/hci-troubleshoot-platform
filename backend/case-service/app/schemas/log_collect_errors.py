"""日志收集错误码（Python 侧）。

单一真源：``backend/shared/resolution/catalogs/log_collect_error_catalog.json``。
本模块只读取与格式化，不重复定义码值；Go 侧 ``log_collect_errors.go`` 通过
单测与本目录强一致，任何一侧漂移都会让 CI 失败。

HTTP 输出采用 RFC 7807 子集（``type/title/status/code/layer/owner/retryable``），
与业界 Problem Details 规范一致，前端可直接按 ``detail.code`` 做分层展示。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_PATH = (
    Path(__file__).resolve().parents[3] / "shared" / "resolution" / "catalogs" / "log_collect_error_catalog.json"
)


@lru_cache(maxsize=1)
def load_error_catalog() -> dict[str, dict[str, Any]]:
    """返回 code → 元数据映射（进程内缓存）。"""
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {str(item["code"]): item for item in raw["errors"]}


def normalize_retryable(value: Any) -> str:
    """把 JSON 中的 bool/str 统一成 true/false/manual。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_problem(code: str, detail: str, trace_id: str | None = None) -> dict[str, Any]:
    """构造 Problem Details 结构；未知码降级为通用 500，不阻断响应。"""
    meta = load_error_catalog().get(code)
    if meta is None:
        return {
            "type": "urn:hci:log-collect:error:unknown",
            "title": "未知错误",
            "status": 500,
            "code": code or "LC-PLATFORM-006",
            "layer": "PLATFORM",
            "owner": "platform",
            "retryable": "true",
            "detail": detail,
            "trace_id": trace_id,
        }
    return {
        "type": f"urn:hci:log-collect:error:{code}",
        "title": meta["title_zh"],
        "status": int(meta["http_status"]),
        "code": code,
        "layer": meta["layer"],
        "owner": meta["owner"],
        "retryable": normalize_retryable(meta["retryable"]),
        "detail": detail,
        "trace_id": trace_id,
    }


def error_status(code: str, fallback: int = 500) -> int:
    meta = load_error_catalog().get(code)
    return int(meta["http_status"]) if meta else fallback
