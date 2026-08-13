"""Vision Evidence IR 的纯函数契约测试，不调用外部模型。"""

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.routes.extract_signals import (
    _diagnostic_image_source_refs,
    _format_image_evidence,
    _validate_and_collect_signals,
)
from app.services.vision_processor import (
    VisionEmptyResultError,
    _assess_inference,
    _build_context_map_from_images_json,
    _build_evidence_ir,
    _compress_image_if_needed,
    _freeze_vision_proposal,
    _parse_type,
    _parse_unstructured_ocr_text,
    _prefer_task_detail_type,
    _prepare_vision_images,
    _require_nonempty_vision_result,
    _vision_analyze,
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


def test_plain_ocr_model_output_is_kept_as_observed_text():
    raw = "```markdown\n虚拟机启动失败\n错误码：E1001\n```"

    assert _parse_unstructured_ocr_text(raw) == ["虚拟机启动失败", "错误码：E1001"]


def test_empty_or_structured_empty_output_is_rejected():
    with pytest.raises(VisionEmptyResultError):
        _require_nonempty_vision_result([], "")


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


@pytest.mark.asyncio
async def test_vision_generation_uses_shared_append_only_revision_path():
    session = AsyncMock()
    entry = SimpleNamespace(
        id=9,
        latest_proposal_revision_id=7,
        working_revision_id=8,
        images_json=[
            {
                "seq": 0,
                "evidence": {"provenance": {"image_sha256": "a" * 64}},
            }
        ],
    )
    created = SimpleNamespace(id=10)

    with patch(
        "app.services.vision_processor.freeze_kbd_ai_proposal",
        AsyncMock(return_value=created),
    ) as ensure:
        revision_id = await _freeze_vision_proposal(
            session,
            kbd_entry=entry,
            prompt_template="vision {context}",
            trace_id="trace-vision",
            origin="vision_reanalyze_single",
            scope={"mode": "single", "seqs": [0]},
            validation_summary={"status": "needs_review"},
        )

    assert revision_id == 10
    kwargs = ensure.await_args.kwargs
    assert kwargs["generation_kind"] == "vision"
    assert kwargs["origin"] == "vision_reanalyze_single"
    assert kwargs["generation_metadata"]["scope"] == {"mode": "single", "seqs": [0]}


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


@pytest.mark.asyncio
async def test_vision_retry_success_does_not_raise_stale_error():
    """首次超时、第二次成功时，不得再次抛出首次异常。"""

    import httpx

    image = Image.new("RGB", (2, 2), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=(
            "TYPE: 告警截图\nBACKGROUND: 白色\nFULL_TEXT:\n- 磁盘告警\nDESCRIPTION:\n磁盘告警截图"
        )))],
        usage=None,
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=[httpx.ReadTimeout("request timed out"), response])
            )
        )
    )

    with patch("app.services.vision_processor.asyncio.sleep", AsyncMock()):
        result = await _vision_analyze(
            client,
            buffer.getvalue(),
            "image/png",
            "磁盘异常",
            "{context}",
            trace_id="trace-retry-success",
        )

    assert result == ("告警截图", "白色", ["磁盘告警"], "磁盘告警截图")


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
    assert evidence_json == "[]"


def test_signal_prompt_excludes_root_cause_and_solution_images_and_context():
    images = [
        {
            "seq": 0,
            "section": "steps_text",
            "context_before": "查看配置文件",
            "context_after": "此处不应出现在图片输入中",
            "evidence": {
                "regions": [
                    {
                        "region_id": "img_0:r_0",
                        "text_lines": [{"text": "address 10.0.0.8"}],
                        "fields": {},
                        "observed_facts": ["截图可见文字：address 10.0.0.8"],
                    }
                ],
            },
        },
        {
            "seq": 1,
            "section": "root_cause",
            "context_before": "根因中的敏感结论",
            "evidence": {
                "regions": [{"region_id": "img_1:r_0", "text_lines": [{"text": "根因命令"}]}],
            },
        },
        {
            "seq": 2,
            "section": "solution",
            "context_before": "rm /sf/cfg/if.d/eth0",
            "evidence": {
                "regions": [{"region_id": "img_2:r_0", "text_lines": [{"text": "service restart"}]}],
            },
        },
    ]

    evidence = json.loads(_format_image_evidence(images))

    assert [item["source_ref"] for item in evidence] == ["img:0"]
    assert "context_before" not in evidence[0]
    assert "context_after" not in evidence[0]
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert "根因中的敏感结论" not in serialized
    assert "rm /sf/cfg/if.d/eth0" not in serialized
    assert "service restart" not in serialized


def test_kbd_candidate_cannot_reference_image_not_entering_diagnostic_prompt():
    images = [
        {
            "seq": 0,
            "section": "steps_text",
            "evidence": {
                "regions": [
                    {
                        "region_id": "img_0:r_0",
                        "text_lines": [{"text": "address 10.0.0.8"}],
                        "fields": {},
                        "observed_facts": ["截图可见文字：address 10.0.0.8"],
                    }
                ],
            },
        }
    ]
    candidate = {
        "id": "sig_001",
        "acquire": {"tool": "qfk_system", "args": {"command": "cat", "command_args": ["/sf/cfg/if.d/eth0"]}},
        "match": {
            "type": "keyword",
            "pattern": "address",
            "mode": "or",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"},
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
        "provenance": {
            "category": "backend",
            "source_section": "steps_text",
            "source_refs": ["img:2/region:img_2:r_0"],
            "evidence": "rm /sf/cfg/if.d/eth0",
        },
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate],
        source_id="kbd:27736",
        enforce_kbd_read_only=True,
        diagnostic_image_source_refs=_diagnostic_image_source_refs(images),
    )

    assert accepted == []
    assert rejected[0]["reason_code"] == "run_failed"
    assert "未进入诊断输入的截图" in rejected[0]["reason"]


def test_kbd_candidate_allows_body_evidence_with_diagnostic_image_reference():
    """正文证据和辅助截图可联合引用，不能被要求逐字相等。"""
    images = [
        {
            "seq": 2,
            "section": "steps_text",
            "evidence": {
                "regions": [
                    {
                        "region_id": "img_2:r_0",
                        "text_lines": [{"text": "address 10.0.0.8"}],
                    }
                ],
            },
        }
    ]
    candidate = {
        "id": "sig_001",
        "acquire": {"tool": "qfk_system", "args": {"command": "cat", "command_args": ["/sf/cfg/if.d/eth0"]}},
        "match": {
            "type": "exists",
            "mode": "or",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"},
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
        "provenance": {
            "category": "backend",
            "source_section": "steps_text",
            "source_refs": ["img:2/region:img_2:r_0"],
            "evidence": "管理口为channel4，但是eth0口也残留了跟管理口一样的ip",
        },
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate],
        source_id="kbd:27736",
        enforce_kbd_read_only=True,
        diagnostic_image_source_refs=_diagnostic_image_source_refs(images),
    )

    assert len(accepted) == 1
    assert rejected == []


def test_kbd_candidate_with_body_evidence_reaches_matcher_gate_after_image_ref_check():
    """KBD27736 型候选应报告真实 Matcher 问题，而非虚假的图片逐字追溯错误。"""
    images = [
        {
            "seq": 2,
            "section": "steps_text",
            "evidence": {
                "regions": [
                    {
                        "region_id": "img_2:r_0",
                        "text_lines": [{"text": "address xx.100.88"}],
                    }
                ],
            },
        }
    ]
    candidate = {
        "id": "sig_004",
        "acquire": {"tool": "qfk_system", "args": {"command": "cat", "command_args": ["/sf/cfg/if.d/channel4"]}},
        "match": {
            "type": "keyword",
            "pattern": "address xx.100.88",
            "mode": "or",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"},
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
        "provenance": {
            "category": "backend",
            "source_section": "steps_text",
            "source_refs": ["img:2/region:img_2:r_0"],
            "evidence": "管理口为channel4，但是eth0口也残留了跟管理口一样的ip",
        },
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate],
        source_id="kbd:27736",
        enforce_kbd_read_only=True,
        diagnostic_image_source_refs=_diagnostic_image_source_refs(images),
    )

    assert accepted == []
    assert rejected[0]["reason_code"] == "run_failed"
    assert "match.pattern 包含脱敏占位文本" in rejected[0]["reason"]
    assert "原子事实中逐字追溯" not in rejected[0]["reason"]


def test_signal_image_prompt_limit_keeps_only_complete_images_and_matching_source_refs():
    images = [
        {
            "seq": 0,
            "section": "steps_text",
            "evidence": {
                "regions": [{"region_id": "img_0:r_0", "text_lines": [{"text": "first"}]}],
            },
        },
        {
            "seq": 1,
            "section": "steps_text",
            "evidence": {
                "regions": [{"region_id": "img_1:r_0", "text_lines": [{"text": "second"}]}],
            },
        },
    ]
    first_image_payload = _format_image_evidence([images[0]])

    payload = _format_image_evidence(images, max_chars=len(first_image_payload))
    source_refs = _diagnostic_image_source_refs(images, max_chars=len(first_image_payload))

    assert json.loads(payload) == json.loads(first_image_payload)
    assert source_refs == {"img:0/region:img_0:r_0"}
