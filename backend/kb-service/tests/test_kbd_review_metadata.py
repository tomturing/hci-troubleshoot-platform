from app.routes.admin import _derive_capability_gaps
from app.services.kbd_review_metadata import build_review_metadata, normalize_change_annotations


def _payload(signals):
    return {
        "title": "GPU 主机异常",
        "signals_json": {"schema_version": 2, "signals": signals},
        "payload_schema_version": 1,
    }


def test_delete_signal_uses_expert_reason_and_stable_signal_id():
    before = _payload(
        [
            {"id": "sig_001", "acquire": {"tool": "qkv_task"}},
            {"id": "sig_004", "acquire": {"tool": "qfk_system"}},
        ]
    )
    after = _payload([{"id": "sig_001", "acquire": {"tool": "qkv_task"}}])

    metadata = build_review_metadata(
        parent_payload=before,
        payload=after,
        annotations=normalize_change_annotations(
            [
                {
                    "signal_id": "sig_004",
                    "reason_code": "redundant_signal",
                    "note": "正常 GPU 主机示例不应在异常主机执行",
                }
            ]
        ),
        identity_status="unavailable",
        review_state="working",
    )

    change = metadata["change_summary"]["changes"][0]
    assert change["operation"] == "delete"
    assert change["path"].startswith("/signals_json/signals/sig_004")
    assert change["reason_code"] == "redundant_signal"
    assert change["reason_source"] == "expert"
    assert metadata["expert_gold"]["status"] == "not_eligible"


def test_unannotated_signal_add_gets_conservative_reason_code():
    metadata = build_review_metadata(
        parent_payload=_payload([]),
        payload=_payload([{"id": "sig_new", "acquire": {"tool": "qfk_log"}}]),
        identity_status="unavailable",
        review_state="working",
    )

    assert metadata["change_summary"]["reason_counts"] == {"missing_signal": 1}


def test_category_and_vision_expert_corrections_become_learning_labels():
    before = {
        "category_id": None,
        "ai_category_id": "vm-001",
        "images_json": [{"seq": 0, "desc": "AI 误读"}],
        "payload_schema_version": 1,
    }
    after = {
        "category_id": "storage-002",
        "ai_category_id": "vm-001",
        "images_json": [{"seq": 0, "desc": "专家确认的截图文字"}],
        "payload_schema_version": 1,
    }

    metadata = build_review_metadata(
        parent_payload=before,
        payload=after,
        identity_status="unavailable",
        review_state="working",
    )

    assert metadata["change_summary"]["reason_counts"] == {
        "screenshot_misread": 1,
        "wrong_category": 1,
    }
    labelled_paths = {
        item["path"]: item["reason_code"]
        for item in metadata["change_summary"]["changes"]
    }
    assert labelled_paths["/category_id"] == "wrong_category"
    assert labelled_paths["/images_json/0/desc"] == "screenshot_misread"


def test_rejects_uncontrolled_reason_code_and_unstable_target():
    try:
        normalize_change_annotations([{"signal_id": "sig_1", "reason_code": "因为我觉得不对"}])
    except ValueError as exc:
        assert "受控原因码" in str(exc)
    else:
        raise AssertionError("未拒绝自由原因码")

    try:
        normalize_change_annotations([{"reason_code": "redundant_signal"}])
    except ValueError as exc:
        assert "path 或 signal_id" in str(exc)
    else:
        raise AssertionError("未拒绝无目标标注")


def test_capability_gap_is_engineering_data_not_expert_todo():
    gaps = _derive_capability_gaps(
        {
            "schema_version": 2,
            "signals": [
                {"id": "sig_known", "acquire": {"tool": "qkv_task", "args": {}}},
                {"id": "sig_unknown", "acquire": {"tool": "filesystem.count_entries", "args": {}}},
            ],
        }
    )

    assert {
        (item["capability_id"], item["code"], item["signal_id"])
        for item in gaps
    } == {
        ("qkv_task", "CAPABILITY_RUNTIME_UNVERIFIED", "sig_known"),
        ("filesystem.count_entries", "CAPABILITY_UNDECLARED", "sig_unknown"),
    }
