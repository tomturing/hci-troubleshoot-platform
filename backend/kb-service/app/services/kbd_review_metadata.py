"""KBD 专家审核的结构化监督元数据。

Revision payload 已经是知识内容的唯一事实源；本模块只为字段级差异补充“为什么改”。
不保存第二份 before/after，消费者必须通过 parent_revision_id + payload_json 重算 Diff，
从而避免训练数据出现两套可漂移的知识正文。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

REASON_CODES = frozenset(
    {
        "source_missed",
        "screenshot_misread",
        "fact_inference_confused",
        "wrong_category",
        "wrong_capability",
        "invalid_argument",
        "missing_signal",
        "redundant_signal",
        "unsafe_command",
        "unsupported_semantics",
        "threshold_or_match_error",
        "wording_only",
        "other_expert_correction",
    }
)


def _diff_revision_payloads(before: Any, after: Any) -> list[dict[str, Any]]:
    """延迟导入避免 revision 服务与审核元数据形成模块初始化环。"""

    from app.services.kbd_revision_service import diff_revision_payloads

    return diff_revision_payloads(before, after)


def normalize_change_annotations(raw: Any) -> list[dict[str, str]]:
    """校验前端提交的最小标注，不允许自由原因码污染后续评估维度。"""

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("change_annotations 必须是数组")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"change_annotations[{index}] 必须是对象")
        reason_code = str(item.get("reason_code") or "").strip()
        if reason_code not in REASON_CODES:
            raise ValueError(f"change_annotations[{index}].reason_code 不在受控原因码范围内")
        path = str(item.get("path") or "").strip()
        signal_id = str(item.get("signal_id") or "").strip()
        if not path and not signal_id:
            raise ValueError(f"change_annotations[{index}] 必须指定 path 或 signal_id")
        if path and not path.startswith("/"):
            raise ValueError(f"change_annotations[{index}].path 必须是 JSON Pointer")
        note = str(item.get("note") or "").strip()
        if len(note) > 500:
            raise ValueError(f"change_annotations[{index}].note 不能超过 500 个字符")
        normalized.append(
            {
                **({"path": path} if path else {}),
                **({"signal_id": signal_id} if signal_id else {}),
                "reason_code": reason_code,
                **({"note": note} if note else {}),
            }
        )
    return normalized


def _inferred_reason(change: dict[str, Any]) -> str:
    """没有额外打断专家时，基于结构差异给出保守的可统计默认原因。"""

    path = str(change.get("path") or "")
    operation = str(change.get("operation") or "")
    if path.startswith("/category_id") or path.startswith("/ai_category"):
        return "wrong_category"
    if path.startswith("/images_json"):
        return "screenshot_misread"
    if path.startswith("/signals_json/signals"):
        if operation == "add":
            return "missing_signal"
        if operation == "delete":
            return "redundant_signal"
        if any(token in path for token in ("/command", "/host", "/container", "/command_args")):
            return "invalid_argument"
        if any(token in path for token in ("/match", "/extract")):
            return "threshold_or_match_error"
    return "other_expert_correction"


def _find_annotation(change: dict[str, Any], annotations: list[dict[str, str]]) -> dict[str, str] | None:
    path = str(change.get("path") or "")
    # 精确路径优先，再允许上级 JSON Pointer 覆盖其子字段。信号级标注仅作用于
    # 稳定 signal id 下的字段，不依赖可变化的展示序号。
    matches: list[dict[str, str]] = []
    for annotation in annotations:
        annotation_path = annotation.get("path")
        if annotation_path and (path == annotation_path or path.startswith(f"{annotation_path}/")):
            matches.append(annotation)
            continue
        signal_id = annotation.get("signal_id")
        if signal_id and path.startswith(f"/signals_json/signals/{signal_id}"):
            matches.append(annotation)
    if not matches:
        return None
    return max(matches, key=lambda item: len(item.get("path") or item.get("signal_id") or ""))


def build_review_metadata(
    *,
    parent_payload: dict[str, Any] | None,
    payload: dict[str, Any],
    annotations: list[dict[str, str]] | None = None,
    identity_status: str,
    review_state: str,
    reviewer_id: int | None = None,
    review_note: str | None = None,
) -> dict[str, Any]:
    """构造一条 Expert revision 的可训练、可审计元数据。"""

    changes = _diff_revision_payloads(parent_payload or {}, payload)
    normalized_annotations = annotations or []
    labelled_changes: list[dict[str, Any]] = []
    for change in changes:
        annotation = _find_annotation(change, normalized_annotations)
        reason_code = annotation["reason_code"] if annotation else _inferred_reason(change)
        labelled_changes.append(
            {
                "operation": change["operation"],
                "path": change["path"],
                "reason_code": reason_code,
                "reason_source": "expert" if annotation else "inferred",
                **({"note": annotation["note"]} if annotation and annotation.get("note") else {}),
            }
        )
    reason_counts = dict(sorted(Counter(item["reason_code"] for item in labelled_changes).items()))
    gold_blockers = ["trusted_reviewer_required", "evidence_replay_required", "execution_replay_required"]
    return {
        "schema_version": 1,
        "review_state": review_state,
        "identity_status": identity_status,
        **({"reviewer_id": reviewer_id} if reviewer_id is not None else {}),
        **({"review_note": review_note} if review_note else {}),
        "change_summary": {
            "total": len(labelled_changes),
            "reason_counts": reason_counts,
            "changes": labelled_changes,
        },
        # 这是“为什么当前不能称 Gold”的机器可读事实，不是一个虚假的 Gold 标记。
        "expert_gold": {"status": "not_eligible", "blockers": gold_blockers},
    }
