"""
QKV 前端数据解析与过滤器
负责将 acli 返回的 JSON 数据清洗提取为精简的 Value 键值列表
"""

from __future__ import annotations

import json
from typing import Any

from app.tools.qkv.signal import FrontendQueryType


def parse_frontend_value(query_type: FrontendQueryType, stdout_text: str) -> list[dict[str, Any]]:
    """
    根据前端信号类型解析 stdout 文本，提取所需的 Value 结构

    Args:
        query_type: 前端信号查询类型
        stdout_text: 底层 acli 命令标准输出文本

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
    items = []
    if isinstance(raw_data, dict):
        items = raw_data.get("data") or raw_data.get("items") or []
        if not items and not isinstance(items, list):
            # 若不是列表结构，尝试整体作为单个对象转换
            items = [raw_data]
    elif isinstance(raw_data, list):
        items = raw_data
    else:
        return []

    results = []
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
