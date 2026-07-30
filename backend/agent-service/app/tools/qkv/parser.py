"""
QKV 前端数据解析与过滤器
负责将 acli 返回的 JSON 数据清洗提取为精简的 Value 键值列表

支持两种提取模式：
1. 动态模式（produces 非空）：按 produces 规格提取字段，name=变量名, path=JSON字段路径
2. 兜底模式（produces 为空）：按 query_type 硬编码提取标准字段集
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.tools.qkv.signal import FrontendQueryType

# ─── request_id 正则模式 ───────────────────────────────────────────────────────
# 匹配格式如: request_id: ,a5ed4ad9340ce338ba1ac71d13ffcfb9
_REQUEST_ID_PATTERN = re.compile(
    r"request[_-]?id\s*(?::|=)\s*,?([a-f0-9]{32})",
    re.IGNORECASE,
)
# 匹配格式如: trace":"a5ed4ad9340ce338ba1ac71d13ffcfb9:...
_TRACE_ID_PATTERN = re.compile(
    r"(?:trace|trace[_-]id)['\"]?\s*(?::|=)\s*['\"]?([a-f0-9]{32})",
    re.IGNORECASE,
)

# ─── 日志时间正则模式 ───────────────────────────────────────────────────────
# 格式1: [2026-07-21 22:09:43]
_LOG_TIME_BRACKET = re.compile(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]")
# 格式2: 2026-07-21 10:08:47.454040
_LOG_TIME_DOT = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\.\d+")
# 格式3: 2026/07/21 10:10:45
_LOG_TIME_SLASH = re.compile(r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})")
# 格式4: 2026-07-21 10:10（少数弹框关联日志只精确到分钟）
_LOG_TIME_MINUTE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?!:\d{2})")


def _convert_timestamp(time_str: str | None) -> str:
    """将时间字符串或时间戳转换为标准格式 "YYYY-MM-DD HH:MM:SS"。

    Args:
        time_str: 时间字符串或时间戳，支持格式：
            - "2026-07-20 18:45:22" (保持原样)
            - "2026/07/20 18:45:22" (转换为标准格式)
            - Unix 时间戳字符串

    Returns:
        标准格式时间字符串 "YYYY-MM-DD HH:MM:SS"
    """
    if not time_str:
        return ""

    time_str = str(time_str).strip()

    # 已经是标准格式，直接返回
    if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", time_str):
        return time_str

    # 尝试解析 "2026-07-20 18:45:22" 格式
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass

    # 尝试解析 "2026/07/20 18:45:22" 格式
    try:
        dt = datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass

    # 尝试解析 Unix 时间戳
    try:
        ts = int(time_str)
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass

    # 解析失败，返回原始字符串
    return time_str


def _extract_time_from_log_line(line: str) -> str:
    """从日志行中提取时间并转换为标准格式 "YYYY-MM-DD HH:MM:SS"。

    支持格式：
    - [2026-07-21 22:09:43]
    - 2026-07-21 10:08:47.454040
    - 2026/07/21 10:10:45

    Args:
        line: 日志行

    Returns:
        标准格式时间字符串，如果未找到返回空字符串
    """
    # 尝试匹配 [YYYY-MM-DD HH:MM:SS]
    match = _LOG_TIME_BRACKET.search(line)
    if match:
        return _convert_timestamp(match.group(1))

    # 尝试匹配 YYYY-MM-DD HH:MM:SS.ffffff
    match = _LOG_TIME_DOT.search(line)
    if match:
        return _convert_timestamp(match.group(1))

    # 尝试匹配 YYYY/MM/DD HH:MM:SS
    match = _LOG_TIME_SLASH.search(line)
    if match:
        return _convert_timestamp(match.group(1))

    match = _LOG_TIME_MINUTE.search(line)
    if match:
        return f"{match.group(1)}:00"

    return ""


def _extract_from_dialog_log(stdout_text: str) -> list[dict[str, Any]]:
    """从日志文本中提取 request_id、end 等信息。

    日志格式示例：
    /sf/log/21/sfvt_vtplogd.log:... request_id: ,a5ed4ad9340ce338ba1ac71d13ffcfb9, ...

    Args:
        stdout_text: acli log get 命令的输出文本

    Returns:
        包含 request_id、end、line 的 dict 列表
    """
    results: list[dict[str, Any]] = []
    seen_request_ids: set[str] = set()

    lines = [line for line in stdout_text.splitlines() if line.strip()]

    def nearest_time(index: int) -> str:
        """从命中行及其 aCLI context 邻行中确定事件 END，禁止使用当前系统时间猜测。"""

        for distance in (0, 1, 2):
            candidates = (index - distance, index + distance) if distance else (index,)
            for candidate in candidates:
                if 0 <= candidate < len(lines):
                    value = _extract_time_from_log_line(lines[candidate])
                    if value:
                        return value
        return ""

    for index, line in enumerate(lines):
        if not line.strip():
            continue

        # 提取日志时间作为 end 字段
        end_ts = nearest_time(index)

        # 尝试匹配 request_id
        match = _REQUEST_ID_PATTERN.search(line)
        if match:
            request_id = match.group(1)
            if request_id not in seen_request_ids:
                seen_request_ids.add(request_id)
                results.append({
                    "request_id": request_id,
                    "end": end_ts,
                    "line": line.strip(),
                })
                continue

        # 尝试匹配 trace id（备选）
        match = _TRACE_ID_PATTERN.search(line)
        if match:
            trace_id = match.group(1)
            if trace_id not in seen_request_ids:
                seen_request_ids.add(trace_id)
                results.append({
                    "request_id": trace_id,
                    "end": end_ts,
                    "line": line.strip(),
                })

    # 同一弹框文本可能重复出现；按 END 降序让当前/最近一次事件优先，避免依赖
    # aCLI 的文件遍历顺序。仍返回全部候选，由 QKV limit 统一截断并在 Evidence 展示歧义。
    if results:
        results.sort(key=lambda item: str(item.get("end") or ""), reverse=True)
        return results

    # 如果没有找到 request_id，返回原始行（带时间戳）。
    return [
        {
            "line": line.strip(),
            "end": _extract_time_from_log_line(line),
            "description": line.strip(),
        }
        for line in lines
    ]


def parse_frontend_value(
    query_type: FrontendQueryType,
    stdout_text: str,
    produces: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """
    根据前端信号类型解析 stdout 文本，提取所需的 Value 结构

    Args:
        query_type: 前端信号查询类型
        stdout_text: 底层 acli 命令标准输出文本
        produces: 产出变量规格列表 [{name, path}, ...]；
                  非空时按规格动态提取，为空时走硬编码兜底

    Returns:
        包含解析提取后关键数据的 dict 列表
    """
    if not stdout_text.strip():
        return []

    # 1. 弹框/日志（dialog）：从日志文本中提取 request_id
    if query_type == FrontendQueryType.DIALOG:
        return _extract_from_dialog_log(stdout_text)

    # 2. 告警（alert）与任务（task）：反序列化 JSON 进行精细抽取
    try:
        raw_data = json.loads(stdout_text)
    except json.JSONDecodeError:
        # 容错：如果执行结果不是合法 JSON，直接按行以文本包裹形式返回，避免系统崩溃
        return [{"raw_text": line.strip()} for line in stdout_text.splitlines() if line.strip()]

    # acli 命令返回值数组通常放在 "data" 键中，若没有，尝试直接作为 root 列表处理
    items: list[Any] = []
    if isinstance(raw_data, dict):
        items = raw_data.get("data") or raw_data.get("items") or []
        if not items and not isinstance(items, list):
            # 若不是列表结构，尝试整体作为单个对象转换
            items = [raw_data]
    elif isinstance(raw_data, list):
        items = raw_data
    else:
        return []

    # 选择提取模式：produces 非空走动态，否则走硬编码兜底
    if produces:
        return _extract_by_produces(items, produces)
    return _extract_hardcoded(items, query_type)


def _extract_by_produces(items: list[Any], produces: list[dict[str, str]]) -> list[dict[str, Any]]:
    """按 produces 规格动态提取字段：name=变量名(输出key), path=JSON字段路径(输入key)。

    支持多路径容错：path 可为 "host|hostname|hostid" 形式，按 | 分隔依次尝试。
    """
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        extracted: dict[str, Any] = {}
        for spec in produces:
            name = spec.get("name", "")
            if not name:
                continue
            path = spec.get("path", name)
            # 支持 | 分隔的多路径容错（如 "host|hostname|hostid"）
            candidates = path.split("|") if isinstance(path, str) else [path]
            val: Any = None
            for cand in candidates:
                val = item.get(cand.strip()) if isinstance(cand, str) else None
                if val:
                    break
            extracted[name.lower()] = val if val is not None else ""
        if any(extracted.values()):
            results.append(extracted)
    return results


def _extract_hardcoded(items: list[Any], query_type: FrontendQueryType) -> list[dict[str, Any]]:
    """硬编码兜底：按 query_type 提取标准字段集（兼容无 produces 的旧信号）。"""
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        extracted: dict[str, Any] = {}

        if query_type == FrontendQueryType.ALERT:
            # 告警提取标准：alert_type, end, target, type, description, host, vm
            # 支持对 hostname / hostid 等容错兼容映射
            extracted["alert_type"] = item.get("alert_type") or item.get("type") or ""
            # end 字段转换为 Unix 时间戳
            end_raw = item.get("end") or item.get("start") or ""
            extracted["end"] = _convert_timestamp(end_raw)
            extracted["target"] = item.get("target") or item.get("object_name") or ""
            extracted["type"] = item.get("type") or ""
            extracted["description"] = item.get("description") or ""
            extracted["host"] = item.get("host") or item.get("hostname") or item.get("hostid") or ""
            extracted["vm"] = item.get("vm") or item.get("object_id") if item.get("object_type") == "虚拟机" else item.get("vm", "")

        elif query_type == FrontendQueryType.TASK:
            # 任务提取标准：type, end, target, description, host, vm, errcode_tracing, request_id
            extracted["type"] = item.get("type") or item.get("alert_type") or ""
            # end 字段转换为 Unix 时间戳
            end_raw = item.get("end") or item.get("start") or ""
            extracted["end"] = _convert_timestamp(end_raw)
            extracted["target"] = item.get("target") or item.get("object_name") or ""
            extracted["description"] = item.get("description") or ""
            extracted["host"] = item.get("host") or item.get("hostname") or item.get("hostid") or ""
            extracted["vm"] = item.get("vm") or ""
            extracted["errcode_tracing"] = item.get("errcode_tracing") or ""
            extracted["request_id"] = item.get("request_id") or ""
            # status 字段保留（额外）
            extracted["status"] = item.get("status") or item.get("process") or ""

        # 过滤掉全空的结果，保留包含有效信息的元素
        if any(extracted.values()):
            results.append(extracted)

    return results
