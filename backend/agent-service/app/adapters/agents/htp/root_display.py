"""text 根因信号的展示层：只吃纯数据，不认识 ``KBD`` 模型。

text 根因信号没有 ``acquire``、不参与任何现场取证与判定，只把「结论侧特征关键词」
交给运维人工核对。把它放在本文件而不是 ``kbd_differential`` 里的原因：渲染逻辑属于
根因链路，一旦它住在关键链路文件里，淘汰根因链路就必须改动关键链路代码，
违背「两条链路可分别安全下线」的约束。

入参刻意不做成 ``KBD``：共享内核的 ``KBD`` 不得承载任何根因字段（否则淘汰根因
链路要改共享内核）。锚点由 kb-service 装配、经编排层原样透传到这里，本模块只负责
确定性渲染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ANCHOR_HEADER = "根因特征（请人工核对）"


@dataclass(frozen=True)
class AnchorEntry:
    """一篇 KBD 的 text 根因锚点（纯展示数据，与 KBD 模型解耦）。

    :param label: 稳定标识（support_id 优先，回退 kbd_id/id）
    :param name: KBD 标题
    :param anchors: ``[{"signal_text": str, "keywords": [str, ...]}, ...]``
    """

    label: str
    name: str
    anchors: tuple[dict[str, Any], ...] = ()


def build_anchor_entries(raw_cases: list[dict[str, Any]] | None) -> list[AnchorEntry]:
    """从 kb-service 的候选载荷中提取 text 锚点。

    ``root_display_anchors`` 与 ``root_executable`` 是两条独立的展示/执行通道：
    只有 text 根因信号的 KBD 通常不可执行（``root_executable=False``），但它的
    「人工核对特征」依然必须出现在报告里，因此这里只按锚点是否存在筛选，
    绝不要求候选可执行。
    """
    entries: list[AnchorEntry] = []
    for case in raw_cases or []:
        anchors = tuple(case.get("root_display_anchors") or ())
        if not anchors:
            continue
        entries.append(
            AnchorEntry(
                label=str(case.get("support_id") or case.get("kbd_id") or case.get("id") or ""),
                name=str(case.get("name") or case.get("title") or ""),
                anchors=anchors,
            )
        )
    return entries


def format_root_display_anchors(
    anchors: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    indent: str = "",
) -> str:
    """渲染一组 text 根因锚点；无有效锚点返回空串（对既有报告零影响）。"""
    lines: list[str] = []
    for anchor in anchors or ():
        text = str(anchor.get("signal_text") or "").strip()
        keywords = [str(item).strip() for item in (anchor.get("keywords") or []) if str(item).strip()]
        if not text and not keywords:
            continue
        suffix = f"（关键词：{'、'.join(keywords)}）" if keywords else ""
        lines.append(f"{indent}- {text or '（无描述）'}{suffix}")
    if not lines:
        return ""
    header = f"\n\n{indent}**{ANCHOR_HEADER}**：" if indent == "" else f"{indent}**{ANCHOR_HEADER}**："
    return header + "\n" + "\n".join(lines)


def append_anchors_to_report(report: str, entries: list[AnchorEntry]) -> str:
    """把候选的 text 锚点追加到报告末尾（由编排层统一调用）。"""
    blocks: list[str] = []
    for entry in entries:
        rendered = format_root_display_anchors(entry.anchors, indent="- ")
        if not rendered:
            continue
        label = entry.label or entry.name or "未知案例"
        blocks.append(f"- 案例 {label}（{entry.name}）{rendered}")
    if not blocks:
        return report
    return report + f"\n\n**{ANCHOR_HEADER}**：\n" + "\n".join(blocks)
