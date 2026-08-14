"""QFK 命令输出的确定性提取器（兼容模块）。

所有纯文本提取逻辑已移至 ``shared.signals.extractor``。
本模块保留 ``get_complete_output()``（Redis 缓存恢复，执行基础设施）
并重新导出共享符号以保持向后兼容。
"""

from __future__ import annotations

from typing import Any, Literal

from shared.signals.extractor import (  # noqa: F401 — 重新导出以保持向后兼容
    DEFAULT_OUTPUT_MAX_BYTES,
    HARD_OUTPUT_MAX_BYTES,
    ExtractionResult,
    QFKExtractionError,
    _LineRef,
    extract_output_values,
    extract_value,
)

from app.tools.acli.executor import ExecResult

__all__ = [
    "DEFAULT_OUTPUT_MAX_BYTES",
    "HARD_OUTPUT_MAX_BYTES",
    "ExtractionResult",
    "QFKExtractionError",
    "extract_output_values",
    "extract_value",
    "get_complete_output",
]


async def get_complete_output(
    result: ExecResult,
    redis: Any,
    *,
    source: Literal["stdout", "stderr"] = "stdout",
    max_bytes: int = DEFAULT_OUTPUT_MAX_BYTES,
) -> str:
    """返回完整 stdout/stderr；被截断时从 Redis 缓存读取。

    ``cmd_cache:{exec_id}`` 是历史 stdout 缓存键；stderr 使用独立键，避免改变既有
    SOP JSONPath 提取的缓存格式。缓存过期、不可用或数据超限都明确报错，绝不退化到
    截断摘要继续提取。
    """

    if source not in {"stdout", "stderr"}:
        raise QFKExtractionError("QFK_EXTRACT_INVALID_SPEC", f"不支持的输出来源: {source}")
    if max_bytes < 1 or max_bytes > HARD_OUTPUT_MAX_BYTES:
        raise QFKExtractionError(
            "QFK_EXTRACT_INVALID_SPEC",
            f"输出大小上限必须在 1 到 {HARD_OUTPUT_MAX_BYTES} 字节之间",
        )

    truncated = result.truncated if source == "stdout" else result.stderr_truncated
    output = result.stdout if source == "stdout" else result.stderr
    if truncated:
        if not result.exec_id:
            raise QFKExtractionError(
                "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE",
                f"{source} 已截断但执行结果缺少 exec_id",
            )
        client = getattr(redis, "client", None)
        if client is None:
            raise QFKExtractionError(
                "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE",
                "完整输出缓存未连接",
            )
        cache_prefix = "cmd_cache" if source == "stdout" else "cmd_stderr_cache"
        try:
            cached = await client.get(f"{cache_prefix}:{result.exec_id}")
        except Exception as exc:
            raise QFKExtractionError(
                "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE",
                f"读取完整 {source} 缓存失败",
            ) from exc
        if cached is None:
            raise QFKExtractionError(
                "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE",
                f"{source} 已截断且完整输出缓存不存在或已过期",
            )
        output = cached.decode("utf-8") if isinstance(cached, bytes) else str(cached)

    if len(output.encode("utf-8")) > max_bytes:
        raise QFKExtractionError(
            "QFK_OUTPUT_TOO_LARGE",
            f"{source} 超过允许的 {max_bytes} 字节，需改用专用采集器缩小结果集",
        )
    return output
