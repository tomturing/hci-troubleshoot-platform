"""Vision Evidence IR 的纯函数契约测试，不调用外部模型。"""

import io

from app.routes.extract_signals import _format_image_evidence
from app.services.vision_processor import (
    _assess_inference,
    _build_context_map_from_images_json,
    _build_evidence_ir,
    _compress_image_if_needed,
    _parse_type,
    _prefer_task_detail_type,
    _prepare_vision_images,
)
from PIL import Image


def test_context_comes_from_persisted_image_metadata():
    context_map = _build_context_map_from_images_json([
        {
            "seq": 2,
            "section": "alert_info",
            "context_before": "编辑显卡核心",
            "context_after": "随后出现失败提示",
        }
    ])

    assert context_map[2] == "【截图前文】编辑显卡核心\n【截图后文】随后出现失败提示"


def test_dialog_is_a_first_class_screenshot_type():
    assert _parse_type("TYPE: 弹框截图\nBACKGROUND: 白色") == "弹框截图"


def test_task_detail_modal_is_not_downgraded_to_dialog():
    assert _prefer_task_detail_type(
        "弹框截图",
        [
            "状态：失败",
            "行为：启动虚拟机",
            "起始时间：2024-03-14 16:03:45",
            "结束时间：2024-03-14 16:04:00",
            "对象类型：虚拟机",
        ],
    ) == "任务截图"


def test_generic_error_dialog_remains_dialog():
    assert _prefer_task_detail_type(
        "弹框截图",
        ["操作失败", "错误信息：虚拟机镜像忙，正在执行其他操作"],
    ) == "弹框截图"


def test_evidence_ir_separates_observed_text_from_inference():
    evidence = _build_evidence_ir(
        seq=0,
        section="alert_info",
        context_before="编辑显卡核心",
        context_after="",
        screenshot_type="弹框截图",
        full_text=["编辑显卡核心失败", "设置显卡切分方式失败"],
        description="该弹框说明显卡配置操作失败。",
        image_data=b"original-image",
        prompt_template="context={context}",
    )

    region = evidence["regions"][0]
    assert region["evidence_type"] == "dialog"
    assert region["observed_facts"] == [
        "截图可见文字：编辑显卡核心失败",
        "截图可见文字：设置显卡切分方式失败",
    ]
    assert region["inferences"] == ["该弹框说明显卡配置操作失败。"]
    assert evidence["quality"] == {
        "ocr_coverage": None,
        "type_confidence": None,
        "status": "success",
        "needs_review": False,
        "inference_status": "unverified",
        "inference_needs_review": True,
        "inference_issues": ["single_model_semantic_inference"],
    }
    assert len(evidence["provenance"]["image_sha256"]) == 64


def test_empty_vision_result_is_low_quality_and_requires_review():
    evidence = _build_evidence_ir(
        seq=1,
        section="steps_text",
        context_before="",
        context_after="",
        screenshot_type="任务截图",
        full_text=[],
        description="",
        image_data=b"image",
        prompt_template="{context}",
    )

    assert evidence["quality"]["status"] == "low_quality"
    assert evidence["quality"]["needs_review"] is True
    assert evidence["quality"]["inference_status"] == "not_present"
    assert evidence["quality"]["inference_needs_review"] is False


def test_causal_description_is_flagged_without_promoting_it_to_fact():
    description = "聚合口掉线是导致后续内存错误的根本原因。"

    status, needs_review, issues = _assess_inference(description)
    evidence = _build_evidence_ir(
        seq=0,
        section="alert_info",
        context_before="",
        context_after="",
        screenshot_type="告警截图",
        full_text=["数据通信口(vxlan)告警"],
        description=description,
        image_data=b"image",
        prompt_template="{context}",
    )

    assert status == "needs_review"
    assert needs_review is True
    assert issues == ["unsupported_causal_claim"]
    assert evidence["quality"]["status"] == "success"
    assert evidence["quality"]["needs_review"] is False
    assert evidence["quality"]["inference_status"] == "needs_review"
    assert evidence["quality"]["inference_needs_review"] is True
    assert evidence["regions"][0]["observed_facts"] == ["截图可见文字：数据通信口(vxlan)告警"]
    assert evidence["regions"][0]["inferences"] == [description]


def test_small_png_is_not_lossily_reencoded():
    original = b"\x89PNG\r\n\x1a\n" + b"text-pixel-data"

    processed, mime_type = _compress_image_if_needed(original, "image/png")

    assert processed is original
    assert mime_type == "image/png"


def test_tall_text_image_is_split_with_overlap_and_source_coordinates():
    source = Image.new("RGB", (600, 5000), "white")
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")

    tiles = _prepare_vision_images(buffer.getvalue(), "image/png")
    bboxes = [bbox for _, mime, bbox in tiles if mime == "image/png" and bbox]

    assert 2 <= len(tiles) <= 8
    assert bboxes[0] == (0, 0, 600, 1800)
    assert bboxes[-1][3] == 5000
    assert all(
        current[1] < previous[3]
        for previous, current in zip(bboxes, bboxes[1:], strict=False)
    )


def test_signal_prompt_receives_observed_image_facts_with_source_refs():
    evidence_json = _format_image_evidence([{
        "seq": 0,
        "section": "alert_info",
        "context_before": "编辑显卡核心",
        "context_after": "",
        "evidence": {
            "regions": [{
                "region_id": "img_0:r_0",
                "surface": "unknown",
                "evidence_type": "dialog",
                "text_lines": [{"text": "编辑显卡核心失败", "confidence": None}],
                "fields": {"message": "设置显卡切分方式失败"},
                "observed_facts": ["页面显示编辑显卡核心失败"],
                "inferences": ["可能是授权异常"],
            }],
            "quality": {"status": "success", "needs_review": False},
        },
    }])

    assert '"source_ref":"img:0/region:img_0:r_0"' in evidence_json
    assert '"observed_facts":["页面显示编辑显卡核心失败"]' in evidence_json
    assert "可能是授权异常" not in evidence_json
    assert "inferences_not_facts" not in evidence_json


def test_signal_prompt_never_receives_legacy_description_text():
    evidence_json = _format_image_evidence([{
        "seq": 1,
        "section": "steps_text",
        "desc": "DESCRIPTION: 未经验证的根因推断",
    }])

    assert "未经验证的根因推断" not in evidence_json
    assert '"legacy_evidence_unavailable":true' in evidence_json
