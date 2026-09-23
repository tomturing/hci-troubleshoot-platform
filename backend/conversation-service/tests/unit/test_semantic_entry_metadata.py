"""语义入口 SSE 元数据的客户侧安全展示契约。"""

from app.services.conversation_service import _semantic_entry_metadata


def test_title_recommendation_cannot_claim_verification_in_customer_metadata():
    metadata = _semantic_entry_metadata({
        "decision": "case_recommendations", "candidates": [{
            "kbd_id": "1", "title": "缺少介质驱动程序", "verified": True,
            "problem_excerpt": "安装系统报错", "recommendation_solution": "历史建议",
            "resource_revision": {"revision": 8}, "command": "untrusted command",
        }],
    })
    candidate = metadata["candidates"][0]
    assert candidate["verified"] is False
    assert candidate["problem_excerpt"] == "安装系统报错"
    assert candidate["resource_revision"] == {"revision": 8}
    assert "command" not in candidate


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
                    "manual_evidence_fields": [
                        {"id": "controller_type", "label": "控制器类型", "required": True, "input_type": "text"}
                    ],
                }
            ],
        }
    )

    assert metadata == {
        "decision": "inconclusive",
        "reason": "guidance_only",
        "candidates": [
            {
                "kbd_id": "",
                "support_id": "15936",
                "title": "ISO 安装缺少介质驱动程序",
                "diagnosis_capability": "guidance_only",
                "matched_positive_anchors": ["缺少介质驱动程序"],
                "manual_evidence_fields": [
                    {"id": "controller_type", "label": "控制器类型", "required": True, "input_type": "text", "placeholder": ""}
                ],
            }
        ],
    }


def test_semantic_recommendation_passes_through_reviewed_root_cause_and_solution():
    """高置信语义推荐必须原文透传已审核案例的根因与解决方案，与强信号口径一致。"""

    metadata = _semantic_entry_metadata(
        {
            "decision": "semantic_recommendation",
            "reason": "semantic_recommendation",
            "candidates": [
                {
                    "support_id": "15936",
                    "title": "ISO 安装缺少介质驱动程序",
                    "diagnosis_capability": "guidance_only",
                    "recommendation_conclusion": "安装镜像不完整或缺少磁盘控制器驱动",
                    "recommendation_solution": "更换经校验的完整安装镜像，并加载 VirtIO 磁盘控制器驱动",
                    "recommendation_facts": [
                        {"question": "错误是否发生在选择要安装的驱动程序界面？", "answer": "是"}
                    ],
                }
            ],
        }
    )

    assert metadata is not None
    candidate = metadata["candidates"][0]
    assert candidate["recommendation_conclusion"] == "安装镜像不完整或缺少磁盘控制器驱动"
    assert candidate["recommendation_solution"] == "更换经校验的完整安装镜像，并加载 VirtIO 磁盘控制器驱动"
    assert candidate["recommendation_facts"] == [{"question": "错误是否发生在选择要安装的驱动程序界面？", "answer": "是"}]
