"""语义入口 SSE 元数据的客户侧安全展示契约。"""

from app.services.conversation_service import _semantic_entry_metadata


def test_semantic_entry_metadata_keeps_only_display_safe_candidate_fields():
    metadata = _semantic_entry_metadata(
        {
            "decision": "inconclusive",
            "reason": "guidance_only",
            "candidates": [
                {
                    "support_id": "15936",
                    "title": "ISO 安装缺少介质驱动程序",
                    "diagnosis_capability": "guidance_only",
                    "matched_positive_anchors": ["缺少介质驱动程序"],
                    "manual_evidence_request": ["不应作为卡片元数据透传"],
                }
            ],
        }
    )

    assert metadata == {
        "decision": "inconclusive",
        "reason": "guidance_only",
        "candidates": [
            {
                "support_id": "15936",
                "title": "ISO 安装缺少介质驱动程序",
                "diagnosis_capability": "guidance_only",
                "matched_positive_anchors": ["缺少介质驱动程序"],
            }
        ],
    }
