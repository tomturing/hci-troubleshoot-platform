"""
QKV 前端数据解析与过滤器
负责将 acli 返回的 JSON 数据清洗提取为精简的 Value 键值列表

支持两种提取模式：
1. 动态模式（produces 非空）：按 produces 规格提取字段，name=变量名, path=JSON字段路径
2. 兜底模式（produces 为空）：按 query_type 硬编码提取标准字段集
"""

from __future__ import annotations

import json
from typing import Any

from app.tools.qkv.signal import FrontendQueryType


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

    # 1. 弹框/日志（dialog）：直接按行拆分，提取文本
    if query_type == FrontendQueryType.DIALOG:
        lines = stdout_text.splitlines()
        return [{"line": line.strip(), "description": line.strip()} for line in lines if line.strip()]

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
            extracted["end"] = item.get("end") or item.get("start") or ""
            extracted["target"] = item.get("target") or item.get("object_name") or ""
            extracted["type"] = item.get("type") or ""
            extracted["description"] = item.get("description") or ""
            extracted["host"] = item.get("host") or item.get("hostname") or item.get("hostid") or ""
            extracted["vm"] = item.get("vm") or item.get("object_id") if item.get("object_type") == "虚拟机" else item.get("vm", "")

        elif query_type == FrontendQueryType.TASK:
            # 任务提取标准：status, type, end, host, vm, target, description, errcode_tracing, request_id
            extracted["status"] = item.get("status") or item.get("process") or ""
            extracted["type"] = item.get("type") or item.get("alert_type") or ""
            extracted["end"] = item.get("end") or item.get("start") or ""
            extracted["host"] = item.get("host") or item.get("hostname") or item.get("hostid") or ""
            extracted["vm"] = item.get("vm") or ""
            extracted["target"] = item.get("target") or item.get("object_name") or ""
            extracted["description"] = item.get("description") or ""
            extracted["errcode_tracing"] = item.get("errcode_tracing") or ""
            extracted["request_id"] = item.get("request_id") or ""

        # 过滤掉全空的结果，保留包含有效信息的元素
        if any(extracted.values()):
            results.append(extracted)

    return results
