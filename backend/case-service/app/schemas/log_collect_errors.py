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

from shared.observability.logger import get_logger

logger = get_logger("case-service")

_CATALOG_RELATIVE = Path("shared") / "resolution" / "catalogs" / "log_collect_error_catalog.json"


def _resolve_catalog_path() -> Path:
    """兼容两种目录布局：仓库内 `backend/` 与镜像内 `/app/`。

    仓库布局：`backend/case-service/app/schemas/` → 向上第 3 层是 `backend/`；
    镜像布局：`/app/app/schemas/` → 向上第 3 层是 `/`（错误）。固定层数必然在
    其中一种布局下失效，这里统一逐级向上查找。
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / _CATALOG_RELATIVE
        if candidate.is_file():
            return candidate
    # 都没找到时返回仓库布局路径，仅用于错误信息/测试
    return here.parents[3] / _CATALOG_RELATIVE


CATALOG_PATH = _resolve_catalog_path()


@lru_cache(maxsize=1)
def load_error_catalog() -> dict[str, dict[str, Any]]:
    """返回 code → 元数据映射（进程内缓存）。

    目录文件缺失/损坏时不抛异常：错误码目录只是展示元数据，缺失应降级为通用
    Problem（`build_problem` 已有 unknown 分支），否则 409/4xx 会变成 500。
    staging 曾因镜像内路径少一层 `/app` 触发此问题。
    """
    try:
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("错误码目录不可用，降级为通用错误结构: %s (%s)", CATALOG_PATH, exc)
        return {}
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
