"""日志收集场景的节点能力判据。

第一性原理：一个场景能不能选，只取决于**目标节点 acli 的 log_collect help 是否列出它**。
平台白名单只回答「acli 可能接受哪些名字」，不回答「这台机器支持哪些专项」。
把两者混为一谈，就会出现「界面可勾选、下发必失败」的选项。

对抗性审查：
- acli 未安装 / help 解析失败时没有能力事实，此时不能假装知道，返回 None 让调用方降级；
- 旧 bridge 不返回 node_modules，同样按「未知」处理，不误伤存量；
- 只有 catalog_source=acli_help 且 node_modules 非空才算权威能力快照。
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from typing import Any


def supported_modules_from_probe(probe: Mapping[str, Any] | None) -> frozenset[str] | None:
    """从探测快照提取节点真实支持的场景；无法判定时返回 None。

    兼容三种输入：
    - payload.probe（前端回传的探测快照）；
    - bridge_execution_logs.extra（JSONB，驱动通常已反序列化为 dict）；
    - 某些驱动把 JSONB 当字符串返回。
    """
    if not isinstance(probe, Mapping):
        return None
    if str(probe.get("catalog_source") or "").strip() != "acli_help":
        return None
    raw = probe.get("node_modules")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, (list, tuple)):
        return None
    supported = {
        str(item).strip()
        for item in raw
        if isinstance(item, (str, int)) and str(item).strip()
    }
    return frozenset(supported) if supported else None


def unsupported_modules(
    requested: Iterable[str],
    supported: Collection[str] | None,
) -> list[str]:
    """返回请求中节点不支持的场景，保持用户选择顺序。

    supported=None 表示能力未知：不拦截，交由 bridge 的最终校验兜底，
    避免「探测降级」被误判成「节点不支持」而让整个面板不可用。
    """
    if supported is None:
        return []
    return [str(item) for item in requested if str(item) not in supported]


def capability_rejection_message(
    unsupported: Iterable[str],
    acli_version: str | None = None,
) -> str:
    """给出可执行的拒绝文案：明确是哪几个场景、为什么、下一步做什么。"""
    names = "、".join(str(item) for item in unsupported)
    version = f" acli {acli_version}" if acli_version else ""
    return (
        f"当前节点{version}不支持所选场景：{names}。"
        "请取消这些场景，或升级节点 acli 后重新探测再试。"
    )
