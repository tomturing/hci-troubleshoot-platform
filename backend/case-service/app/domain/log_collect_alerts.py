"""采集告警抽取：从节点日志里找出「交付成功但产物可能不完整」的证据。

第一性原理：
    P2 的 sha256 只证明「平台副本 == 节点源包」，不证明「源包内容有效」。
    acli 打包时的 I/O 错误会在源包内部留下坏块，摘要却一致——这类缺陷
    只能从 acli 自己的运行日志里发现。本模块把该日志转成结构化告警，
    让平台在「已交付」之外还能说清「采集时有损失」。

对抗性审查：
    - 只认采集插件日志（collection_log.log）：acli.log 里大量 SF_DCODE
      ERROR 是命令非零退出的正常噪音，绝不能上屏淹没客户；
    - 只认 `failed add ... to tar` 这一类实质失败；get_gpuinfo exit 1、
      HCI not update、tls_relaxed 等全部忽略；
    - EIO 与 not exists 分开：前者 tarfile.add 已写 header 但 copyfileobj
      中途失败，会写坏 tar 流；后者只是跳过文件，结构仍完整；
    - 正文有界（单文件 2 MiB 已是平台上限）、证据行截断，防止 detail 膨胀；
    - 幂等：同 source 的旧告警由本次重算替换，重采干净后告警自然消失。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

# 只有采集插件自己的日志能产生采集告警；acli.log / cmd_output 不算。
COLLECTION_LOG_SUFFIX = "/plugins/log_collect/collection_log.log"

MAX_ALERTS = 20
MAX_EVIDENCE_LINE = 500

# `failed add <path> to tar, error: <reason>` 是 single_collect.py:265 的统一文案。
_FAILED_ADD_RE = re.compile(
    r"failed add\s+(?P<path>\S+)\s+to tar,\s*error:\s*(?P<reason>.+)$",
    re.IGNORECASE,
)
# EIO：tarfile.add 已写 header，copyfileobj 中途失败 → tar 流被写坏。
_IO_RE = re.compile(r"Errno\s+5|Input/output error", re.IGNORECASE)
# 失效链接/已移除文件：add 之前就抛错，header 未落盘 → tar 流不受影响。
_MISSING_RE = re.compile(r"not exists|No such file or directory", re.IGNORECASE)

CODE_IO = "LC-COLLECT-001"
CODE_PARTIAL = "LC-COLLECT-002"


def is_collect_alert_source(path: object) -> bool:
    """进程日志路径是否是采集插件日志（唯一可信来源）。"""
    return str(path or "").endswith(COLLECTION_LOG_SUFFIX)


def extract_collect_alerts(
    path: str,
    content: str,
    *,
    collected_at: str | None = None,
    trace_id: str = "",
    node_ip: str = "",
) -> list[dict[str, Any]]:
    """从一段采集日志正文抽取告警。无告警返回空列表（调用方据此清理旧快照）。

    ``content`` 是已解码的 UTF-8 文本；bridge 用 tail -c 取尾部，截断日志里的
    错误行通常仍在窗口内，因此不做「必须从头解析」的假设。
    """
    if not is_collect_alert_source(path) or not content:
        return []
    at = collected_at or datetime.now(UTC).isoformat()
    alerts: list[dict[str, Any]] = []
    for raw in str(content).splitlines():
        match = _FAILED_ADD_RE.search(raw)
        if not match:
            continue
        reason = match.group("reason")
        if _IO_RE.search(reason):
            code = CODE_IO
        elif _MISSING_RE.search(reason):
            code = CODE_PARTIAL
        else:
            continue
        alerts.append(
            {
                "code": code,
                "source": str(path),
                "node_ip": node_ip,
                "file": match.group("path"),
                "line": raw.strip()[:MAX_EVIDENCE_LINE],
                "collected_at": at,
                "trace_id": trace_id,
            }
        )
    return alerts


def merge_collect_alerts(
    existing: object,
    fresh: list[dict[str, Any]],
    *,
    source: str,
) -> list[dict[str, Any]]:
    """合并告警快照：同 source 由本次重算替换，跨 source 保留，去重且有界。"""
    old = [item for item in (existing if isinstance(existing, list) else []) if isinstance(item, dict)]
    kept = [item for item in old if item.get("source") != source]
    seen: set[tuple[object, object, object]] = set()
    merged: list[dict[str, Any]] = []
    for item in kept + fresh:
        key = (item.get("code"), item.get("source"), item.get("file"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[-MAX_ALERTS:]


def primary_collect_alert(detail: object) -> dict[str, Any] | None:
    """从 detail 里取一个最严重的告警供客户侧展示（EIO 优先于文件缺失）。"""
    alerts = detail.get("collect_alerts") if isinstance(detail, dict) else None
    if not isinstance(alerts, list):
        return None
    ranked = sorted(
        (item for item in alerts if isinstance(item, dict) and item.get("code")),
        key=lambda item: 0 if item.get("code") == CODE_IO else 1,
    )
    return ranked[0] if ranked else None
